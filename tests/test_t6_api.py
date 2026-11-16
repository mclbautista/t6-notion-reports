"""Every read goes through the t6 API (#108). The fake API serves the fixtures' own data in the verbs' shape."""
import sqlite3
from datetime import date, datetime, timezone

import pytest

import test_mom
from fake_api import FakeApi
from fake_notion import (JOB_ORDERS, MEETING, FakeNotion, contact, formula, job_order, meeting_page, project,
                         relation, rich, select)
from fake_notion import date as notion_date
from test_mom_send import CHAT, NOW, TOPIC, FakeGateway, full_page, setup
from t6_notion_reports.holidays import leave_lines
from t6_notion_reports.jo_snapshot_job import run_jo_snapshot
from t6_notion_reports.jo_snapshots import connect as jo_connect
from t6_notion_reports.mom_job import run_draft_mom
from t6_notion_reports.mom_lines import load_minutes
from t6_notion_reports.mom_send import build_messages, run_send_ready_mom
from t6_notion_reports.shift_job import NotionUnavailable
from t6_notion_reports.snapshot_job import run_snapshot
from t6_notion_reports.snapshots import SCHEMA
from t6_notion_reports.t6_api import ApiNotion, ApiUnavailable, client_from_env


def via_api(notion, down=False):
    return ApiNotion(FakeApi(notion, down=down), notion)


def people():
    quil = contact("c-quil", "Quilla")
    quil["properties"].update({"TelegramHandle": rich("@quil"), "TelegramUserID": rich("100000001"),
                               "Aliases": rich("Quil, Q")})
    oto = contact("c-oto", "Oto")
    oto["properties"]["TelegramUserID"] = rich("100000002")
    return [quil, oto]


def test_contacts_carry_handle_telegram_id_and_aliases():
    got = FakeNotion(contacts=people()).read_contacts()
    quil = got["c-quil"]
    assert (quil.name, quil.handle, quil.telegram_id, quil.aliases) == ("Quilla", "quil", 100000001, "Quil, Q")
    assert got["c-oto"].handle is None


def test_no_api_url_is_no_client():
    assert client_from_env({}) is None
    assert client_from_env({"T6_API_URL": "http://api:8081", "T6_API_SECRET": "x"}).timeout > 10


def test_notices_runs_keep_the_bold_flag():
    desc = [{"plain_text": "Upcoming Holidays and Leaves\n", "annotations": {"bold": True}},
            {"plain_text": "Quilla Nov 20\nBig note to the team\n", "annotations": {"bold": False}}]
    notices = FakeNotion(description=desc).notices()
    assert notices.runs[0].bold and not notices.runs[1].bold
    assert leave_lines(notices.runs) == ["Quilla Nov 20", "Big note to the team"]
    assert notices.description.startswith("Upcoming Holidays and Leaves")


# ---- Projects ----

def project_record(page):
    n = FakeNotion([page], people())
    return {r.page_id: r.normalized() for r in n.project_records()}[page["id"]]


def test_a_date_range_keeps_its_end():
    rec = project_record(project("p1", Playdate=notion_date("2026-12-01", "2026-12-03"),
                                 **{"Submission Deadline": notion_date("2026-12-05T10:00:00.000+08:00", "2026-12-06T10:00:00.000+08:00")}))
    assert rec["Playdate"] == {"start": "2026-12-01", "end": "2026-12-03"}
    assert rec["Submission Deadline"] == {"start": "2026-12-05T10:00:00.000+08:00", "end": "2026-12-06T10:00:00.000+08:00"}
    assert rec["Archival Deadline"] is None


def test_the_project_code_is_the_one_the_notion_property_shows():
    assert project_record(project("p1", code="QAZ"))["Project Code"] == "QAZ"


def test_relations_use_page_ids_and_names_whether_or_not_the_api_binds_them():
    n = FakeNotion([project("p1", **{"T6 Post Coordinator": relation("c-quil", "c-oto")})], people())
    n.t6_ids = {"c-quil": "per_q1"}  # one person is bound: the API gives a T6 ID for it
    rec = n.project_records()[0].normalized()
    assert rec["T6 Post Coordinator"] == [{"id": "c-oto", "name": "Oto"}, {"id": "c-quil", "name": "Quilla"}]


# ---- Job Orders ----

def jo_record(**props):
    n = FakeNotion([project("pr1", code="AAA"), project("pr2", name="Show Two", code=None)], people(),
                   job_orders=[job_order("j1", **props)])
    return n.job_order_records()[0].normalized()


def test_every_related_project_is_kept_with_its_code_or_name():
    rec = jo_record(Projects=relation("pr1", "pr2"))
    assert rec["Projects"] == [{"id": "pr1", "name": "AAA"}, {"id": "pr2", "name": "Show Two"}]


def test_blank_health_is_kept_apart_from_missing_health():
    assert jo_record(Health=formula(""))["Health"] == ""
    assert jo_record(Health=formula(None))["Health"] is None


def test_a_date_with_an_offset_is_the_same_instant_in_manila():
    rec = jo_record(Date=notion_date("2026-11-03T01:00:00.000+00:00"), Deadline=notion_date("2026-11-03"))
    assert rec["Date"] == "2026-11-03T09:00:00+08:00" and rec["Deadline"] == "2026-11-03T23:59:00+08:00"


def test_assigned_people_are_page_ids_when_the_api_binds_them():
    n = FakeNotion([], people(), job_orders=[job_order("j1", Assigned=relation("c-quil"))])
    n.t6_ids = {"c-quil": "per_q1"}
    assert n.job_order_records()[0].normalized()["Assigned"] == [{"id": "c-quil", "name": "Quilla"}]


# ---- Meeting Notes ----

def test_a_note_carries_its_times_attendees_and_blocks():
    n = setup(attendees=[("u1", "Quilla")], sent_at="2026-11-16T15:00:00+08:00")
    n.meetings[0]["last_edited_time"] = "2026-11-16T07:00:00.000Z"
    (note,) = via_api(n).meeting_notes(title_prefix="PSR MOM")
    assert (note.title, note.meeting_day, note.last_edited) == ("PSR MOM 2026-11-16", date(2026, 11, 16), "2026-11-16T07:00:00.000Z")
    assert note.attendees == (("u1", "Quilla"),) and note.sent_at == "2026-11-16T15:00:00+08:00"
    assert note.body[0].kind == "heading_1" and note.body[4].children[1].text == "VBM & Co will <haggle>"


def test_build_messages_group_the_body_and_keep_the_unchanged_list_out():
    (note,) = via_api(setup()).meeting_notes(ready_to_send=True)
    texts = build_messages(note.body)
    assert len(texts) == 4 and not any("Unchanged" in t for t in texts)


def test_minutes_lines_are_the_bullets_under_each_project():
    n = setup()
    minutes = load_minutes(via_api(n), date(2026, 11, 20))
    assert minutes.lines_for("CR") == ("• Delay in Mixing", "• VBM & Co will <haggle>")


def draft(n):
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    run_snapshot(conn, FakeNotion(test_mom.pages(False), []), "friday", test_mom.FRIDAY)
    run_snapshot(conn, FakeNotion(test_mom.pages(True), []), "monday", test_mom.MONDAY)
    return conn


# ---- an unavailable API is an outage the jobs handle ----

@pytest.mark.parametrize("call", [
    lambda a: a.read_contacts(),
    lambda a: a.notices(),
    lambda a: load_minutes(a, date(2026, 11, 20)),
    lambda a: run_send_ready_mom(a, FakeGateway(), CHAT, TOPIC, NOW, log=lambda _: None),
    lambda a: run_snapshot(sqlite3.connect(":memory:"), a, "manual", NOW),
    lambda a: run_jo_snapshot(jo_connect(), a, "manual", NOW),
])
def test_unavailable_api_raises_an_oserror_without_data(call):
    n = setup()
    n.down = True
    with pytest.raises(OSError) as exc:
        call(via_api(n))
    assert isinstance(exc.value, ApiUnavailable)


def test_send_ready_mom_marks_nothing_when_unavailable():
    n = setup()
    n.down = True
    with pytest.raises(ApiUnavailable):
        run_send_ready_mom(n, FakeGateway(), CHAT, TOPIC, NOW, log=lambda _: None)
    assert not n.api.writes


def test_draft_mom_unavailable_creates_nothing():
    n = FakeNotion(test_mom.pages(True), test_mom.contacts())
    conn = draft(n)
    n.down = True
    with pytest.raises(ApiUnavailable):
        run_draft_mom(conn, n, MEETING, test_mom.D, log=lambda _: None)
    assert not n.api.writes


def test_brief_unavailable_api_is_an_oserror():
    from test_brief_job import BRIEF, SUNDAY
    from t6_notion_reports.brief_job import run_brief
    n = FakeNotion([], test_mom.contacts())
    conn = draft(n)
    n.down = True
    with pytest.raises(ApiUnavailable):
        run_brief(conn, n, BRIEF, SUNDAY, log=lambda _: None)


def test_shift_report_treats_an_unavailable_api_as_the_outage_it_retries():
    from test_shift_outage import Clock, PM, go, snapshot_count
    from test_shift_report import world
    from test_shift_send import FakeTransport
    clock = Clock(PM)
    n = world()
    n.down = True
    conn = jo_connect()
    err, logs = go(conn, n, clock, FakeTransport())
    assert isinstance(err, NotionUnavailable) and snapshot_count(conn) == 0


def test_prod_coor_supe_people_are_stored_as_page_ids_when_the_api_binds_them():
    n = FakeNotion([project("p1", **{"Prod Coor/Supe": relation("c-quil", "c-oto")})], people())
    n.t6_ids = {"c-quil": "per_q1"}
    rec = n.project_records()[0].normalized()
    assert rec["Prod Coor/Supe"] == [{"id": "c-oto", "name": "Oto"}, {"id": "c-quil", "name": "Quilla"}]


def test_find_people_asks_at_most_50_names_a_call_and_keeps_the_order():
    n = FakeNotion(contacts=people())
    names = ["Quilla"] + [f"Nobody{i}" for i in range(54)] + ["Oto"]
    found = n.find_people(names)
    assert [len(c[0]) for c in n.api.find_calls] == [50, 6] and all(c[1] for c in n.api.find_calls)
    assert [f.name if f else None for f in found][:1] == ["Quilla"] and found[-1].name == "Oto" and len(found) == 56
