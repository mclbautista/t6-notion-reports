"""Raw-Project lines go to the bots test Telegram topic (issue #157): code and rule kind only, never a name or word."""
import json
import sqlite3
from datetime import date

import pytest

from fake_llm_api import FakeLlmApi, ok, refused
from fake_notion import MEETING, FakeNotion, project, rich
from test_condense import FRI, MON, NEW_ALL, OLD_ALL, adapter
from t6_notion_reports.gateway import GatewayError
from t6_notion_reports.mom_job import rule_kind, run_draft_mom
from t6_notion_reports.snapshot_job import run_snapshot
from t6_notion_reports.snapshots import SCHEMA

DAY = date(2026, 10, 26)
CHAT, TOPIC = -100777, 42


class FakeGateway:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def notify(self, to, text, key, topic=None):
        if self.fail:
            raise GatewayError("down")
        self.sent.append((to, topic, key, text))
        return len(self.sent)


def snap(which):
    return [project(c.lower(), f"Secret Show {c}", c, **{"PM Notes": rich(t)}) for c, t in which.items()]


def answer(inputs):
    code = json.loads(inputs.input_text)["project"].split()[0]
    if code == "QAZ":
        return ok({"bullets": ["Dario confirmed 12 shooting days"]})  # Dario is not in the notes: grounding
    if code == "LSB":
        return refused("masking", "leak")
    return ok({"bullets": ["Online on Nov 9; Colorist: Pax"]})


def draft(gateway, destination=(CHAT, TOPIC), **kw):
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    run_snapshot(conn, FakeNotion(snap(OLD_ALL), []), "friday", FRI)
    run_snapshot(conn, FakeNotion(snap(NEW_ALL), []), "monday", MON)
    notion = FakeNotion(snap(NEW_ALL), [])
    logs = []
    result = run_draft_mom(conn, notion, MEETING, DAY, log=logs.append, llm=adapter(FakeLlmApi(answer)),
                           gateway=gateway, raw_destination=destination, **kw)
    return notion, result, logs


def test_one_line_per_raw_project_with_code_and_rule_kind_only():
    gw = FakeGateway()
    _, result, _ = draft(gw)
    assert result.page_id == "new-1"
    sent = {key: (to, topic, text) for to, topic, key, text in gw.sent}
    assert set(sent) == {"psr-raw-2026-10-26-QAZ", "psr-raw-2026-10-26-LSB"}
    assert all((to, topic) == (CHAT, TOPIC) for to, topic, _ in sent.values())
    assert "QAZ" in sent["psr-raw-2026-10-26-QAZ"][2] and "grounding word" in sent["psr-raw-2026-10-26-QAZ"][2]
    assert "LSB" in sent["psr-raw-2026-10-26-LSB"][2] and "masking leak" in sent["psr-raw-2026-10-26-LSB"][2]
    for _, _, text in sent.values():
        assert "Dario" not in text and "Secret Show" not in text


def test_lines_are_sent_after_the_page_exists():
    order = []
    gw = FakeGateway()
    gw.notify = lambda *a, **k: order.append("notify")
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    run_snapshot(conn, FakeNotion(snap(OLD_ALL), []), "friday", FRI)
    run_snapshot(conn, FakeNotion(snap(NEW_ALL), []), "monday", MON)
    notion = FakeNotion(snap(NEW_ALL), [])
    create = notion.api.meeting_notes_create
    notion.api.meeting_notes_create = lambda i: (order.append("create"), create(i))[1]
    run_draft_mom(conn, notion, MEETING, DAY, log=lambda _: None, llm=adapter(FakeLlmApi(answer)),
                  gateway=gw, raw_destination=(CHAT, TOPIC))
    assert order[0] == "create" and order.count("notify") == 2


def test_a_gateway_failure_is_logged_and_the_draft_still_succeeds():
    _, result, logs = draft(FakeGateway(fail=True))
    assert result.page_id == "new-1"
    assert any("mom raw telegram failed" in line for line in logs)


def test_dry_print_sends_nothing():
    gw = FakeGateway()
    draft(gw, dry_print=True, out=lambda _: None)
    assert gw.sent == []


@pytest.mark.parametrize("gateway, destination", [(FakeGateway(), None), (None, (CHAT, TOPIC))])
def test_without_the_config_keys_or_a_gateway_nothing_is_sent(gateway, destination):
    _, result, _ = draft(gateway, destination)
    assert result.page_id == "new-1"
    assert gateway is None or gateway.sent == []


@pytest.mark.parametrize("reason, kind", [
    ("grounding: Dario is not in the notes", "grounding word"),
    ("grounding: date 11/9 is not in the notes", "grounding date"),
    ("grounding: number 12 is not in the notes", "grounding number"),
    ("grounding: episode 4 is not in the notes", "grounding episode"),
    ("grounding: unresolved placeholder [person 1]", "grounding placeholder"),
    ("grounding: more than one date from an older block", "grounding older date"),
    ("grounding: older date 11/6 is not upcoming", "grounding older date"),
    ("grounding: no bullets", "grounding no bullets"),
    ("refused rule=masking failure=leak", "masking leak"),
    ("output invalid failure=schema", "output invalid"),
    ("API error status=500", "llm error"),
    ("LLM calls are turned off", "llm error"),
    (None, "llm error"),
])
def test_rule_kind_never_carries_the_word(reason, kind):
    assert rule_kind(reason) == kind


def test_config_reads_the_bots_test_keys(tmp_path):
    from t6_notion_reports.config import load_config
    path = tmp_path / "config.yaml"
    path.write_text("bots_test_chat_id: -100777\nbots_test_topic_id: 42\n")
    cfg = load_config(str(path), {})
    assert (cfg.bots_test_chat_id, cfg.bots_test_topic_id) == (-100777, 42)
    path.write_text("database_path: x.db\n")
    cfg = load_config(str(path), {})
    assert (cfg.bots_test_chat_id, cfg.bots_test_topic_id) == (None, None)


@pytest.mark.parametrize("yaml_text, expected", [
    ("bots_test_chat_id: -100777\nbots_test_topic_id: 42\n", (-100777, 42)),
    ("bots_test_chat_id: -100777\n", None),
    ("bots_test_topic_id: 42\n", None),
    ("database_path: x.db\n", None),
])
def test_lines_go_out_only_with_both_bots_test_keys(tmp_path, yaml_text, expected):
    from t6_notion_reports.cli import _bots_test
    from t6_notion_reports.config import load_config
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text)
    assert _bots_test(load_config(str(path), {})) == expected


def test_a_raw_project_without_a_code_shows_no_code_and_never_its_name():
    gw = FakeGateway()
    which = {"QAZ": NEW_ALL["QAZ"]}
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    nameless = lambda notes: [project("qaz", "Secret Show QAZ", "", **{"PM Notes": rich(notes)})]  # noqa: E731
    run_snapshot(conn, FakeNotion(nameless(OLD_ALL["QAZ"]), []), "friday", FRI)
    run_snapshot(conn, FakeNotion(nameless(which["QAZ"]), []), "monday", MON)
    run_draft_mom(conn, FakeNotion(nameless(which["QAZ"]), []), MEETING, DAY, log=lambda _: None,
                  llm=adapter(FakeLlmApi(lambda i: ok({"bullets": ["Dario confirmed 12 shooting days"]}))),
                  gateway=gw, raw_destination=(CHAT, TOPIC))
    ((_, _, key, text),) = gw.sent
    assert key == "psr-raw-2026-10-26-qaz" and "NO-CODE" in text and "Secret Show" not in text
