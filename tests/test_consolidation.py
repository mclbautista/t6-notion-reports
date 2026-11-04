"""LLM consolidation (#47), through the brief job with a fake Notion, a fake LLM and a fake gateway."""
import json
import re
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from fake_notion import CONTACTS, MEETING, PROJECTS, FakeNotion, block, meeting_page, rich
from t6_notion_reports.brief_job import run_brief
from t6_notion_reports.cli import main
from t6_notion_reports.llm import LlmError
from t6_notion_reports.snapshot_job import run_snapshot
from t6_notion_reports.snapshots import SCHEMA
from test_gated_briefs import BRIEF, FRIDAY, IN_PROGRESS, MONDAY, SATURDAY, Gateway, contacts, dm, digest, proj

LAST_FRIDAY = datetime(2026, 11, 6, 15, 30, tzinfo=timezone.utc)
NOTES = "As of Nov 2\n• Online started, TLR piclock TBC\nAs of Nov 9\n• QC on Nov 20, waiting for client notes"
STALE = "As of Aug 3\n• Offline locked\nAs of Aug 10\n• Colour pass on Dec 2"


def page(page_id="p1", code="ONE", notes=NOTES, stage="Online", **kw):
    return proj(page_id, code, stage=stage, **{"PM Notes": rich(notes)}, **kw)


def make_db(last_friday, monday, friday):
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    for kind, at, pages in (("friday", LAST_FRIDAY, last_friday), ("monday", MONDAY, monday), ("friday", FRIDAY, friday)):
        if pages is not None:
            run_snapshot(conn, FakeNotion(pages, contacts()), PROJECTS, CONTACTS, kind, at)
    return conn


class Llm:
    """Records every payload it gets and answers with ``reply(payload)``, or fails for the named projects."""

    def __init__(self, reply=None, fail_for=()):
        self.payloads, self.reply, self.fail_for = [], reply or (lambda p: {}), set(fail_for)

    def complete(self, system, user, schema):
        payload = json.loads(user)
        self.payloads.append(payload)
        if payload["project"]["name"] in self.fail_for:
            raise LlmError("down")
        return schema(**self.reply(payload))


def line_id(payload, text, where="pm_notes"):
    rows = payload["pm_notes"] if where == "pm_notes" else payload["minutes"]["lines"]
    return next(r["line_id"] for r in rows if text in r["text"])


def run(conn, llm=None, notion=None, brief=BRIEF, **kw):
    gw, logs = Gateway(), []
    run_brief(conn, notion or FakeNotion([], contacts()), CONTACTS, brief, SATURDAY, gateway=gw, llm=llm,
              log=logs.append, **kw)
    return gw, logs


def minutes_notion(*children, edited="2026-11-09T14:10:00.000+08:00", sent_at="2026-11-09T14:30:00+08:00"):
    notion = FakeNotion([], contacts(), meetings=[meeting_page("m1", "PSR MOM 2026-11-09", "2026-11-09", sent_at=sent_at)])
    notion.meetings[0]["last_edited_time"] = edited
    notion.page_blocks["m1"] = [
        block("heading_2", "🟠 At risk"),
        block("bulleted_list_item", "ONE", children=[block("bulleted_list_item", c) for c in children]),
        block("bulleted_list_item", "OTHER", children=[block("bulleted_list_item", "Not for ONE")]),
    ]
    return notion


def conn_one(notes=NOTES):
    return make_db([page(notes=notes, stage="Offline")], [page(notes=notes, stage="Offline")], [page(notes=notes)])


# ---- payload --------------------------------------------------------------------------------

def test_the_payload_holds_only_the_listed_fields_for_gated_projects():
    done = proj("p2", "DONE", "✅ Done", coordinator="c-ana")
    conn = make_db([page(stage="Offline"), done], [page(stage="Offline"), done], [page(), done])
    llm = Llm()
    run(conn, llm)
    assert [p["project"]["name"] for p in llm.payloads] == ["Show ONE"]  # DONE never reaches the LLM
    payload = llm.payloads[0]
    assert set(payload) == {
        "today", "project", "dates", "blank_date_properties", "pm_notes", "snapshot_changes", "minutes"}
    assert payload["today"] == "2026-11-14"
    assert payload["project"] == {
        "name": "Show ONE", "stage": "Online", "status": "🚨 In-Progress", "project_health": "ON TRACK"}
    assert payload["dates"] == {"playdate": None, "submission_deadline": None, "archival_deadline": None}
    assert payload["blank_date_properties"] == ["playdate", "submission_deadline"]  # relevant for In-Progress
    text = json.dumps(payload, ensure_ascii=False)
    for private in ("Pio", "Ana", "c-pio", "c-ana", "p1", "p2", "9999", "111"):
        assert private not in text


def test_pm_notes_lines_are_numbered_and_tagged_with_their_block():
    llm = Llm()
    run(conn_one(), llm)
    assert llm.payloads[0]["pm_notes"] == [
        {"line_id": 1, "block": "As of Nov 2", "text": "As of Nov 2"},
        {"line_id": 2, "block": "As of Nov 2", "text": "• Online started, TLR piclock TBC"},
        {"line_id": 3, "block": "As of Nov 9", "text": "As of Nov 9"},
        {"line_id": 4, "block": "As of Nov 9", "text": "• QC on Nov 20, waiting for client notes"},
    ]


def test_pm_notes_older_than_the_window_are_sent_only_when_they_hold_a_future_date():
    llm = Llm()
    run(conn_one(NOTES + "\n" + STALE), llm, brief=replace(BRIEF, pm_notes_weeks=4))
    texts = [r["text"] for r in llm.payloads[0]["pm_notes"]]
    assert "• Offline locked" not in texts and "As of Aug 3" not in texts  # old, no future date
    assert "• Colour pass on Dec 2" in texts and "As of Aug 10" in texts  # old, but Dec 2 is ahead
    assert "• QC on Nov 20, waiting for client notes" in texts
    llm = Llm()
    run(conn_one(NOTES + "\n" + STALE), llm)  # the default 52 weeks reaches everything here
    assert "• Offline locked" in [r["text"] for r in llm.payloads[0]["pm_notes"]]


def test_both_diff_windows_and_the_minutes_lines_are_in_the_payload():
    conn = make_db([page(stage="Preprod")], [page(stage="Offline")], [page(stage="Online")])
    llm = Llm()
    run(conn, llm, notion=minutes_notion("Submission moved to Nov 28", "Grade on Nov 24"), meeting_notes_ds=MEETING)
    payload = llm.payloads[0]
    assert payload["snapshot_changes"] == [
        {"window": "since_last_brief", "from": "2026-11-06T23:30:00+08:00", "to": "2026-11-13T23:30:00+08:00",
         "changes": [{"field": "Stage", "old": "Preprod", "new": "Online"}]},
        {"window": "since_psr", "from": "2026-11-09T11:00:00+08:00", "to": "2026-11-13T23:30:00+08:00",
         "changes": [{"field": "Stage", "old": "Offline", "new": "Online"}]},
    ]
    assert payload["minutes"] == {
        "page_date": "2026-11-09", "last_edited": "2026-11-09T14:10:00.000+08:00", "sent_at": "2026-11-09T14:30:00+08:00",
        "lines": [{"line_id": 5, "text": "• Submission moved to Nov 28"}, {"line_id": 6, "text": "• Grade on Nov 24"}],
    }  # ids continue after the PM Notes lines; another project's lines are not here


def test_no_minutes_lines_for_the_project_means_no_minutes_in_the_payload():
    llm = Llm()
    notion = minutes_notion()  # ONE has no children
    notion.page_blocks["m1"][1] = block("bulleted_list_item", "ONE - last updated Nov 2")
    run(conn_one(), llm, notion=notion, meeting_notes_ds=MEETING)
    assert llm.payloads[0]["minutes"] is None


def test_the_latest_minutes_page_on_or_before_today_is_the_one_read():
    llm = Llm()
    notion = minutes_notion("Submission moved to Nov 28")
    older = meeting_page("m0", "PSR MOM 2026-11-02", "2026-11-02")
    future = meeting_page("m2", "PSR MOM 2026-11-16", "2026-11-16")
    other = meeting_page("m3", "Team lunch", "2026-11-13")
    notion.meetings += [older, future, other]
    for pid in ("m0", "m2", "m3"):
        notion.page_blocks[pid] = [block("bulleted_list_item", "ONE", children=[block("bulleted_list_item", "wrong page")])]
    run(conn_one(), llm, notion=notion, meeting_notes_ds=MEETING)
    assert [r["text"] for r in llm.payloads[0]["minutes"]["lines"]] == ["• Submission moved to Nov 28"]
    assert llm.payloads[0]["minutes"]["page_date"] == "2026-11-09"


def test_show_llm_input_prints_every_payload_and_calls_no_llm():
    llm, printed = Llm(), []
    conn = conn_one()
    res = run_brief(conn, FakeNotion([], contacts()), CONTACTS, BRIEF, SATURDAY, gateway=Gateway(), llm=llm,
                    dry_print=True, show_llm_input=True, out=printed.append, log=lambda _: None)
    assert llm.payloads == [] and res.sent == 0
    shown = "\n".join(printed)
    assert "--- llm input: ONE ---" in shown and '"pm_notes"' in shown and "TLR piclock TBC" in shown


def test_cli_show_llm_input_needs_dry_print(tmp_path, monkeypatch, capsys):
    from test_brief_job import AnyContacts, cfg_contacts, seeded_db, write_config
    monkeypatch.setenv("NOTION_TOKEN", "ntn_x")
    cfg = write_config(tmp_path, seeded_db(tmp_path))
    now = datetime(2026, 11, 15, 8, tzinfo=timezone.utc)
    assert main(["brief", "--show-llm-input", "--config", cfg], notion=AnyContacts([], cfg_contacts()), now=now) == 2
    assert "--dry-print" in capsys.readouterr().err
    assert main(["brief", "--dry-print", "--show-llm-input", "--config", cfg],
                notion=AnyContacts([], cfg_contacts()), now=now) == 0
    assert "--- llm input:" in capsys.readouterr().out


# ---- validation -----------------------------------------------------------------------------

def reply_with(**items):
    def reply(payload):
        return {k: (v(payload) if callable(v) else v) for k, v in items.items()}
    return reply


def test_an_item_that_fails_validation_is_dropped_and_the_rest_of_the_reply_still_renders():
    qc = lambda p: line_id(p, "QC on Nov 20")
    online = lambda p: line_id(p, "Online started")
    changes = lambda p: [
        {"text": "QC on Nov 20", "source": "pm_notes", "line_id": qc(p), "quote": "• QC on Nov 20, waiting for client notes"},
        {"text": "Online started", "source": "pm_notes", "line_id": online(p), "quote": "Online started, TLR piclock TBC"},
        {"text": "Wrong quote", "source": "pm_notes", "line_id": qc(p), "quote": "something else entirely"},
        {"text": "Wrong line", "source": "pm_notes", "line_id": 99, "quote": "• QC on Nov 20, waiting for client notes"},
        {"text": "Wrong source", "source": "minutes", "line_id": qc(p), "quote": "• QC on Nov 20, waiting for client notes"},
        {"text": "QC moved to Dec 9", "source": "pm_notes", "line_id": qc(p), "quote": "• QC on Nov 20, waiting for client notes"},
        {"text": "QC with Jam", "source": "pm_notes", "line_id": qc(p), "quote": "• QC on Nov 20, waiting for client notes"},
        {"text": "QC after 3 days", "source": "pm_notes", "line_id": qc(p), "quote": "• QC on Nov 20, waiting for client notes"},
        {"text": "QC invented", "source": "invented", "line_id": qc(p), "quote": "• QC on Nov 20, waiting for client notes"},
        {"text": "Stage Offline to Online", "source": "snapshot"},
    ]
    gw, _ = run(conn_one(), Llm(reply_with(changes=changes)))
    text = dm(gw)
    assert "• QC on Nov 20" in text and "• Online started" in text and "• Stage Offline to Online" in text
    for dropped in ("Wrong", "Dec 9", "Jam", "3 days", "invented"):
        assert dropped not in text


def test_a_hallucinated_name_in_a_concern_drops_only_that_concern():
    qc = lambda p: line_id(p, "waiting for client notes")
    concerns = lambda p: [
        {"kind": "open_item", "text": "QC on Nov 20 still waits for client notes", "source": "pm_notes",
         "line_id": qc(p), "quote": "• QC on Nov 20, waiting for client notes"},
        {"kind": "open_item", "text": "QC on Nov 20 still waits for Marco", "source": "pm_notes",
         "line_id": qc(p), "quote": "• QC on Nov 20, waiting for client notes"},
        {"kind": "mystery", "text": "QC on Nov 20 is odd", "source": "pm_notes",
         "line_id": qc(p), "quote": "• QC on Nov 20, waiting for client notes"},
    ]
    gw, _ = run(conn_one(), Llm(reply_with(concerns=concerns)))
    text = dm(gw)
    assert "QC on Nov 20 still waits for client notes · since Nov 14 (0 wks)" in text
    assert "Marco" not in text and "is odd" not in text


def test_health_vs_evidence_is_dropped_when_project_health_is_blank():
    concerns = lambda p: [{"kind": "health_vs_evidence", "text": "Waiting on client notes but health is fine",
                           "source": "pm_notes", "line_id": line_id(p, "waiting for client"),
                           "quote": "• QC on Nov 20, waiting for client notes"}]
    one = lambda health: make_db(*[[page(health=health)]] * 3)
    assert "Waiting on client notes" not in dm(run(one(None), Llm(reply_with(concerns=concerns)))[0])
    assert "Waiting on client notes" in dm(run(one("AT RISK"), Llm(reply_with(concerns=concerns)))[0])


def test_llm_concerns_age_through_the_concern_store():
    pending_notes = NOTES.replace("waiting for client notes", "pending client notes")
    concerns = lambda p: [{"kind": "open_item", "text": "Client notes are still pending", "source": "pm_notes",
                           "line_id": line_id(p, "pending client"), "quote": "• QC on Nov 20, pending client notes"}]
    conn = conn_one(pending_notes)
    run(conn, Llm(reply_with(concerns=concerns)))
    later = datetime(2026, 11, 28, 4, tzinfo=timezone.utc)
    gw = Gateway()
    run_brief(conn, FakeNotion([], contacts()), CONTACTS, BRIEF, later, gateway=gw,
              llm=Llm(reply_with(concerns=concerns)), log=lambda _: None)
    assert "Client notes are still pending · since Nov 14 (2 wks)" in dm(gw)
    gw = Gateway()
    run_brief(conn, FakeNotion([], contacts()), CONTACTS, BRIEF, datetime(2026, 12, 5, 4, tzinfo=timezone.utc),
              gateway=gw, llm=Llm(), log=lambda _: None)
    assert "Resolved last week: Client notes are still pending" in dm(gw)


# ---- last update ----------------------------------------------------------------------------

def weeks_text(gw):
    return re.search(r"updated [^<\n]*", dm(gw)).group(0)


def test_a_valid_last_update_sets_the_weeks_since_update():
    # Notion says edited Nov 13 (this week); the header the LLM picked is Nov 2: 12 days = 1 week.
    last = lambda p: {"line_id": line_id(p, "As of Nov 2"), "as_of_date": "2026-11-02"}
    gw, _ = run(conn_one(), Llm(reply_with(last_update=last)))
    assert weeks_text(gw) == "updated 1 wk ago"


def test_an_invalid_or_future_last_update_falls_back_to_last_edited_time():
    cases = [
        lambda p: {"line_id": line_id(p, "As of Nov 9"), "as_of_date": "2026-11-20"},  # in the future
        lambda p: {"line_id": line_id(p, "QC on Nov 20"), "as_of_date": "2026-11-02"},  # not a header
        lambda p: {"line_id": 99, "as_of_date": "2026-11-02"},  # no such line
        lambda p: {"line_id": line_id(p, "As of Nov 2"), "as_of_date": "second of November"},  # not a date
    ]
    for case in cases:
        gw, _ = run(conn_one(), Llm(reply_with(last_update=case)))
        assert weeks_text(gw) == "updated this week", case


# ---- changes --------------------------------------------------------------------------------

def test_after_psr_changes_are_marked_and_duplicates_across_windows_show_once():
    changes = [
        {"text": "Stage moved to Online", "source": "snapshot", "after_psr": False},
        {"text": "stage  moved to Online", "source": "snapshot", "after_psr": True},  # same change, other window
        {"text": "Online started", "source": "snapshot", "after_psr": True},
    ]
    gw, _ = run(conn_one(), Llm(lambda p: {"changes": changes}))
    text = dm(gw)
    assert text.count("Stage moved to Online") == 1 and "stage  moved" not in text
    assert "• Stage moved to Online (after PSR)" in text and "• Online started (after PSR)" in text


def test_consolidated_changes_replace_the_raw_changes_list():
    gw, _ = run(conn_one(), Llm(lambda p: {"changes": [{"text": "Online started", "source": "snapshot"}]}))
    text = dm(gw)
    assert "• Online started" in text
    assert "→" not in text and "PM Notes ·" not in text  # no raw field change, no raw notes quote


def test_a_project_whose_raw_changes_were_all_consolidated_away_and_has_nothing_else_is_left_out():
    from test_flags import staffed
    mk = lambda stage: [page(stage=stage, **staffed())]
    conn = make_db(mk("Offline"), mk("Offline"), mk("Online"))
    gw, _ = run(conn, Llm())  # a Stage change, but the LLM reports no change
    assert [to for to, *_ in gw.sent] == [999]  # no DM for the coordinator, only the digest
    assert "No changes on any project this week" in digest(gw)


def test_minutes_changes_list_a_project_that_has_no_snapshot_change():
    from test_flags import staffed
    mk = lambda: [page(**staffed())]
    conn = make_db(mk(), mk(), mk())
    change = lambda p: {"changes": [{"text": "Submission moved to Nov 28", "source": "minutes",
                                     "line_id": line_id(p, "Submission moved", "minutes"),
                                     "quote": "Submission moved to Nov 28"}]}
    gw, _ = run(conn, Llm(change), notion=minutes_notion("Submission moved to Nov 28"), meeting_notes_ds=MEETING)
    assert "• Submission moved to Nov 28" in dm(gw)


# ---- failure --------------------------------------------------------------------------------

def two_projects():
    a, b = page("p1", "ONE", stage="Offline"), page("p2", "TWO", stage="Offline", coordinator="c-ana")
    a2, b2 = page("p1", "ONE"), page("p2", "TWO", coordinator="c-ana")
    return make_db([a, b], [a, b], [a2, b2])


def test_a_failing_call_marks_that_project_only_and_the_digest_names_it():
    gw, logs = run(two_projects(), Llm(lambda p: {"changes": [{"text": "Online started", "source": "snapshot"}]},
                                       fail_for={"Show ONE"}))
    one = dm(gw)
    assert "(LLM unavailable)" in one and "Stage: Offline → Online" in one  # code-only: the raw diff
    assert "(LLM unavailable)" not in "\n".join(t for to, _, _, t in gw.sent if to == 222)
    assert "⚠️ LLM unavailable: ONE" in digest(gw) and "TWO" not in digest(gw).split("LLM unavailable:")[1]
    assert any(l == "brief llm unavailable project=p1" for l in logs)
    assert not any("Show ONE" in l for l in logs)


def test_invalid_json_counts_as_a_failure():
    class BadJson:
        def complete(self, system, user, schema):
            raise LlmError("OpenRouter answer did not match the schema")

    gw, _ = run(conn_one(), BadJson())
    assert "(LLM unavailable)" in dm(gw)


def test_a_failed_call_does_not_resolve_the_projects_open_concerns():
    concerns = lambda p: [{"kind": "open_item", "text": "Client notes are still pending", "source": "pm_notes",
                           "line_id": line_id(p, "waiting for client"), "quote": "• QC on Nov 20, waiting for client notes"}]
    conn = conn_one()
    run(conn, Llm(reply_with(concerns=concerns)))
    gw = Gateway()
    run_brief(conn, FakeNotion([], contacts()), CONTACTS, BRIEF, datetime(2026, 11, 21, 4, tzinfo=timezone.utc),
              gateway=gw, llm=Llm(fail_for={"Show ONE"}), log=lambda _: None)
    text = dm(gw)
    assert "Client notes are still pending · since Nov 14 (1 wk)" in text and "Resolved last week" not in text


def test_the_owed_items_section_is_gone():
    gw, _ = run(conn_one(), Llm())
    assert not any("What you owe" in t for *_, t in gw.sent)
