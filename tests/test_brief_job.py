import json
import sqlite3
from datetime import datetime, timezone

import pytest
import yaml

from fake_notion import CONTACTS, PROJECTS, FakeNotion, contact, date, project, relation, rich, select, title
from t6_notion_reports.brief_job import BriefFailed, run_brief
from t6_notion_reports.cli import main
from t6_notion_reports.config import BriefSettings
from t6_notion_reports.flags import ROLES
from t6_notion_reports.gateway import GatewayClient
from t6_notion_reports.snapshot_job import run_snapshot

CYRIL_ID = "c-cyril"
BRIEF = BriefSettings("http://gw:8780", -1003, 7, CYRIL_ID, False, "s3cret")
SUNDAY = datetime(2026, 11, 15, 8, tzinfo=timezone.utc)  # 16:00 Manila


def tg(handle, user_id):
    return {"TelegramHandle": rich(handle), "TelegramUserID": {"type": "number", "number": user_id}}


def contacts():
    out = [contact("c-pio", "Pio"), contact(CYRIL_ID, "Cyril"), contact("c-angel", "Angel Gaas")]
    out[0]["properties"].update(tg("pio_h", 111))
    out[1]["properties"].update(tg("cyril_h", 999))
    out[2]["properties"].update({"TelegramHandle": rich("@heygelica"), "TelegramUserID": {"type": "number", "number": None}})
    return out


STAFF = [r for r in ROLES if r != "T6 Post Coordinator"]  # the coordinator is set per project


def pages(stage, dubbing_status=None, staffed=True):
    extra = {"Status": select(dubbing_status)} if dubbing_status else {}
    if staffed:  # nothing to flag, so Dubbing stays quiet when unchanged
        extra.update({role: relation("c-pio") for role in STAFF})
        extra.update({"Playdate": date("2026-12-01"), "Submission Deadline": date("2026-12-20")})
    return [
        project("p1", "Show", "PAS", Stage=select(stage), **{"T6 Post Coordinator": relation("c-pio")}),
        project("p2", "Dubbing", "DW", **extra, **{"T6 Post Coordinator": relation("c-angel")}),
    ]


def seeded(staffed):
    c = sqlite3.connect(":memory:")
    from t6_notion_reports.snapshots import SCHEMA
    c.executescript(SCHEMA)
    notion = FakeNotion(pages("Offline", staffed=staffed), contacts())
    run_snapshot(c, notion, PROJECTS, CONTACTS, "monday", datetime(2026, 11, 9, 3, tzinfo=timezone.utc))
    notion = FakeNotion(pages("Online", staffed=staffed), contacts())
    run_snapshot(c, notion, PROJECTS, CONTACTS, "friday", datetime(2026, 11, 13, 10, tzinfo=timezone.utc))
    return c


@pytest.fixture
def conn():
    return seeded(staffed=True)


@pytest.fixture
def conn_unstaffed():
    """Dubbing has blank dates and no team, so it is flagged and may be flagged."""
    return seeded(staffed=False)


class EmptyLlm:
    def complete(self, system, user, schema):
        return schema()


class FakeGateway:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], set(fail_for)

    def notify(self, to, text, key, topic=None):
        from t6_notion_reports.gateway import GatewayError
        if to in self.fail_for:
            raise GatewayError("down")
        self.sent.append((to, topic, key, text))
        return len(self.sent)


def test_sends_dms_only_to_changed_coordinators_plus_digest_through_the_gateway(conn):
    gw = FakeGateway()
    res = run_brief(conn, FakeNotion([], contacts()), CONTACTS, BRIEF, SUNDAY, gateway=gw, log=lambda _: None)
    assert res.sent == 2 and res.failed == 0
    assert {(to, topic, key) for to, topic, key, _ in gw.sent} == {
        (111, None, "notion-reports-2026-11-15-c-pio"),
        (999, None, "psr-digest-2026-11-15"),
    }


def digest_text(gw):
    return next(text for to, _, _, text in gw.sent if to == 999)


def test_digest_no_changes_line_skips_coordinators_with_only_standby_projects():
    c = sqlite3.connect(":memory:")
    from t6_notion_reports.snapshots import SCHEMA
    c.executescript(SCHEMA)
    for kind, stage, at in (("monday", "Offline", datetime(2026, 11, 9, 3, tzinfo=timezone.utc)),
                            ("friday", "Online", datetime(2026, 11, 13, 10, tzinfo=timezone.utc))):
        run_snapshot(c, FakeNotion(pages(stage, "🏋️ Standby"), contacts()), PROJECTS, CONTACTS, kind, at)
    gw = FakeGateway()
    run_brief(c, FakeNotion([], contacts()), CONTACTS, BRIEF, SUNDAY, gateway=gw, log=lambda _: None)
    assert "No changes" not in digest_text(gw)
    assert all(to != -1003 for to, *_ in gw.sent)


def test_digest_no_changes_line_lists_active_quiet_coordinators(conn):
    gw = FakeGateway()
    run_brief(conn, FakeNotion([], contacts()), CONTACTS, BRIEF, SUNDAY, gateway=gw, log=lambda _: None)
    assert "No changes: Angel Gaas" in digest_text(gw)


def test_dry_print_prints_and_sends_nothing(conn):
    gw, printed = FakeGateway(), []
    res = run_brief(conn, FakeNotion([], contacts()), CONTACTS, BRIEF, SUNDAY, gateway=gw,
                    dry_print=True, out=printed.append, log=lambda _: None)
    assert gw.sent == [] and res.sent == 0 and len(res.messages) == 2
    assert any("psr-digest-2026-11-15" in p for p in printed)


def test_a_failed_send_does_not_stop_the_others_and_then_raises(conn):
    gw, logs = FakeGateway(fail_for={111}), []
    with pytest.raises(BriefFailed):
        run_brief(conn, FakeNotion([], contacts()), CONTACTS, BRIEF, SUNDAY, gateway=gw, log=logs.append)
    assert len(gw.sent) == 1
    assert not any("s3cret" in line for line in logs)


def test_no_friday_snapshot_yet_sends_nothing():
    c = sqlite3.connect(":memory:")
    from t6_notion_reports.snapshots import SCHEMA
    c.executescript(SCHEMA)
    gw, printed = FakeGateway(), []
    res = run_brief(c, FakeNotion([], contacts()), CONTACTS, BRIEF, SUNDAY, gateway=gw, out=printed.append, log=lambda _: None)
    assert res.baseline and gw.sent == [] and "first snapshot" in printed[0]


def test_real_gateway_client_posts_through_the_fake_transport(conn):
    calls = []

    def transport(url, headers, body):
        calls.append((url, headers["Authorization"], body))
        return 202, {"message_ref": len(calls)}

    gw = GatewayClient("http://gw:8780", "s3cret", transport, sleep=lambda _: None)
    run_brief(conn, FakeNotion([], contacts()), CONTACTS, BRIEF, SUNDAY, gateway=gw, log=lambda _: None)
    assert len(calls) == 2
    assert all(url == "http://gw:8780/notify" and auth == "Bearer s3cret" and body["format"] == "html"
               for url, auth, body in calls)


def write_config(tmp_path, db):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "projects_data_source": "11111111-1111-1111-1111-111111111111",
        "contacts_data_source": "22222222-2222-2222-2222-222222222222",
        "database_path": str(db),
        "gateway_url": "http://gw:8780", "psr_group_chat_id": -1003, "psr_group_topic_id": 7,
        "cyril_contact_id": "33333333-3333-3333-3333-333333333333",
    }))
    return str(path)


CFG_CYRIL = "33333333-3333-3333-3333-333333333333"


class AnyContacts(FakeNotion):
    """Config ids must look like Notion ids, so answer any contacts query."""

    def query_data_source(self, data_source_id):
        return list(self.contacts)


def seeded_db(tmp_path):
    from t6_notion_reports.snapshots import connect
    db = tmp_path / "psr.db"
    c = connect(str(db))
    run_snapshot(c, FakeNotion(pages("Offline"), contacts()), PROJECTS, CONTACTS, "monday", datetime(2026, 11, 9, 3, tzinfo=timezone.utc))
    run_snapshot(c, FakeNotion(pages("Online"), contacts()), PROJECTS, CONTACTS, "friday", datetime(2026, 11, 13, 10, tzinfo=timezone.utc))
    c.close()
    return db


def cfg_contacts():
    out = contacts()
    out[1]["id"] = CFG_CYRIL
    return out


def test_cli_dry_print_needs_no_secret_and_prints_to_stdout(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NOTION_TOKEN", "ntn_x")
    monkeypatch.delenv("PSR_SECRET", raising=False)
    cfg = write_config(tmp_path, seeded_db(tmp_path))
    assert main(["brief", "--dry-print", "--config", cfg], notion=AnyContacts([], cfg_contacts()), now=SUNDAY) == 0
    out = capsys.readouterr().out
    assert "key=psr-digest-2026-11-15" in out and "PAS" in out


def test_cli_send_without_secret_is_a_config_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NOTION_TOKEN", "ntn_x")
    monkeypatch.delenv("PSR_SECRET", raising=False)
    cfg = write_config(tmp_path, seeded_db(tmp_path))
    assert main(["brief", "--config", cfg], notion=AnyContacts([], cfg_contacts()), now=SUNDAY) == 2
    assert "PSR_SECRET" in capsys.readouterr().err


def test_cli_gateway_failure_exits_non_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NOTION_TOKEN", "ntn_x")
    monkeypatch.setenv("PSR_SECRET", "s3cret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr("t6_notion_reports.cli.OpenRouterClient", lambda key, model: EmptyLlm())
    monkeypatch.setattr("t6_notion_reports.cli.GatewayClient",
                        lambda url, secret: GatewayClient(url, secret, lambda *a: (500, None), sleep=lambda _: None))
    cfg = write_config(tmp_path, seeded_db(tmp_path))
    assert main(["brief", "--config", cfg], notion=AnyContacts([], cfg_contacts()), now=SUNDAY) == 1
    captured = capsys.readouterr()
    assert "s3cret" not in captured.out + captured.err and "brief failed" in captured.err


def test_missing_cyril_id_sends_dms_then_fails(conn):
    gw = FakeGateway()
    no_id = [c for c in contacts()]
    no_id[1]["properties"]["TelegramUserID"] = {"type": "number", "number": None}
    with pytest.raises(BriefFailed, match="digest"):
        run_brief(conn, FakeNotion([], no_id), CONTACTS, BRIEF, SUNDAY, gateway=gw, log=lambda _: None)
    assert len(gw.sent) == 1


class FakeLlm:
    def __init__(self, fail_for=()):
        self.users, self.fail_for = [], set(fail_for)

    def complete(self, system, user, schema):
        from t6_notion_reports.llm import LlmError
        data = json.loads(user)
        self.users.append(data)
        if data["project"]["name"] in self.fail_for:
            raise LlmError("down")
        return schema()


def test_brief_job_llm_failure_still_sends_and_digest_notes_it(conn_unstaffed):
    conn = conn_unstaffed
    gw = FakeGateway()
    logs = []
    res = run_brief(conn, FakeNotion([], contacts()), CONTACTS, BRIEF, SUNDAY, gateway=gw,
                    llm=FakeLlm(fail_for={"Show"}), log=logs.append)
    texts = {key: text for _, _, key, text in gw.sent}
    assert "(LLM unavailable)" in texts["notion-reports-2026-11-15-c-pio"]
    assert "LLM unavailable: PAS" in texts["psr-digest-2026-11-15"]
    assert res.sent == 3 and res.llm_failed == 1
    assert any("llm unavailable project=p1" in line for line in logs)


def test_no_llm_marks_nothing_unavailable(conn):
    gw = FakeGateway()
    run_brief(conn, FakeNotion([], contacts()), CONTACTS, BRIEF, SUNDAY, gateway=gw, log=lambda _: None)
    assert not any("What you owe" in text or "unavailable" in text for *_, text in gw.sent)
