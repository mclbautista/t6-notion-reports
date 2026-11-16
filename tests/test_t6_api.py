"""Reads through the t6 API give what the Notion reads gave (#108). The fake API serves the fixtures' own data."""
import sqlite3
from datetime import date, datetime, timezone

import pytest

import test_mom
from fake_api import FakeApi
from fake_notion import (CONTACTS, JOB_ORDERS, MEETING, PROJECTS, FakeNotion, contact, job_order, meeting_page,
                         project, relation, rich, rt, select)
from test_mom_send import CHAT, NOW, TOPIC, FakeGateway, full_page, setup
from t6_notion_reports.contacts import load_contacts
from t6_notion_reports.holidays import leave_lines
from t6_notion_reports.jo_snapshot_job import parse_job_order, run_jo_snapshot
from t6_notion_reports.jo_snapshots import connect as jo_connect, load_snapshot as load_jo
from t6_notion_reports.mom_job import run_draft_mom
from t6_notion_reports.mom_lines import load_minutes
from t6_notion_reports.mom_send import build_messages, run_send_ready_mom
from t6_notion_reports.shift_job import NotionUnavailable
from t6_notion_reports.snapshot_job import parse_project, run_snapshot
from t6_notion_reports.snapshots import SCHEMA, load_snapshot
from t6_notion_reports.t6_api import ApiNotion, ApiUnavailable, client_from_env


def via_api(notion, down=False, **kw):
    return ApiNotion(notion, FakeApi(notion, down=down), PROJECTS, MEETING, **kw)


def people():
    quil = contact("c-quil", "Quilla")
    quil["properties"].update({"TelegramHandle": rich("@quil"), "TelegramUserID": rich("100000001"),
                               "Aliases": rich("Quil, Q")})
    oto = contact("c-oto", "Oto")
    oto["properties"]["TelegramUserID"] = rich("100000002")
    return [quil, oto, contact("c-none", "")]


def test_contacts_are_the_same():
    n = FakeNotion(contacts=people())
    assert load_contacts(via_api(n), CONTACTS) == load_contacts(n, CONTACTS)


def test_no_api_url_keeps_notion():
    assert client_from_env({}) is None
    assert client_from_env({"T6_API_URL": "http://api:8081", "T6_API_SECRET": "x"}).timeout > 10


def test_notices_runs_keep_the_bold_flag():
    desc = [{"plain_text": "Upcoming Holidays and Leaves\n", "annotations": {"bold": True}},
            {"plain_text": "Quilla Nov 20\nBig note to the team\n", "annotations": {"bold": False}}]
    n = FakeNotion(description=desc)
    api = via_api(n)
    assert leave_lines(api.retrieve_data_source(PROJECTS)["description"]) == leave_lines(desc) == ["Quilla Nov 20", "Big note to the team"]


def test_send_ready_mom_messages_and_sent_at_are_the_same():
    old, new = setup(), setup()
    gw_old, gw_new = FakeGateway(), FakeGateway()
    run_send_ready_mom(old, MEETING, gw_old, CHAT, TOPIC, NOW, log=lambda _: None)
    api = via_api(new)
    run_send_ready_mom(api, MEETING, gw_new, CHAT, TOPIC, NOW, log=lambda _: None)
    assert gw_new.sent == gw_old.sent and gw_new.sent
    assert new.sent_marked == old.sent_marked and new.sent_marked  # the write went to Notion


def test_build_messages_keep_rich_text_and_nesting():
    n = setup()
    api = via_api(n)
    api.query_data_source(MEETING, {"property": "Name", "title": {"starts_with": "PSR MOM"}})  # the bodies arrive with the notes
    assert build_messages(api, "m1") == build_messages(n, "m1")


def test_minutes_lines_are_the_same():
    n = FakeNotion(meetings=[meeting_page("m1", "PSR MOM 2026-11-16", "2026-11-16", sent_at="2026-11-16T15:00:00+08:00")])
    n.page_blocks["m1"] = full_page()
    n.meetings[0]["last_edited_time"] = "2026-11-16T07:00:00.000Z"
    a, b = load_minutes(n, MEETING, date(2026, 11, 20)), load_minutes(via_api(n), MEETING, date(2026, 11, 20))
    assert a == b and a.by_label


def draft(n):
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    run_snapshot(conn, FakeNotion(test_mom.pages(False), []), PROJECTS, CONTACTS, "friday", test_mom.FRIDAY)
    run_snapshot(conn, FakeNotion(test_mom.pages(True), []), PROJECTS, CONTACTS, "monday", test_mom.MONDAY)
    return conn


def test_draft_mom_creates_the_same_page():
    def run(wrap):
        meetings = [meeting_page("prev", "PSR MOM 2026-11-09", "2026-11-09", attendees=[("u1", "Quilla")])]
        n = FakeNotion(test_mom.pages(True), test_mom.contacts(), meetings,
                       "Upcoming Holidays and Leaves\nOto Nov 17\nNov 18 holiday")
        reader = via_api(n) if wrap else n
        conn = draft(n)
        run_draft_mom(conn, reader, PROJECTS, CONTACTS, MEETING, test_mom.D, log=lambda _: None)
        return n.created, n.trashed
    assert run(True) == run(False)


def test_snapshot_names_come_from_the_api():
    pages = [project("p1", "Show", "QAZ", **{"T6 Post Coordinator": relation("c-quil")})]
    results = []
    for wrap in (False, True):
        n = FakeNotion(pages, people())
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        r = run_snapshot(conn, via_api(n) if wrap else n, PROJECTS, CONTACTS, "manual", datetime(2026, 11, 13, tzinfo=timezone.utc))
        results.append(load_snapshot(conn, r.snapshot_id))
    assert results[0] == results[1]
    assert results[0]["p1"]["T6 Post Coordinator"] == [{"id": "c-quil", "name": "Quilla"}]


def test_jo_snapshot_names_come_from_the_api():
    jo = job_order("j1", Assigned=relation("c-quil"), Projects=relation("pr1"))
    out = []
    for wrap in (False, True):
        n = FakeNotion([project("pr1", "Show", "QAZ")], people(), job_orders=[jo])
        conn = jo_connect()
        r = run_jo_snapshot(conn, via_api(n) if wrap else n, JOB_ORDERS, CONTACTS, PROJECTS, "manual",
                            datetime(2026, 11, 3, tzinfo=timezone.utc))
        out.append(load_jo(conn, r.snapshot_id).pages)
    assert out[0] == out[1]


def test_project_and_jo_records_from_the_api_match_when_nothing_is_lost():
    """What ``compare-api`` builds: the verbs' own data gives the same records as the Notion pages."""
    pages = [project("p1", "Show", "QAZ", **{"T6 Post Coordinator": relation("c-quil"), "PM Notes": rich("")})]
    jos = [job_order("j1", Assigned=relation("c-quil"), Projects=relation("p1"))]
    n = FakeNotion(pages, people(), job_orders=jos)
    api = via_api(n, snapshots=True)
    names = {c["id"]: c["properties"]["Name"]["title"][0]["plain_text"] if c["properties"]["Name"]["title"] else None for c in n.contacts}
    old = [parse_project(p, names).normalized() for p in pages]
    assert [r.normalized() for r in api.project_records()] == old
    labels = {"p1": "QAZ"}
    assert [r.normalized() for r in api.job_order_records()] == [parse_job_order(j, names, labels).normalized() for j in jos]


def test_a_second_related_project_is_a_gap_the_records_show():
    jos = [job_order("j1", Projects=relation("p1", "p2"))]
    n = FakeNotion([project("p1"), project("p2")], people(), job_orders=jos)
    got = via_api(n, snapshots=True).job_order_records()[0].normalized()["Projects"]
    assert len(got) == 1  # t6 0.6.5+ `jo list` carries the first Project only


# ---- an unavailable API is a Notion outage ----

@pytest.mark.parametrize("call", [
    lambda a: load_contacts(a, CONTACTS),
    lambda a: a.retrieve_data_source(PROJECTS),
    lambda a: load_minutes(a, MEETING, date(2026, 11, 20)),
    lambda a: run_send_ready_mom(a, MEETING, FakeGateway(), CHAT, TOPIC, NOW, log=lambda _: None),
    lambda a: run_snapshot(sqlite3.connect(":memory:"), a, PROJECTS, CONTACTS, "manual", NOW),
    lambda a: run_jo_snapshot(jo_connect(), a, JOB_ORDERS, CONTACTS, PROJECTS, "manual", NOW),
])
def test_unavailable_api_raises_an_oserror_without_data(call):
    api = via_api(setup(), down=True)
    with pytest.raises(OSError) as exc:
        call(api)
    assert isinstance(exc.value, ApiUnavailable)


def test_send_ready_mom_marks_nothing_when_unavailable():
    n = setup()
    with pytest.raises(ApiUnavailable):
        run_send_ready_mom(via_api(n, down=True), MEETING, FakeGateway(), CHAT, TOPIC, NOW, log=lambda _: None)
    assert not n.sent_marked


def test_draft_mom_unavailable_creates_nothing():
    n = FakeNotion(test_mom.pages(True), test_mom.contacts())
    with pytest.raises(ApiUnavailable):
        run_draft_mom(draft(n), via_api(n, down=True), PROJECTS, CONTACTS, MEETING, test_mom.D, log=lambda _: None)
    assert not n.created


def test_shift_report_treats_an_unavailable_api_as_the_notion_outage():
    from test_shift_outage import Clock, PM, go, snapshot_count
    from test_shift_report import world
    from test_shift_send import FakeTransport
    clock = Clock(PM)
    n = world()
    conn = jo_connect()
    err, logs = go(conn, via_api(n, down=True), clock, FakeTransport())
    assert isinstance(err, NotionUnavailable) and snapshot_count(conn) == 0
    assert any("notion unavailable" in line for line in logs)


# ---- compare-api ----

def compare(tmp_path, pages, jos=()):
    from types import SimpleNamespace
    from t6_notion_reports.compare_api import run_compare
    from t6_notion_reports.config import BriefSettings
    brief = BriefSettings("http://gw:8780", -100123, None, "c-oto", False, None)
    cfg = SimpleNamespace(database_path=str(tmp_path / "absent.db"), projects_data_source=PROJECTS,
                          contacts_data_source=CONTACTS, jo_data_source=JOB_ORDERS,
                          meeting_notes_data_source=MEETING, brief=brief, matching=SimpleNamespace())
    from t6_notion_reports.people_match import MatchSettings
    cfg.matching = MatchSettings()
    n = FakeNotion(pages, people(), [], "", list(jos))
    n.page_blocks = {}
    return run_compare(cfg, n, via_api(n, snapshots=True), str(tmp_path / "out"),
                       datetime(2026, 11, 13, 15, tzinfo=timezone.utc))


def test_compare_reports_equal_and_counts_only(tmp_path, capsys):
    pages = [project("p1", "Show", "QAZ", **{"T6 Post Coordinator": relation("c-quil")})]
    results = compare(tmp_path, pages, [job_order("j1", Assigned=relation("c-quil"), Projects=relation("p1"))])
    assert [(r.name, r.differing) for r in results] == [("projects", 0), ("job_orders", 0), ("brief", 0)]
    assert all("Quilla" not in r.line() and "Show" not in r.line() for r in results)
    assert "Show" in (tmp_path / "out" / "projects.old.txt").read_text()  # the files hold names, the output does not


def test_compare_shows_a_date_end_the_api_cannot_carry(tmp_path):
    from fake_notion import date as notion_date
    pages = [project("p1", "Show", "QAZ", Playdate=notion_date("2026-12-01", "2026-12-03"))]
    by_name = {r.name: r.differing for r in compare(tmp_path, pages)}
    assert by_name["projects"] > 0
