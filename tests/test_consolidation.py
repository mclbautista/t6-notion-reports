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


def snapshot_change(payload, window="since_psr", **item):
    """A change citing the payload's first snapshot change in ``window``, with its exact text."""
    w = next(w for w in payload["snapshot_changes"] if w["window"] == window)
    c = w["changes"][0]
    return {"source": "snapshot", "line_id": c["line_id"], "quote": c["text"], **item}


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
         "changes": [{"line_id": 7, "field": "Stage", "old": "Preprod", "new": "Online", "text": "Stage: Preprod → Online"}]},
        {"window": "since_psr", "from": "2026-11-09T11:00:00+08:00", "to": "2026-11-13T23:30:00+08:00",
         "changes": [{"line_id": 8, "field": "Stage", "old": "Offline", "new": "Online", "text": "Stage: Offline → Online"}]},
    ]  # snapshot changes are numbered after the Minutes lines, so they can be cited too
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
        snapshot_change(p, text="Stage Offline to Online"),
        {"text": "Snapshot without a citation", "source": "snapshot"},
        {"text": "Snapshot invented line", "source": "snapshot", "line_id": 99999, "quote": "Stage: Offline → Online"},
        {**snapshot_change(p, text="Snapshot wrong quote"), "quote": "Stage: Preprod → Online"},
    ]
    gw, _ = run(conn_one(), Llm(reply_with(changes=changes)))
    text = dm(gw)
    assert "• QC on Nov 20" in text and "• Online started" in text and "• Stage Offline to Online" in text
    for dropped in ("Wrong", "Dec 9", "Jam", "3 days", "invented", "Snapshot"):
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
        lambda p: {"line_id": line_id(p, "As of Nov 2"), "as_of_date": "2026-10-01"},  # not the readable header's date
    ]
    for case in cases:
        gw, _ = run(conn_one(), Llm(reply_with(last_update=case)))
        assert weeks_text(gw) == "updated this week", case


# ---- changes --------------------------------------------------------------------------------

def test_after_psr_changes_are_marked_and_duplicates_across_windows_show_once():
    changes = lambda p: [
        snapshot_change(p, text="Stage moved to Online", after_psr=False),
        snapshot_change(p, text="stage  moved to Online", after_psr=True),  # same change, other window
        snapshot_change(p, text="Online started", after_psr=True),
    ]
    gw, _ = run(conn_one(), Llm(lambda p: {"changes": changes(p)}))
    text = dm(gw)
    assert text.count("Stage moved to Online") == 1 and "stage  moved" not in text
    assert "• Stage moved to Online (after PSR)" in text and "• Online started (after PSR)" in text


def test_consolidated_changes_replace_the_raw_changes_list():
    gw, _ = run(conn_one(), Llm(lambda p: {"changes": [snapshot_change(p, text="Online started")]}))
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
    gw, logs = run(two_projects(), Llm(lambda p: {"changes": [snapshot_change(p, text="Online started")]},
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


# ---- PM Notes years (#60) -------------------------------------------------------------------

OLD_2024 = "As of Nov 1, 2024\n• Offline locked in 2024"


def test_a_header_with_an_old_year_is_outside_the_window_and_can_be_cited_as_the_last_update():
    llm = Llm()
    run(conn_one(OLD_2024 + "\n" + NOTES), llm)
    texts = [r["text"] for r in llm.payloads[0]["pm_notes"]]
    assert "As of Nov 1, 2024" not in texts and "• Offline locked in 2024" not in texts
    notes = OLD_2024  # only the old block: the LLM cites it as the real last update
    llm = Llm(reply_with(last_update=lambda p: {"line_id": 1, "as_of_date": "2024-11-01"}))
    gw, _ = run(conn_one(notes), llm, brief=replace(BRIEF, pm_notes_weeks=200))
    assert llm.payloads[0]["pm_notes"][0]["text"] == "As of Nov 1, 2024"
    assert weeks_text(gw) == "updated 106 wks ago"  # Nov 1, 2024 to Sat Nov 14, 2026: 743 days


def test_an_old_block_is_kept_for_a_far_or_iso_future_date_and_dropped_for_a_past_year():
    cases = {
        "As of Aug 3\n• Launch in July 2027": True,
        "As of Aug 3\n• Delivery 2027-07-01": True,
        "As of Aug 3\n• Festival Q1 2027": True,
        "As of Aug 3, 2024\n• Screening Dec 2, 2024": False,
        "As of Aug 3, 2024\n• Screening Dec 2": False,  # no year: read near its 2024 block
        "As of Aug 3, 2024\n• Premiere December 2024": False,
    }
    for old, kept in cases.items():
        llm = Llm()
        run(conn_one(NOTES + "\n" + old), llm, brief=replace(BRIEF, pm_notes_weeks=4))
        texts = [r["text"] for r in llm.payloads[0]["pm_notes"]]
        assert (old.splitlines()[1] in texts) is kept, old


def test_a_malformed_item_is_dropped_without_losing_its_valid_siblings():
    qc = lambda p: line_id(p, "QC on Nov 20")
    quote = "• QC on Nov 20, waiting for client notes"
    reply = reply_with(
        changes=lambda p: [{"text": None, "source": "pm_notes", "line_id": qc(p), "quote": quote},
                           {"text": "QC on Nov 20", "source": "pm_notes", "line_id": qc(p), "quote": quote}],
        concerns=lambda p: [{"kind": "open_item", "text": ["not", "text"], "source": "pm_notes"},
                            {"kind": "open_item", "text": "QC still waits for client notes", "source": "pm_notes",
                             "line_id": qc(p), "quote": quote}],
        last_update={"line_id": "not a number"},
    )
    gw, _ = run(conn_one(), Llm(reply))
    text = dm(gw)
    assert "• QC on Nov 20" in text and "QC still waits for client notes" in text
    assert "LLM unavailable" not in text and "LLM unavailable" not in digest(gw)


# ---- free-text privacy (#60) ----------------------------------------------------------------

LEAKY = ("As of Nov 9\n• Pio to send the files to ana, see notion.so/0123456789abcdef0123456789abcdef"
         "\n• Ping @ana_h or tg 123456789\n• Same colorist as Show TWO, TWO needs her first")


def leaky_db():
    one, two = page("p1", "ONE", notes=LEAKY), page("p2", "TWO", coordinator="c-ana")
    return make_db([one, two], [one, two], [one, two])


def test_names_ids_and_other_projects_in_free_text_never_reach_the_llm():
    llm = Llm()
    run(leaky_db(), llm, notion=minutes_notion("Pio confirmed with Ana, same as TWO"), meeting_notes_ds=MEETING)
    payload = next(p for p in llm.payloads if p["project"]["name"] == "Show ONE")
    blob = json.dumps(payload, ensure_ascii=False)
    for private in ("Pio", "ana", "Ana", "notion.so", "0123456789abcdef", "@ana_h", "ana_h", "123456789",
                    "Show TWO", "TWO", "c-pio", "c-ana", "p1", "p2"):
        assert private not in blob, private
    texts = [r["text"] for r in payload["pm_notes"]]
    pio, ana = ("[person 1]", "[person 2]") if texts[1].startswith("• [person 1]") else ("[person 2]", "[person 1]")
    assert texts[1] == f"• {pio} to send the files to {ana}, see [id 1]"
    assert texts[2] == "• Ping [id 2] or tg [id 3]"
    assert texts[3] in ("• Same colorist as [project 1], [project 2] needs her first",
                        "• Same colorist as [project 2], [project 1] needs her first")
    two = texts[3].split(", ")[1].split(" needs")[0]
    assert payload["minutes"]["lines"][0]["text"] == f"• {pio} confirmed with {ana}, same as {two}"  # one mapping
    assert payload["project"]["name"] == "Show ONE"  # the project's own name stays


def test_an_accepted_item_shows_the_real_names_and_an_invented_or_lowercase_name_is_dropped():
    def row(p):
        return next(r for r in p["pm_notes"] if "send the files" in r["text"])

    def placeholders(p):  # (Pio's, Ana's), as this payload numbered them
        text = row(p)["text"]
        return text[2:12], text.split("files to ")[1][:10]

    def reply(p):
        if p["project"]["name"] != "Show ONE":
            return {}
        pio, ana = placeholders(p)
        cite = {"source": "pm_notes", "line_id": row(p)["line_id"], "quote": row(p)["text"]}
        return {"concerns": [{"kind": "open_item", "text": f"{pio} still has to send the files to {ana}", **cite}],
                "changes": [{"text": "files go to ana", **cite}, {"text": "files sent by pio", **cite},
                            {"text": "files to send", **cite}]}
    gw, _ = run(leaky_db(), Llm(reply))
    text = dm(gw)
    assert "Pio still has to send the files to ana" in text
    assert "• files to send" in text
    assert "files go to ana" not in text and "files sent by pio" not in text


def test_a_first_name_that_is_also_a_word_is_hidden_only_when_capitalised():
    from fake_notion import contact as page_of
    people = contacts() + [page_of("c-will", "Will Santos")]
    notes = "As of Nov 9\n• Will sends the grade, we will wait"
    one = page(notes=notes)
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    for kind, at in (("monday", MONDAY), ("friday", FRIDAY)):
        run_snapshot(conn, FakeNotion([one], people), PROJECTS, CONTACTS, kind, at)
    cite = lambda p: {"source": "pm_notes", "line_id": line_id(p, "grade"), "quote": "• [person 1] sends the grade, we will wait"}
    llm = Llm(lambda p: {"changes": [{"text": "team will wait for the grade", **cite(p)}]})
    gw, _ = run(conn, llm, notion=FakeNotion([], people))
    assert llm.payloads[0]["pm_notes"][1]["text"] == "• [person 1] sends the grade, we will wait"
    assert "• team will wait for the grade" in dm(gw)


# ---- grounding of fluent prose (#62 item 3) ----------------------------------------------------
# Contract: every date with a written year must be a date a source line writes (so the year cannot be
# recombined from another date), and every word of an item must be a word of the sources (or one of
# their inflections), a month name, a number, or a small set of plain connecting words.

QC_QUOTE = "• QC on Nov 20, waiting for client notes"


def _concern(text, p):
    return {"kind": "open_item", "text": text, "source": "pm_notes",
            "line_id": line_id(p, "waiting for client notes"), "quote": QC_QUOTE}


def test_an_invented_lowercase_name_drops_only_that_item():
    concerns = lambda p: [_concern("QC on Nov 20 waiting for client notes", p),
                          _concern("QC waiting for zyxmarco", p)]
    gw, _ = run(conn_one(), Llm(reply_with(concerns=concerns)))
    text = dm(gw)
    assert "QC on Nov 20 waiting for client notes" in text and "zyxmarco" not in text


def test_inflections_and_plain_connecting_words_are_still_grounded():
    changes = lambda p: [{"text": "QC is still waiting on the client notes", "source": "pm_notes",
                          "line_id": line_id(p, "waiting for client notes"), "quote": QC_QUOTE}]
    gw, _ = run(conn_one(), Llm(reply_with(changes=changes)))
    assert "QC is still waiting on the client notes" in dm(gw)


def test_a_year_recombined_from_another_date_is_dropped():
    notes = "As of Nov 9\n• QC on 2026-12-01, festival in Q2 2027"
    quote = "• QC on 2026-12-01, festival in Q2 2027"
    changes = lambda p: [
        {"text": "QC on 2026-12-01", "source": "pm_notes", "line_id": line_id(p, "festival"), "quote": quote},
        {"text": "QC on 2027-12-01", "source": "pm_notes", "line_id": line_id(p, "festival"), "quote": quote},
        {"text": "Festival in Q2 2027", "source": "pm_notes", "line_id": line_id(p, "festival"), "quote": quote},
        {"text": "Festival in Q2 2026", "source": "pm_notes", "line_id": line_id(p, "festival"), "quote": quote},
        {"text": "QC on Dec 1, 2027", "source": "pm_notes", "line_id": line_id(p, "festival"), "quote": quote},
    ]
    gw, _ = run(conn_one(notes), Llm(reply_with(changes=changes)))
    text = dm(gw)
    assert "• QC on 2026-12-01" in text and "• Festival in Q2 2027" in text
    for dropped in ("2027-12-01", "Q2 2026", "Dec 1, 2027"):
        assert dropped not in text


def test_a_name_that_starts_like_a_month_is_still_an_invented_name():
    concerns = lambda p: [_concern("QC waiting for marco", p), _concern("QC waiting for janice", p)]
    gw, _ = run(conn_one(), Llm(reply_with(concerns=concerns)))
    assert "marco" not in dm(gw) and "janice" not in dm(gw)


# ---- redaction contract: identity and id variants (#62 item 4) --------------------------------
# Exposure boundary (recommended, pending Cyril's approval): a name or project code that is not an
# ordinary word is hidden in any case; one that is an ordinary word (will, two) only as written
# capitalised or in capitals. Every known TelegramUserID is hidden wherever it stands as a number.

def variants_db(notes, extra_people=()):
    from fake_notion import contact as page_of
    people = contacts() + [page_of("c-marco", "Marco Reyes"), *extra_people]
    done = proj("p3", "AB", "✅ Done", coordinator="c-ana")
    one = page(notes=notes)
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    for kind, at in (("monday", MONDAY), ("friday", FRIDAY)):
        run_snapshot(conn, FakeNotion([one, done], people), PROJECTS, CONTACTS, kind, at)
    return conn, people


def outbound(notes, **kw):
    conn, people = variants_db(notes)
    llm = Llm()
    run(conn, llm, notion=FakeNotion([], people), **kw)
    return json.dumps(llm.payloads[0], ensure_ascii=False)


def test_name_and_code_variants_never_reach_the_llm():
    blob = outbound("As of Nov 9\n• marco will ask Reyes, and MARCO again\n• AB is wrapped, ab too")
    for private in ("marco", "Marco", "MARCO", "Reyes", "reyes", "AB", "ab too"):
        assert private not in blob, private
    assert "will ask" in blob  # ordinary words stay


def test_punctuated_ids_telegram_links_and_known_telegram_ids_never_reach_the_llm():
    notes = ("As of Nov 9\n• Telegram 123456789.\n• Link tg://user?id=123456789.\n"
             "• Pio is user 111 and Ana is 222, call 1234567.89 later")
    blob = outbound(notes)
    for private in ("123456789", "tg://", "111", "222"):
        assert private not in blob, private
    assert "1234567.89" in blob  # a decimal is not an id
    assert "Telegram [id" in blob


def test_an_ordinary_word_name_or_code_is_hidden_only_as_written_capitalised():
    from fake_notion import contact as page_of
    conn, people = variants_db("As of Nov 9\n• Will plans it, we will wait, Sunday two, then TWO", [page_of("c-will", "Will Tan")])
    llm = Llm()
    run(conn, llm, notion=FakeNotion([], people))
    texts = [r["text"] for r in llm.payloads[0]["pm_notes"]]
    assert texts[1].startswith("• [person 1] plans it, we will wait")
    assert "Tan" not in json.dumps(llm.payloads[0])
