"""The status and flag results come from the API (#109): the app applies no status, date, role or JO rule.

The fake API client (``fake_api``) stands in for the API's rules; these tests check what the app does with its results.
"""
import re
import subprocess
from datetime import date as day, datetime, timedelta, timezone
from pathlib import Path

import pytest

from fake_api import FakeApi, project_rules
from fake_notion import FakeNotion, contact, date, project, relation, select
from t6_notion_reports.brief_job import apply_gate, gate_rules, read_flags
from t6_notion_reports.status_gate import gate
from test_flags import body, lines_of, one, staffed
from test_gated_briefs import (
    AIRING, ARCHIVING, BRIEF, LEADS, SATURDAY, STANDBY, TVC, Gateway, brief, changed, contacts, digest, dm, make_db, proj,
)

IN_PROGRESS_PLAIN = "In-Progress"  # no emoji
ELSEWHERE = "🔥 In-Progress"  # a different emoji

STATUSES = {
    "🚨 In-Progress": "in_progress", "🏎. In-Progress TVC": "in_progress", "Airing": "in_progress",
    "🏋️ Standby": "on_hold", "📦 Archiving": "on_hold",
    "🌱 Leads": "leads", "👀 Lead": "leads", "🔏 Negotiations": "leads", "🙋 Proposal Made": "leads",
    "📦 Archived": None, "🧭 Lost": None, IN_PROGRESS_PLAIN: "in_progress", ELSEWHERE: "in_progress",
    "Somewhere else": None,
}


@pytest.mark.parametrize("status,section", STATUSES.items())
def test_the_gate_is_the_group_the_api_returns_with_incoming_as_leads(status, section):
    notion = FakeNotion([project("p1", "Show", "QAZ", Status=select(status))], [])
    rules = notion.project_rules()
    assert gate(rules["p1"]) == section
    kept, _ = apply_gate({"p1": {"Status": status}}, {"p1": {"Status": status}}, rules, lambda _: None)
    assert ("p1" in kept) == (section is not None)


def test_the_status_key_of_archiving_is_compared_by_the_word():
    from t6_notion_reports.status_gate import is_archiving
    rules = FakeNotion([project("a", Status=select("📦 Archiving")), project("b", Status=select("🏋️ Standby"))], []).project_rules()
    assert is_archiving(rules["a"]) and not is_archiving(rules["b"])


def test_a_project_the_list_no_longer_holds_is_judged_by_one_project_get_and_dropped_when_not_found():
    notion = FakeNotion([project("here", Status=select("Airing"))], [])
    rules = gate_rules(notion, {"here": {}, "gone": {}}, {"here": {}})
    assert set(rules) == {"here"}
    assert notion.api.calls.count("project get") == 1
    assert notion.api.calls.count("project list") == 1


def test_a_removed_project_that_the_api_still_knows_is_judged_by_its_result():
    notion = FakeNotion([project("old", Status=select("📦 Archived"))], [])
    rules = gate_rules(notion, {"old": {"Status": "🚨 In-Progress"}}, {})
    assert "old" in rules and gate(rules["old"]) is None  # Archived: outside the gate


# ---- the brief ----

def test_a_lead_negotiation_and_proposal_made_now_reach_the_leads_section_with_incoming_flags():
    rows = [(f"p{n}", code, status, {"Playdate": date("2026-11-10")})
            for n, (code, status) in enumerate((("LEA", "👀 Lead"), ("NEG", "🔏 Negotiations"), ("PRO", "🙋 Proposal Made")), 1)]
    text = digest(brief(make_db(*changed(rows))))
    assert "<b>Leads</b>" in text
    for code in ("LEA", "NEG", "PRO"):
        assert f"Show {code}" in text
    assert text.count("Playdate overdue by 4 days") == 3


def test_a_status_without_its_emoji_or_with_another_one_is_in_the_brief():
    text = dm(brief(make_db(*changed([("p1", "PLAIN", IN_PROGRESS_PLAIN, {}), ("p2", "OTHER", ELSEWHERE, {})]))))
    assert "Show PLAIN" in text and "Show OTHER" in text and "<b>In-Progress</b>" in text


def test_a_lead_playdate_under_the_warning_window_is_flagged_from_the_api_flag():
    text = digest(brief(make_db(*changed([("p1", "LEA", "🔏 Negotiations", {"Playdate": date("2026-11-24")})]))))
    assert "Playdate in 10 days: check with the client" in text
    assert "Playdate today" not in digest(brief(make_db(*changed([("p1", "LEA", LEADS, {"Playdate": date("2026-11-24")})])), llm=None))


def test_one_project_flags_call_for_each_gated_project_with_the_stated_dates_and_the_window():
    rows = [("p1", "ONE", "🚨 In-Progress", {}), ("p2", "TWO", "Airing", {}), ("p3", "OUT", "✅ Done", {})]
    conn = make_db(*changed(rows))
    notion = FakeNotion.live(conn, contacts())
    from t6_notion_reports.brief_job import run_brief
    run_brief(conn, notion, BRIEF, SATURDAY, dry_print=True, out=lambda _: None, log=lambda _: None)
    calls = notion.api.flag_calls
    assert sorted(c.project for c in calls) == ["p1", "p2"]
    assert all(c.lead_warning_days == BRIEF.lead_warning_days and c.as_of == day(2026, 11, 14) for c in calls)


def test_the_date_the_notes_state_for_a_blank_property_is_sent_as_stated():
    from test_dates_from_notes import SUBMIT_QUOTE, brief_for, submission
    from test_consolidation import Llm, reply_with, run
    conn = brief_for(Playdate=date("2026-12-20"))
    notion = FakeNotion.live(conn, contacts())
    run(conn, Llm(reply_with(dates_from_notes=submission())), notion=notion)
    assert [c.stated for c in notion.api.flag_calls] == [{"submission_deadline": day(2026, 12, 1)}]


def test_the_missing_roles_line_is_the_roles_the_api_names_in_report_order():
    text = one("🚨 In-Progress", **staffed(Colorist=relation(), Director=relation()))
    assert "⚠️ Missing roles: Colorist, Director" in text
    assert "Missing roles" not in one("🏋️ Standby", **staffed(Colorist=relation()))


def test_the_health_flag_of_the_api_is_not_used_the_blank_project_health_still_decides():
    # The API's flag and ``health_missing`` read the property "Health", not "Project Health" (an API gap).
    assert one("🚨 In-Progress", **staffed(), health=None).count("missing health update") == 1
    assert "missing health update" not in one("🚨 In-Progress", **staffed(), health="ON TRACK")


def test_a_flags_call_the_api_does_not_answer_gives_no_flags_and_does_not_stop_the_brief():
    conn = make_db(*changed([("p1", "ONE", "🚨 In-Progress", {})]))
    notion = FakeNotion.live(conn, [])
    api = notion.api
    api.project_flags = lambda inputs: __import__("fake_api").down()
    logs = []
    flagged, failed = read_flags(notion, {"p1": {"Project Health": "ON TRACK"}}, notion.project_rules(), day(2026, 11, 14), 30, {}, logs.append)
    assert failed == {"p1"} and not flagged["p1"]
    assert logs == ["brief flags unavailable error=ApiUnavailable"]


# ---- snapshots and the Minutes ----

def test_archived_and_lost_are_not_snapshotted_because_the_api_says_so():
    from t6_notion_reports.snapshot_job import run_snapshot
    from t6_notion_reports.snapshots import connect, load_snapshot
    conn = connect()
    pages = [project("a", Status=select("📦 Archived")), project("l", Status=select("🧭 Lost")), project("k", Status=select("Airing"))]
    res = run_snapshot(conn, FakeNotion(pages, []), "manual", SATURDAY)
    assert set(load_snapshot(conn, res.snapshot_id)) == {"k"}


@pytest.mark.parametrize("status", ["🌱 Leads", "👀 Lead", "🔏 Negotiations", "🙋 Proposal Made"])
def test_the_minutes_count_an_incoming_status_as_incoming(status):
    from t6_notion_reports.mom import group_of
    notion = FakeNotion([project("p", Status=select(status))], [])
    assert group_of({"Stage": "Offline", "Project Health": "Critical"}, notion.project_rules()["p"]) == "incoming"


# ---- shift reports ----

def test_the_shift_report_lists_the_job_orders_the_api_says_are_active_and_shows_its_health_flags():
    from test_shift_report import MID, jo, run, world
    from t6_notion_reports.jo_snapshots import connect
    fake = world(jo("j1", st="Pending", health="🔴 Blocked"), jo("j2", st="Done"), jo("j3", st="Pending", health="🔵 On Track"))
    original = fake.api.jo_list

    def listed(inputs):
        out = original(inputs)
        for order in out.result["job_orders"]:
            if order["page_id"] == "j3":
                order["active"] = False  # the API says so, whatever the Status
        return out

    fake.api.jo_list = listed
    report, text, _ = run(connect(), fake, "1130", MID)
    assert report.unfinished == 1
    assert "🔴" in text


def test_reports_import_only_the_client_and_the_verb_types_from_t6():
    root = Path(__file__).resolve().parent.parent
    found = subprocess.run(["grep", "-rhE", "--include=*.py", r"^\s*(from t6|import t6)\b", str(root / "src"), str(root / "tests")],
                           capture_output=True, text=True).stdout.split("\n")
    allowed = ("from t6.client import", "from t6.verbs import", "import t6_notion_reports", "from t6_notion_reports")
    assert [line.strip() for line in found if line.strip() and not line.strip().startswith(allowed)] == []
