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
    assert "brief master brief readers unavailable error=ApiUnavailable" in logs


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
