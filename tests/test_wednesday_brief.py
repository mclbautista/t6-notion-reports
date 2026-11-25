"""Wednesday brief delivery (#147): the Master brief to the Persons with ``master_brief`` on, one Coordinator brief per
coordinator to the channel or by DM with the same text."""
from dataclasses import replace

import pytest

from fake_api import down
from fake_notion import FakeNotion
from t6_notion_reports.brief_job import BriefFailed, run_brief
from t6_notion_reports.briefing import Settings, build_briefing
from t6_notion_reports.contacts import Contact
from t6_notion_reports.diff import FieldChange, Person, ProjectChange
from test_gated_briefs import BRIEF, IN_PROGRESS, SATURDAY, Gateway, changed, contacts, make_db

CHANNEL = replace(BRIEF, coordinator_destination="channel", coordinator_chat_id=-1000000000002, coordinator_topic_id=3)


def world(masters=("c-owner",)):
    people = contacts()
    for page in people:
        page["master_brief"] = page["id"] in masters
    conn = make_db(*changed([("p1", "ONE", IN_PROGRESS, {}), ("p2", "TWO", IN_PROGRESS, {"coordinator": "c-ida"})]))
    return conn, FakeNotion.live(conn, people)


def send(brief=BRIEF, masters=("c-owner",)):
    conn, notion = world(masters)
    gw = Gateway()
    run_brief(conn, notion, brief, SATURDAY, gateway=gw, log=lambda _: None)
    return gw


def test_the_master_brief_goes_only_to_the_persons_with_master_brief_on():
    gw = send(masters=("c-owner", "c-ida"))
    masters = {(to, key) for to, _, key, text in gw.sent if text.startswith("<b>Master brief · Sat Nov 14</b>")}
    assert masters == {(999, "master-brief-2026-11-14-c-owner"), (222, "master-brief-2026-11-14-c-ida")}
    assert not any(to == 111 and text.startswith("<b>Master brief") for to, _, _, text in gw.sent)


def test_a_contact_without_master_brief_gets_no_master_brief_and_none_at_all_fails_the_run_after_the_coordinator_briefs():
    conn, notion = world(masters=())
    gw = Gateway()
    with pytest.raises(BriefFailed, match="Master brief"):
        run_brief(conn, notion, BRIEF, SATURDAY, gateway=gw, log=lambda _: None)
    assert {to for to, *_ in gw.sent} == {111, 222}
    assert all(text.startswith("<b>Coordinator brief · ") for *_, text in gw.sent)


def test_master_brief_readers_that_cannot_be_read_fail_the_run_and_the_log_names_the_error_type_only():
    conn, notion = world()
    notion.api.person_get = lambda inputs: down()
    gw, logs = Gateway(), []
    with pytest.raises(BriefFailed):
        run_brief(conn, notion, BRIEF, SATURDAY, gateway=gw, log=logs.append)
    assert "brief master brief reader unavailable error=ApiUnavailable" in logs


def test_private_notes_reach_only_the_owners_master_brief():
    owner, kit = Contact("c-owner", "Owner", telegram_id=999), Contact("c-kit", "Kit", telegram_id=777)
    change = ProjectChange("p1", "ONE", "changed", [FieldChange("Stage", "Offline", "Online")], [Person("c-owner", "Owner")],
                           code="ONE", title="Show ONE", group="in_progress")
    msgs = build_briefing([change], {"c-owner": "Owner"}, {"c-owner": owner, "c-kit": kit},
                          Settings(owner, -1003, 7, master_recipients=(owner, kit)), SATURDAY.date(), cyril_notes=["private note"])
    masters = {m.to: m.text for m in msgs if m.key.startswith("master-brief")}
    assert "private note" in masters[999] and "private note" not in masters[777]


def test_a_coordinator_brief_is_byte_identical_in_channel_and_dm_modes():
    by_dm = {key: (to, topic, text) for to, topic, key, text in send(BRIEF).sent if key.startswith("notion-reports-")}
    by_channel = {key: (to, topic, text) for to, topic, key, text in send(CHANNEL).sent if key.startswith("notion-reports-")}
    assert by_dm.keys() == by_channel.keys() == {"notion-reports-2026-11-14-c-oto", "notion-reports-2026-11-14-c-ida"}
    for key in by_dm:
        assert by_dm[key][2] == by_channel[key][2]
        assert by_channel[key][:2] == (-1000000000002, 3)
    assert by_dm["notion-reports-2026-11-14-c-oto"][:2] == (111, None)
    assert by_dm["notion-reports-2026-11-14-c-oto"][2].startswith("<b>Coordinator brief · Oto · Sat Nov 14</b>")


def test_the_channel_gets_a_coordinator_brief_even_for_a_coordinator_without_a_telegram_id():
    conn, _ = world()
    people = contacts()
    people[1]["properties"]["TelegramUserID"] = {"type": "number", "number": None}  # Ida
    people[2]["master_brief"] = True
    gw = Gateway()
    run_brief(conn, FakeNotion.live(conn, people), CHANNEL, SATURDAY, gateway=gw, log=lambda _: None)
    ida = [(to, topic, text) for to, topic, key, text in gw.sent if key == "notion-reports-2026-11-14-c-ida"]
    assert ida and ida[0][:2] == (-1000000000002, 3) and not ida[0][2].startswith("@")
    assert "has no Telegram id" not in "\n".join(text for to, *_, text in gw.sent if to == 999)


def _master_texts(gw):
    return {to for to, _, _, text in gw.sent if text.startswith("<b>Master brief")}


def test_one_failed_person_get_drops_only_that_person_and_logs_the_error_type_only():
    conn, notion = world(masters=("c-owner", "c-ida"))
    real = notion.api.person_get
    notion.api.person_get = lambda inputs: down() if inputs.person_id == "c-ida" else real(inputs)
    gw, logs = Gateway(), []
    run_brief(conn, notion, BRIEF, SATURDAY, gateway=gw, log=logs.append)
    assert _master_texts(gw) == {999}
    assert {111, 222} <= {to for to, *_ in gw.sent}  # the Coordinator briefs still go
    assert "brief master brief reader unavailable error=ApiUnavailable" in logs
    assert not any("c-ida" in line for line in logs)


def test_master_brief_in_person_list_is_used_and_person_get_is_not_called():
    conn, notion = world(masters=("c-owner", "c-ida"))
    listed = notion.api.person_list
    def person_list(inputs):
        out = listed(inputs)
        for p in out.result["people"]:
            p["master_brief"] = p["page_id"] == "c-ida"  # the list says Ida only: it wins
        return out
    notion.api.person_list = person_list
    notion.api.person_get = lambda inputs: pytest.fail("person get must not be called")
    gw = Gateway()
    run_brief(conn, notion, BRIEF, SATURDAY, gateway=gw, log=lambda _: None)
    assert _master_texts(gw) == {222}


def test_the_owner_is_recognised_whatever_the_id_spelling():
    owner = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    people = contacts()
    people[2]["id"] = owner
    conn = make_db(*changed([("p1", "ONE", IN_PROGRESS, {})]))
    notion = FakeNotion.live(conn, people)
    gw = Gateway()
    run_brief(conn, notion, replace(BRIEF, cyril_contact_id=owner.replace("-", "")), SATURDAY, gateway=gw, log=lambda _: None)
    assert not any("set cyril_contact_id" in text for *_, text in gw.sent)


def _two_weeks_db():
    """Tuesday snapshot, Monday one, next Tuesday one: a Thursday change sits between the two Tuesdays only."""
    from fake_notion import FakeNotion as FN
    from t6_notion_reports.snapshot_job import run_snapshot
    from t6_notion_reports.snapshots import SCHEMA
    from test_gated_briefs import FRIDAY, MONDAY, proj
    import sqlite3
    from datetime import timedelta
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    run_snapshot(conn, FN([proj("p1", "ONE", stage="Offline")], contacts()), "friday", FRIDAY - timedelta(days=7))
    run_snapshot(conn, FN([proj("p1", "ONE", stage="Online")], contacts()), "monday", MONDAY)  # changed Thursday
    run_snapshot(conn, FN([proj("p1", "ONE", stage="Online")], contacts()), "friday", FRIDAY)
    return conn


def test_the_change_diff_runs_from_the_previous_tuesday_snapshot_not_the_monday_one():
    conn = _two_weeks_db()
    gw = Gateway()
    run_brief(conn, FakeNotion.live(conn, contacts()), BRIEF, SATURDAY, gateway=gw, log=lambda _: None)
    assert "Stage: Offline → Online" in "\n".join(text for to, _, _, text in gw.sent if to == 111)


def test_a_diff_pair_whose_new_snapshot_is_over_two_days_old_is_refused_with_a_note():
    conn, notion = world()
    gw, logs = Gateway(), []
    late = SATURDAY.replace(day=16)  # the snapshot is Fri 23:30 Manila: more than 2 days before
    run_brief(conn, notion, BRIEF, late, gateway=gw, log=logs.append)
    text = "\n".join(t for *_, t in gw.sent)
    assert "Stage" not in text and "Offline" not in text
    assert "snapshot is too old" in text
    assert any(line.startswith("brief snapshot stale") for line in logs)


def test_a_fresh_snapshot_is_not_refused():
    gw = send()
    assert "snapshot is too old" not in "\n".join(t for *_, t in gw.sent)


@pytest.mark.parametrize("brief", [BRIEF, CHANNEL])
def test_weekly_drive_counts_and_each_overdue_drive_stay_under_their_project_and_coordinator(brief):
    from test_gated_briefs import proj

    rows = [proj("p1", "ONE"), proj("p2", "TWO", coordinator="c-ida"), proj("p3", "THREE")]
    conn = make_db(rows, rows)
    notion = FakeNotion.live(conn, contacts())
    notion.t6_ids = {"p1": "t6_proj_one", "p2": "t6_proj_two"}
    notion.api.drive_weeks["2026-11-09"] = [
        {"project": {"page_id": "t6_proj_two", "text": None}, "out_count": 1, "in_count": 2,
         "overdue": [{"drive_id": "drive-b", "label": "Box Beta", "serial": None, "person": "Receiver B",
                      "released_on": "2026-10-29", "days_out": 16}]},
        {"project": {"page_id": "t6_proj_one", "text": None}, "out_count": 3, "in_count": 1,
         "overdue": [{"drive_id": "drive-a", "label": "Box Alpha", "serial": None, "person": "Receiver A",
                      "released_on": "2026-10-25", "days_out": 20},
                     {"drive_id": "drive-c", "label": None, "serial": "SYNTH-C", "person": None,
                      "released_on": "2026-10-30", "days_out": 15}]},
        {"project": {"page_id": "p3", "text": None}, "out_count": 2, "in_count": 0, "overdue": []},
    ]
    gw = Gateway()
    run_brief(conn, notion, brief, SATURDAY, gateway=gw, log=lambda _: None)
    by_key = {key: text for _, _, key, text in gw.sent}
    oto = by_key["notion-reports-2026-11-14-c-oto"]
    ida = by_key["notion-reports-2026-11-14-c-ida"]
    master = by_key["master-brief-2026-11-14-c-owner"]
    for text in (oto, master[master.index("<b>Oto</b>"):]):
        one, three = text.split("<b>THREE · Show THREE</b>")
        assert "<b>ONE · Show ONE</b>" in one and "Drives: 3 OUT, 1 IN" in one
        assert "Drive Box Alpha: out 20 days" in one and "Drive SYNTH-C: out 15 days" in one
        assert "Drives: 2 OUT, 0 IN" in three
        assert "Box Beta" not in text and "1 OUT, 2 IN" not in text
    for text in (ida, master[:master.index("<b>Oto</b>")]):
        assert "<b>TWO · Show TWO</b>" in text and "Drives: 1 OUT, 2 IN" in text
        assert "Drive Box Beta: out 16 days" in text
        assert "Box Alpha" not in text and "SYNTH-C" not in text and "3 OUT, 1 IN" not in text


def test_projects_without_movements_or_overdue_drives_add_nothing_to_either_brief():
    from test_gated_briefs import STANDBY, proj

    quiet = proj("p2", "QUIET", STANDBY, coordinator="c-ida")
    conn = make_db([proj("p1", "ONE"), quiet], [proj("p1", "ONE", stage="Online"), quiet])
    notion = FakeNotion.live(conn, contacts())
    baseline = run_brief(conn, notion, BRIEF, SATURDAY, dry_print=True, out=lambda _: None, log=lambda _: None)
    notion.api.drive_weeks["2026-11-09"] = [
        {"project": {"page_id": p, "text": None}, "out_count": 0, "in_count": 0, "overdue": []}
        for p in ("p1", "p2")
    ]
    result = run_brief(conn, notion, BRIEF, SATURDAY, dry_print=True, out=lambda _: None, log=lambda _: None)
    assert result.messages == baseline.messages
    assert all("Show QUIET" not in m.text and "Drives:" not in m.text for m in result.messages)


@pytest.mark.parametrize("out_count, overdue", [
    (3, []),
    (0, [{"drive_id": "drive-old", "label": "Box <Delta>", "serial": None, "person": None,
          "released_on": "2026-10-30", "days_out": 15}]),
])
def test_drive_activity_alone_lists_an_unchanged_project_even_when_the_llm_finds_no_changes(out_count, overdue):
    from test_gated_briefs import Llm, STANDBY, proj

    rows = [proj("p1", "ONE", STANDBY)]  # no flags, Concerns or snapshot changes
    conn = make_db(rows, rows)
    notion = FakeNotion.live(conn, contacts())
    notion.api.drive_weeks["2026-11-09"] = [
        {"project": {"page_id": "p1", "text": None}, "out_count": out_count, "in_count": 0, "overdue": overdue},
    ]
    gw = Gateway()
    run_brief(conn, notion, BRIEF, SATURDAY, gateway=gw, llm=Llm(), log=lambda _: None)
    assert {to for to, *_ in gw.sent} == {111, 999}
    for *_, text in gw.sent:
        assert "<b>ONE · Show ONE</b>" in text and f"Drives: {out_count} OUT, 0 IN" in text
        assert ("Drive Box &lt;Delta&gt;: out 15 days" in text) is bool(overdue)
        assert "No changes" not in text


@pytest.mark.parametrize("now, expected", [
    (SATURDAY.replace(day=15, hour=15, minute=59), "Drives: 1 OUT, 0 IN"),  # Sunday Manila
    (SATURDAY.replace(day=15, hour=16, minute=1), "Drives: 2 OUT, 0 IN"),  # Monday Manila, still Sunday UTC
])
def test_drive_counts_use_the_monday_sunday_week_in_manila(now, expected):
    from test_gated_briefs import STANDBY, proj

    rows = [proj("p1", "ONE", STANDBY)]
    conn = make_db(rows, rows)
    notion = FakeNotion.live(conn, contacts())
    notion.api.drive_weeks = {
        "2026-11-09": [{"project": {"page_id": "p1", "text": None}, "out_count": 1, "in_count": 0, "overdue": []}],
        "2026-11-16": [{"project": {"page_id": "p1", "text": None}, "out_count": 2, "in_count": 0, "overdue": []}],
    }
    result = run_brief(conn, notion, BRIEF, now, dry_print=True, out=lambda _: None, log=lambda _: None)
    assert {m.to for m in result.messages} == {111, 999}
    assert all(expected in m.text for m in result.messages)
