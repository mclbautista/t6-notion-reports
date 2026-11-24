""""Open Concerns" and "Closing after PSR" in the Wednesday brief (#120, #138, #147); the brief runs no scan of its own."""
import re
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fake_api import FakeConcerns, down, refused
from fake_notion import FakeNotion, relation
from t6_notion_reports.brief_job import run_brief
from t6_notion_reports.briefing import RAISE_UNAVAILABLE_NOTE
from test_flags import lines_of, staffed
from test_gated_briefs import BRIEF, IN_PROGRESS, SATURDAY, Gateway, contacts, digest, dm, make_db, proj

T6 = {"c-oto": "t6p_oto", "c-ida": "t6p_ida", "p1": "t6_proj_one", "p2": "t6_proj_two"}
COORDINATORS = replace(BRIEF, concern_dm="coordinators")  # each Coordinator brief also lists its coordinator's Concerns


def world(*pages, store=None, bind=True):
    conn = make_db(list(pages), list(pages))
    notion = FakeNotion.live(conn, contacts(), concerns=store or FakeConcerns())
    if bind:
        notion.t6_ids = dict(T6)
    return conn, notion


def go(conn, notion, brief=BRIEF, now=SATURDAY, **kw):
    gw, logs = Gateway(), []
    run_brief(conn, notion, brief, now, gateway=gw, log=logs.append, **kw)
    return gw, logs


def stale(store, project="p1", text="Vendor quote still missing", owner="t6p_oto", waiting_on=None, opened="2026-10-20", evidence="2026-11-03"):
    """An open Concern that was last heard of on ``evidence`` (an ISO date), open since ``opened`` as of the brief."""
    store.items.append({"id": f"t6c_{text[:6]}", "project": project, "key": " ".join(text.split()).casefold(), "text": text, "source": "pm_notes_scan",
                        "opened_on": opened, "state": "open", "closed_on": None, "owner": owner, "waiting_on": waiting_on,
                        "evidence_at": datetime.fromisoformat(evidence).replace(tzinfo=ZoneInfo("Asia/Manila")).timestamp(),
                        "age_seconds": (SATURDAY - datetime.fromisoformat(opened).replace(tzinfo=ZoneInfo("Asia/Manila"))).total_seconds()})


def raise_lines(text, title="Open Concerns"):
    ls = lines_of(text)
    if title not in ls:
        return []
    out = []
    for ln in ls[ls.index(title) + 1:]:
        if not ln.startswith("•"):
            break
        out.append(ln)
    return out


# ---- A: no scan of its own (#147): the API scans at 21:00 ----

def test_the_brief_makes_no_scan_call_and_reads_the_list_after_the_findings_are_handed_over():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS), proj("p2", "TWO", IN_PROGRESS, coordinator="c-ida"))
    _, logs = go(conn, notion)
    assert notion.api.concerns.scans == [] and not any(log.startswith("brief scan") for log in logs)
    assert [c for c in notion.api.calls if c in ("concern scan", "concern observe", "concern list")] == [
        "concern observe", "concern list", "concern list"]  # the Open Concerns list, then the open list for its text


def test_each_call_gets_its_own_limit_and_the_shared_client_keeps_its_own():
    from t6.client import Client
    from t6_notion_reports.concern_api import LIST_TIMEOUT, OBSERVE_TIMEOUT, limited
    client = Client("http://api.invalid", "x", timeout=120)
    assert (OBSERVE_TIMEOUT, LIST_TIMEOUT) == (30, 30)
    assert limited(client, LIST_TIMEOUT).timeout == 30 and client.timeout == 120


def test_a_dry_run_still_reads():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    run_brief(conn, notion, BRIEF, SATURDAY, dry_print=True, out=lambda _: None, log=lambda _: None)
    assert "concern scan" not in notion.api.calls and "concern observe" in notion.api.calls


# ---- B: Open Concerns ----

EXPECTED = "• ONE: Vendor quote still missing · last evidence Nov 3 · owner Oto · waiting on the vendor · open 25 days"


SHEET = "https://sheets.example.invalid/d/SHEET_ID"
SHEET_LINE = f'<a href="{SHEET}">Concerns sheet</a>'


def test_the_master_brief_links_the_concerns_sheet_when_the_setting_is_set_and_no_coordinator_brief_does():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    stale(notion.api.concerns)
    gw, _ = go(conn, notion, replace(COORDINATORS, concerns_sheet_url=SHEET))
    assert digest(gw).count(SHEET_LINE) == 1 and digest(gw).index("Vendor quote") < digest(gw).index(SHEET_LINE)
    assert SHEET not in dm(gw)


def test_with_no_concerns_the_master_brief_has_no_sheet_line_even_when_the_setting_is_set():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    gw, _ = go(conn, notion, replace(COORDINATORS, concerns_sheet_url=SHEET))
    assert "Concerns sheet" not in digest(gw)


def test_without_the_setting_the_master_brief_has_no_sheet_line():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    stale(notion.api.concerns)
    gw, _ = go(conn, notion)
    assert "Concerns sheet" not in digest(gw)


def test_an_open_concern_is_in_the_master_brief_with_the_evidence_date_the_owner_the_party_and_its_age():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    stale(notion.api.concerns, waiting_on="the vendor")
    gw, _ = go(conn, notion)
    assert raise_lines(digest(gw)) == [EXPECTED]
    assert "Concerns" not in dm(gw) and "Vendor quote" not in dm(gw)  # concern_dm "master": no Concerns in Coordinator briefs


def test_with_concern_dm_coordinators_the_owners_coordinator_brief_lists_it_too():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    stale(notion.api.concerns, waiting_on="the vendor")
    gw, _ = go(conn, notion, COORDINATORS)
    assert raise_lines(digest(gw)) == raise_lines(dm(gw)) == [EXPECTED]


def test_the_dm_of_another_coordinator_does_not_list_it_but_the_digest_does():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS), proj("p2", "TWO", IN_PROGRESS, coordinator="c-ida"))
    stale(notion.api.concerns, owner="t6p_oto")
    gw, _ = go(conn, notion, COORDINATORS)
    assert raise_lines(dm(gw, 222)) == [] and len(raise_lines(dm(gw, 111))) == 1 and len(raise_lines(digest(gw))) == 1


def test_an_old_concern_with_recent_evidence_is_listed_once_and_not_a_line_of_its_project():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    stale(notion.api.concerns, opened="2026-10-03", evidence="2026-11-12")
    gw, _ = go(conn, notion)
    assert len(raise_lines(digest(gw))) == 1  # age never hides it, however recent the evidence
    assert digest(gw).count("Vendor quote still missing") == 1 and "since Oct 3" not in digest(gw)


def test_the_section_is_sorted_by_project_then_oldest_first():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS), proj("p2", "TWO", IN_PROGRESS, coordinator="c-ida"))
    stale(notion.api.concerns, project="p2", text="Two old", owner="t6p_ida", opened="2026-09-01", evidence="2026-09-02")
    stale(notion.api.concerns, text="Newer news", opened="2026-11-01", evidence="2026-10-01")
    stale(notion.api.concerns, text="Older news", opened="2026-10-01", evidence="2026-11-10")
    gw, _ = go(conn, notion)
    assert [ln.split(" · ")[0] for ln in raise_lines(digest(gw))] == ["• ONE: Older news", "• ONE: Newer news", "• TWO: Two old"]


def closed_at(day):
    return datetime.fromisoformat(day).replace(hour=15, tzinfo=ZoneInfo("Asia/Manila")).timestamp()


def pending_world(**close):
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    stale(notion.api.concerns, text="Still open")
    stale(notion.api.concerns, text="Vendor quote still missing", waiting_on="the vendor")
    notion.api.concerns.pending_list = {"t6c_Vendor": {"close_at": closed_at("2026-11-26"), **close}}
    return conn, notion


def test_a_pending_close_is_listed_only_under_closing_after_psr_with_who_and_when():
    conn, notion = pending_world(close_by_name="Ida", close_source="close")
    gw, _ = go(conn, notion)
    closing = EXPECTED + " · closing (by Ida, Nov 26)"
    assert raise_lines(digest(gw), "Closing after PSR") == [closing]
    assert [ln.split(" · ")[0] for ln in raise_lines(digest(gw))] == ["• ONE: Still open"]
    assert digest(gw).count("Vendor quote still missing") == 1
    assert "Closing after PSR" not in dm(gw)
    gw, _ = go(conn, notion, COORDINATORS)
    assert raise_lines(dm(gw), "Closing after PSR") == [closing]


def test_a_pending_close_is_not_in_the_open_list_so_its_text_is_the_pending_items_own():
    from t6.verbs import ConcernListInput
    conn, notion = pending_world(close_by_name="Ida")
    assert "t6c_Vendor" not in [c["id"] for c in notion.api.concern_list(ConcernListInput(open=True)).result["concerns"]]
    gw, _ = go(conn, notion)
    assert len(raise_lines(digest(gw), "Closing after PSR")) == 1


def test_a_done_line_close_says_done_line_when_it_has_no_name():
    conn, notion = pending_world(close_by_name=None, close_source="done_line")
    gw, _ = go(conn, notion)
    assert raise_lines(digest(gw), "Closing after PSR") == [EXPECTED + " · closing (by Done line, Nov 26)"]


def test_without_a_pending_list_from_the_api_there_is_no_closing_section():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    stale(notion.api.concerns)
    assert notion.api.concerns.pending_list is None
    gw, _ = go(conn, notion)
    assert len(raise_lines(digest(gw))) == 1 and "Closing after PSR" not in digest(gw)


def test_a_snoozed_concern_is_in_no_line_of_the_brief_until_it_is_woken():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    stale(notion.api.concerns)
    notion.api.concerns.snooze("Vendor quote")
    gw, _ = go(conn, notion)
    assert all("Vendor quote" not in t and "Open Concerns" not in t for *_, t in gw.sent)
    notion.api.concerns.items[0]["state"] = "open"  # a person wakes it
    gw, _ = go(conn, notion, COORDINATORS)
    assert len(raise_lines(digest(gw))) == 1 and "Vendor quote" in dm(gw)


def test_a_closed_concern_is_not_listed():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    stale(notion.api.concerns)
    notion.api.concerns.close("Vendor quote", "2026-11-10")
    gw, _ = go(conn, notion)
    assert "Open Concerns" not in digest(gw)


def test_no_concerns_means_no_heading():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    gw, _ = go(conn, notion)
    assert all("Open Concerns" not in t for *_, t in gw.sent)


def test_a_concern_of_a_project_outside_the_gate_is_left_out():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS), proj("p2", "OUT", "✅ Done"))
    stale(notion.api.concerns, project="p2")
    gw, _ = go(conn, notion)
    assert all("Open Concerns" not in t for *_, t in gw.sent)


@pytest.mark.parametrize("outcome", [down(), refused("concern")])
def test_when_the_list_cannot_be_read_the_section_is_left_out_and_the_digest_says_so(outcome):
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    stale(notion.api.concerns)
    notion.api.fail["concern list"] = outcome
    gw, logs = go(conn, notion)
    assert "Open Concerns" not in dm(gw) and RAISE_UNAVAILABLE_NOTE in digest(gw)
    assert RAISE_UNAVAILABLE_NOTE == "⚠️ Open Concerns unavailable" and dm(gw)
    assert any(log.startswith("brief raise at psr unavailable error=concern list") for log in logs)


def test_a_dry_run_prints_the_section():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    stale(notion.api.concerns)
    out = []
    run_brief(conn, notion, BRIEF, SATURDAY, dry_print=True, out=out.append, log=lambda _: None)
    assert "Open Concerns" in "\n".join(out) and "Vendor quote still missing" in "\n".join(out)


def test_a_long_section_splits_between_lines_and_every_part_is_valid_html():
    from html_check import assert_valid_html
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    for n in range(80):
        stale(notion.api.concerns, text=f"Item {n:02d} " + "words " * 25)
        notion.api.concerns.items[-1]["id"] = f"t6c_{n:02d}"
    gw, _ = go(conn, notion)
    parts = [t for to, _, _, t in gw.sent if to == 999]
    assert len(parts) > 1
    for text in parts:
        assert_valid_html(text) and len(text) <= 4096
    body = "\n".join(re.sub(r"<[^>]+>", "", t) for t in parts)
    assert all(f"Item {n:02d} " in body for n in range(80))


# ---- a malformed answer never stops the brief ----

class Weird(Exception):
    pass


@pytest.mark.parametrize("verb", ["concern list", "concern observe"])
@pytest.mark.parametrize("error", [ValueError("secret text"), KeyError("counts"), Weird("secret text")])
def test_an_unexpected_error_in_a_concern_call_never_stops_the_brief(verb, error):
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS, **staffed(Colorist=relation())))

    def boom(inputs):
        raise error
    setattr(notion.api, verb.replace(" ", "_"), boom)
    gw, logs = go(conn, notion)
    assert dm(gw) and digest(gw)
    assert not any("secret text" in ln for ln in logs)
    assert any(f"error={type(error).__name__}" in ln for ln in logs if "unavailable" in ln)


@pytest.mark.parametrize("reply", [{}, {"projects": [], "counts": {}}, {"counts": {"new": 0, "known": 0, "not_seen": 0, "skipped": 0}}])
def test_an_observe_reply_without_its_parts_is_history_unavailable(reply):
    from t6.verbs import Done
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS, **staffed(Colorist=relation())))
    notion.api.concern_observe = lambda inputs: Done(result=reply)
    gw, logs = go(conn, notion)
    assert "Concern history unavailable" in digest(gw) and "KeyError" in " ".join(logs)
