"""``concern scan`` before the brief and "Raise at PSR" from the open, unsnoozed Concerns (#120, #138)."""
import re
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fake_api import FakeConcerns, down, refused
from fake_notion import FakeNotion, relation
from t6_notion_reports.brief_job import run_brief
from t6_notion_reports.briefing import RAISE_UNAVAILABLE_NOTE
from t6_notion_reports.config import load_config
from test_flags import lines_of, staffed
from test_gated_briefs import BRIEF, IN_PROGRESS, SATURDAY, Gateway, contacts, digest, dm, make_db, proj

T6 = {"c-oto": "t6p_oto", "c-ida": "t6p_ida", "p1": "t6_proj_one", "p2": "t6_proj_two"}


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
    """An open Concern that was last heard of on ``evidence`` (an ISO date)."""
    store.items.append({"id": f"t6c_{text[:6]}", "project": project, "key": " ".join(text.split()).casefold(), "text": text, "source": "pm_notes_scan",
                        "opened_on": opened, "state": "open", "closed_on": None, "owner": owner, "waiting_on": waiting_on,
                        "evidence_at": datetime.fromisoformat(evidence).replace(tzinfo=ZoneInfo("Asia/Manila")).timestamp()})


def raise_lines(text):
    ls = lines_of(text)
    if "Raise at PSR" not in ls:
        return []
    out = []
    for ln in ls[ls.index("Raise at PSR") + 1:]:
        if not ln.startswith("•"):
            break
        out.append(ln)
    return out


# ---- A: the scan ----

def test_one_scan_for_each_gated_project_with_its_coordinator_as_owner_and_the_t6_id_as_reference():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS), proj("p2", "TWO", IN_PROGRESS, coordinator="c-ida"),
                         proj("p3", "OUT", "✅ Done"))
    _, logs = go(conn, notion)
    scans = notion.api.concerns.scans
    assert sorted((s.project, s.owner) for s in scans) == [("t6_proj_one", "t6p_oto"), ("t6_proj_two", "t6p_ida")]
    assert "brief scan ok=2 skipped=0 failed=0" in logs


def test_a_project_without_a_t6_id_is_scanned_by_its_page_id():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS), bind=False)
    notion.t6_ids = {"c-oto": "t6p_oto"}
    go(conn, notion)
    assert [s.project for s in notion.api.concerns.scans] == ["p1"]


@pytest.mark.parametrize("coordinators, n", [((), 0), (("c-oto", "c-ida"), 2)])
def test_a_project_with_no_coordinator_or_two_is_skipped_and_logged_without_names(coordinators, n):
    page = proj("p1", "ONE", IN_PROGRESS)
    page["properties"]["T6 Post Coordinator"] = relation(*coordinators)
    conn, notion = world(page)
    _, logs = go(conn, notion)
    assert notion.api.concerns.scans == []
    assert f"brief scan skipped reason=coordinators n={n}" in logs and "brief scan ok=0 skipped=1 failed=0" in logs


def test_a_coordinator_the_api_does_not_know_as_a_person_is_skipped():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    notion.t6_ids = {"p1": "t6_proj_one"}  # no T6 Person ID for c-oto
    _, logs = go(conn, notion)
    assert notion.api.concerns.scans == [] and "brief scan skipped reason=owner" in logs


@pytest.mark.parametrize("outcome", [refused("owner"), down()])
def test_a_refused_or_timed_out_scan_never_stops_the_brief_and_logs_the_rule_only(outcome):
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS), proj("p2", "TWO", IN_PROGRESS, coordinator="c-ida"))
    notion.api.fail["concern scan"] = outcome
    gw, logs = go(conn, notion)
    assert dm(gw) and digest(gw)  # the brief was built and sent
    assert "brief scan ok=0 skipped=0 failed=2" in logs
    assert any(log.startswith("brief scan failed error=concern scan ") for log in logs)
    assert not any("ONE" in log or "t6p_" in log for log in logs if log.startswith("brief scan"))


def test_the_scan_runs_before_the_findings_are_handed_over_and_the_list_is_read_after():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    go(conn, notion)
    assert [c for c in notion.api.calls if c in ("concern scan", "concern observe", "concern list")] == [
        "concern scan", "concern observe", "concern list", "concern list"]  # the Raise at PSR list, then the open list for its text


def test_each_call_gets_its_own_limit_and_the_shared_client_keeps_its_own():
    from t6.client import Client
    from t6_notion_reports.concern_api import LIST_TIMEOUT, OBSERVE_TIMEOUT, SCAN_TIMEOUT, limited
    client = Client("http://api.invalid", "x", timeout=120)
    assert (SCAN_TIMEOUT, OBSERVE_TIMEOUT, LIST_TIMEOUT) == (60, 30, 30)
    assert limited(client, SCAN_TIMEOUT).timeout == 60 and client.timeout == 120


def test_a_dry_run_makes_no_scan_call_but_still_reads():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    run_brief(conn, notion, BRIEF, SATURDAY, dry_print=True, out=lambda _: None, log=lambda _: None)
    assert notion.api.concerns.scans == [] and "concern scan" not in notion.api.calls
    assert "concern observe" in notion.api.calls


def test_concern_scan_false_turns_the_step_off():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    _, logs = go(conn, notion, replace(BRIEF, concern_scan=False))
    assert notion.api.concerns.scans == [] and not any(log.startswith("brief scan") for log in logs)


def test_projects_not_reached_when_the_budget_is_used_up_are_skipped_and_counted():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS), proj("p2", "TWO", IN_PROGRESS, coordinator="c-ida"))
    ticks = iter(range(0, 10_000, 400))  # each look at the clock is 400 s later
    _, logs = go(conn, notion, monotonic=lambda: next(ticks))
    assert len(notion.api.concerns.scans) == 1
    assert "brief scan ok=1 skipped=1 failed=0" in logs


def test_the_scan_key_is_read_from_config_and_defaults_to_true(tmp_path):
    from test_config import BRIEF_YAML, ENV, write
    assert load_config(write(tmp_path, BRIEF_YAML), ENV).brief.concern_scan is True
    assert load_config(write(tmp_path, BRIEF_YAML + "\nconcern_scan: false\n"), ENV).brief.concern_scan is False


# ---- B: Raise at PSR ----

def test_an_open_concern_is_in_the_digest_and_its_owners_dm_with_the_evidence_date_the_owner_and_the_party():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    stale(notion.api.concerns, waiting_on="the vendor")
    gw, _ = go(conn, notion)
    expected = "• ONE: Vendor quote still missing · last evidence Nov 3 · owner Oto · waiting on the vendor"
    assert raise_lines(digest(gw)) == [expected]
    assert raise_lines(dm(gw)) == [expected]
    assert "no news" not in digest(gw) and "no news" not in dm(gw)


def test_the_dm_of_another_coordinator_does_not_list_it_but_the_digest_does():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS), proj("p2", "TWO", IN_PROGRESS, coordinator="c-ida"))
    stale(notion.api.concerns, owner="t6p_oto")
    gw, _ = go(conn, notion)
    assert raise_lines(dm(gw, 222)) == [] and len(raise_lines(dm(gw, 111))) == 1 and len(raise_lines(digest(gw))) == 1


def test_an_old_concern_with_recent_evidence_is_listed_and_still_one_of_its_projects_lines():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    stale(notion.api.concerns, opened="2026-10-03", evidence="2026-11-12")
    gw, _ = go(conn, notion)
    assert len(raise_lines(digest(gw))) == 1  # age never hides it, however recent the evidence
    assert "Vendor quote still missing · since Oct 3 (6 wks)" in dm(gw)


def test_the_section_lists_the_oldest_evidence_first_as_the_api_orders_it():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    stale(notion.api.concerns, text="Newer news", evidence="2026-11-10")
    stale(notion.api.concerns, text="Older news", evidence="2026-10-01")
    gw, _ = go(conn, notion)
    assert [ln.split(" · ")[0] for ln in raise_lines(digest(gw))] == ["• ONE: Older news", "• ONE: Newer news"]


def test_a_snoozed_concern_is_in_no_line_of_the_brief_until_it_is_woken():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    stale(notion.api.concerns)
    notion.api.concerns.snooze("Vendor quote")
    gw, _ = go(conn, notion)
    assert all("Vendor quote" not in t and "Raise at PSR" not in t for *_, t in gw.sent)
    notion.api.concerns.items[0]["state"] = "open"  # a person wakes it
    gw, _ = go(conn, notion)
    assert len(raise_lines(digest(gw))) == 1 and "Vendor quote" in dm(gw)


def test_a_closed_concern_is_not_listed():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    stale(notion.api.concerns)
    notion.api.concerns.close("Vendor quote", "2026-11-10")
    gw, _ = go(conn, notion)
    assert "Raise at PSR" not in digest(gw)


def test_no_concerns_means_no_heading():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    gw, _ = go(conn, notion)
    assert all("Raise at PSR" not in t for *_, t in gw.sent)


def test_a_concern_of_a_project_outside_the_gate_is_left_out():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS), proj("p2", "OUT", "✅ Done"))
    stale(notion.api.concerns, project="p2")
    gw, _ = go(conn, notion)
    assert all("Raise at PSR" not in t for *_, t in gw.sent)


@pytest.mark.parametrize("outcome", [down(), refused("concern")])
def test_when_the_list_cannot_be_read_the_section_is_left_out_and_the_digest_says_so(outcome):
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    stale(notion.api.concerns)
    notion.api.fail["concern list"] = outcome
    gw, logs = go(conn, notion)
    assert "Raise at PSR" not in dm(gw) and RAISE_UNAVAILABLE_NOTE in digest(gw)
    assert RAISE_UNAVAILABLE_NOTE == "⚠️ Raise at PSR unavailable" and dm(gw)
    assert any(log.startswith("brief raise at psr unavailable error=concern list") for log in logs)


def test_a_dry_run_prints_the_section():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS))
    stale(notion.api.concerns)
    out = []
    run_brief(conn, notion, BRIEF, SATURDAY, dry_print=True, out=out.append, log=lambda _: None)
    assert "Raise at PSR" in "\n".join(out) and "Vendor quote still missing" in "\n".join(out)


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


@pytest.mark.parametrize("verb", ["concern scan", "concern list", "concern observe"])
@pytest.mark.parametrize("error", [ValueError("secret text"), KeyError("counts"), Weird("secret text")])
def test_an_unexpected_error_in_a_concern_call_never_stops_the_brief(verb, error):
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS, **staffed(Colorist=relation())))

    def boom(inputs):
        raise error
    setattr(notion.api, verb.replace(" ", "_"), boom)
    gw, logs = go(conn, notion)
    assert dm(gw) and digest(gw)
    assert not any("secret text" in ln for ln in logs)
    assert any(f"error={type(error).__name__}" in ln for ln in logs if "unavailable" in ln or "scan failed" in ln)


@pytest.mark.parametrize("reply", [{}, {"projects": [], "counts": {}}, {"counts": {"new": 0, "known": 0, "not_seen": 0, "skipped": 0}}])
def test_an_observe_reply_without_its_parts_is_history_unavailable(reply):
    from t6.verbs import Done
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS, **staffed(Colorist=relation())))
    notion.api.concern_observe = lambda inputs: Done(result=reply)
    gw, logs = go(conn, notion)
    assert "Concern history unavailable" in digest(gw) and "KeyError" in " ".join(logs)
