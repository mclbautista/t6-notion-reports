"""Sending the shift reports on schedule (#26): config, daily slots, gateway keys, banner, logs."""
from datetime import datetime, time, timedelta, timezone

import pytest
import yaml

from fake_notion import CONTACTS, JOB_ORDERS, PROJECTS
from html_check import assert_valid_html
from test_schedule import FakeClock, manila
from test_shift_report import JO_ID, PEOPLE, PROJS, RoutedNotion, at, jo, prior, world
from t6_notion_reports import cli
from t6_notion_reports.config import ConfigError, load_config
from t6_notion_reports.gateway import GatewayClient, GatewayError
from t6_notion_reports.jo_snapshots import connect
from t6_notion_reports.schedule import SHIFT_SLOTS, SLOTS, Scheduler
from t6_notion_reports.shift_job import ShiftTarget, run_shift_report

MNL = timezone(timedelta(hours=8))
PM = datetime(2026, 11, 3, 18, tzinfo=MNL)
CHAT, TOPIC = -100777, 9
PROJECTS_ID = "1" * 8 + "-1111-1111-1111-" + "1" * 12
CONTACTS_ID = "2" * 8 + "-2222-2222-2222-" + "2" * 12


class FakeTransport:
    """The gateway's wire: records calls and dedups by idempotency key, as the real gateway does."""

    def __init__(self, fail=False):
        self.calls, self.queued, self.fail = [], {}, fail

    def __call__(self, url, headers, body):
        self.calls.append((url, headers, body))
        if self.fail:
            return 503, None
        ref = self.queued.setdefault(body["idempotency_key"], len(self.queued) + 1)
        return 202, {"message_ref": ref}


def target(transport, secret="shift-s3cret", topic=TOPIC):
    return ShiftTarget(GatewayClient("http://gw:8780", secret, transport, sleep=lambda _: None), CHAT, topic)


def many(n):
    return [jo(f"j{i}", str(i), "AE", task="Export " + "x" * 150, deadline=at(4, 12)) for i in range(n)]


def send(slot, jos, tgt, banner=False, when=PM, conn=None, logs=None):
    conn = conn or connect()
    if not conn.execute("SELECT 1 FROM jo_snapshots").fetchone():
        prior(conn)
    logs = [] if logs is None else logs
    fake = world(*jos)
    run_shift_report(conn, fake, JOB_ORDERS, CONTACTS, PROJECTS, slot, when, banner, log=logs.append, target=tgt)
    return conn, logs


# ---- gateway ------------------------------------------------------------------------------------

def test_bearer_chat_topic_and_part_keys():
    t = FakeTransport()
    send("1800", many(40), target(t))
    assert len(t.calls) > 2
    assert {c[1]["Authorization"] for c in t.calls} == {"Bearer shift-s3cret"}
    assert {(c[2]["to"], c[2]["topic"]) for c in t.calls} == {(CHAT, TOPIC)}
    keys = [c[2]["idempotency_key"] for c in t.calls]
    assert keys == ["shift-2026-11-03-1800"] + [f"shift-2026-11-03-1800-{n}" for n in range(2, len(keys) + 1)]
    for c in t.calls:
        assert c[2]["format"] == "html" and len(c[2]["text"]) <= 4096
        assert_valid_html(c[2]["text"])


def test_topic_is_optional():
    t = FakeTransport()
    send("0500", [], target(t, topic=None), when=datetime(2026, 11, 4, 5, tzinfo=MNL))
    assert "topic" not in t.calls[0][2]
    assert t.calls[0][2]["idempotency_key"] == "shift-2026-11-04-0500"


def test_key_date_is_asia_manila_not_utc():
    t = FakeTransport()
    send("0500", [], target(t), when=datetime(2026, 11, 3, 22, tzinfo=timezone.utc))  # 06:00 on Nov 4 in Manila
    assert t.calls[0][2]["idempotency_key"] == "shift-2026-11-04-0500"


def test_midshift_goes_through_the_same_path():
    t = FakeTransport()
    send("1200", [jo("j1", "1", "AE")], target(t), when=datetime(2026, 11, 3, 12, tzinfo=MNL))
    assert [c[2]["idempotency_key"] for c in t.calls] == ["shift-2026-11-03-1200"]


def test_sending_the_same_slot_twice_queues_nothing_new():
    t = FakeTransport()
    conn, _ = send("1800", many(40), target(t))
    first = dict(t.queued)
    send("1800", many(40), target(t), conn=conn)
    assert t.queued == first and len(t.calls) == 2 * len(first)


def test_gateway_failure_raises_after_retries_and_names_no_text_or_secret():
    t = FakeTransport(fail=True)
    with pytest.raises(GatewayError) as err:
        send("1800", [jo("j1", "48", "TOC", task="Secret task")], target(t))
    assert len(t.calls) == 4  # the client's own retries
    assert "shift-s3cret" not in str(err.value) and "Secret task" not in str(err.value)


# ---- banner and log -----------------------------------------------------------------------------

@pytest.mark.parametrize("banner", [True, False])
def test_banner_on_every_part_only_when_on(banner):
    t = FakeTransport()
    send("1800", many(40), target(t), banner=banner)
    assert len(t.calls) > 1
    assert all(("🧪 TEST" in c[2]["text"]) is banner for c in t.calls)


def test_log_line_has_counts_only():
    t, logs = FakeTransport(), []
    send("1800", [jo("j1", "48", "TOC", task="Secret task")], target(t), logs=logs)
    assert len(logs) == 1 and logs[0].startswith("shift slot=1800 snapshot=")
    for field in ("baseline=", "unfinished=1", "received=", "processed=", "accomplished=", "parts=1"):
        assert field in logs[0]
    assert "Secret task" not in logs[0] and "shift-s3cret" not in logs[0]


# ---- config -------------------------------------------------------------------------------------

BASE = {"projects_data_source": PROJECTS_ID, "contacts_data_source": CONTACTS_ID, "jo_data_source": JO_ID}
SHIFT = {"shift_reports_enabled": True, "shift_chat_id": CHAT, "shift_topic_id": TOPIC, "gateway_url": "http://gw:8780"}
ENV = {"NOTION_TOKEN": "tok", "SHIFT_SECRET": " shift-s3cret "}


def write(tmp_path, **cfg):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({**BASE, **cfg}))
    return str(path)


def test_shift_settings_load_without_the_briefing_keys(tmp_path):
    cfg = load_config(write(tmp_path, **SHIFT, ops_chat_id=5, test_banner=True), ENV)
    s = cfg.shift
    assert (s.gateway_url, s.chat_id, s.topic_id, s.ops_chat_id, s.test_banner, s.shift_secret) == (
        "http://gw:8780", CHAT, TOPIC, 5, True, "shift-s3cret")
    assert cfg.brief is None


def test_briefing_and_shift_can_share_gateway_and_banner(tmp_path):
    cfg = load_config(write(tmp_path, **SHIFT, psr_group_chat_id=-1, test_banner=True,
                            cyril_contact_id=PROJECTS_ID), {**ENV, "PSR_SECRET": "psr"})
    assert cfg.brief.psr_secret == "psr" and cfg.shift.shift_secret == "shift-s3cret"
    assert cfg.shift.test_banner and cfg.brief.test_banner


@pytest.mark.parametrize("over,key", [
    ({"shift_chat_id": None}, "shift_chat_id"),
    ({"shift_chat_id": "abc"}, "shift_chat_id"),
    ({"shift_topic_id": "x"}, "shift_topic_id"),
    ({"ops_chat_id": 1.5}, "ops_chat_id"),
    ({"shift_reports_enabled": "yes"}, "shift_reports_enabled"),
    ({"gateway_url": None}, "gateway_url"),
    ({"gateway_url": "ftp://x"}, "gateway_url"),
    ({"test_banner": "hunter2"}, "test_banner"),
    ({"jo_data_source": None}, "jo_data_source"),
])
def test_missing_or_invalid_shift_keys_fail_boot_naming_the_key(tmp_path, over, key):
    cfg = {**BASE, **SHIFT, **over}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({k: v for k, v in cfg.items() if v is not None}))
    with pytest.raises(ConfigError) as err:
        load_config(str(path), ENV)
    assert key in str(err.value) and "hunter2" not in str(err.value)


def test_disabled_shift_reports_change_nothing(tmp_path):
    cfg = load_config(write(tmp_path, shift_reports_enabled=False), ENV)
    assert cfg.shift is None and cfg.brief is None
    assert cfg.jo_data_source == JO_ID
    assert load_config(write(tmp_path), {"NOTION_TOKEN": "tok"}).shift is None  # no SHIFT_SECRET needed


def test_disabled_shift_ignores_other_shift_keys_and_keeps_briefing_detection(tmp_path):
    cfg = load_config(write(tmp_path, shift_reports_enabled=False, shift_chat_id="junk"), ENV)
    assert cfg.shift is None
    with pytest.raises(ConfigError, match="psr_group_chat_id"):  # gateway_url alone still means "briefing"
        load_config(write(tmp_path, shift_reports_enabled=False, gateway_url="http://gw:8780"), ENV)


def test_serve_needs_shift_secret_when_enabled(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    monkeypatch.delenv("SHIFT_SECRET", raising=False)
    assert cli.main(["serve", "--config", write(tmp_path, **SHIFT)]) == 2
    assert "SHIFT_SECRET" in capsys.readouterr().err


# ---- scheduler ----------------------------------------------------------------------------------

def test_slots_only_include_shift_when_enabled(tmp_path):
    on = load_config(write(tmp_path, **SHIFT), ENV)
    off = load_config(write(tmp_path), ENV)
    assert cli._slots(on) == SLOTS + SHIFT_SLOTS and cli._slots(off) == SLOTS


def test_fake_clock_fires_all_three_every_day_and_friday_1800_runs_projects_first():
    fake, fired = FakeClock(manila(2026, 11, 12, 0)), []
    end = manila(2026, 11, 15, 0)
    Scheduler(lambda k: fired.append((fake.now, k)), fake.clock, fake.sleep, log=lambda _: None,
              slots=SLOTS + SHIFT_SLOTS).run(stop=lambda: fake.now >= end)
    assert fired == [
        (manila(2026, 11, 12, 5), "shift0500"), (manila(2026, 11, 12, 12), "shift1200"),
        (manila(2026, 11, 12, 18), "shift1800"),
        (manila(2026, 11, 13, 5), "shift0500"), (manila(2026, 11, 13, 12), "shift1200"),
        (manila(2026, 11, 13, 18), "friday"), (manila(2026, 11, 13, 18), "shift1800"),
        (manila(2026, 11, 14, 5), "shift0500"), (manila(2026, 11, 14, 12), "shift1200"),
        (manila(2026, 11, 14, 18), "shift1800"),
    ]


def test_scheduler_job_snapshots_and_sends_and_a_gateway_failure_does_not_stop_the_loop(tmp_path, monkeypatch):
    cfg = load_config(write(tmp_path, **SHIFT, database_path=str(tmp_path / "psr.db")), ENV)
    fake = RoutedNotion(projects=PROJS, contacts=PEOPLE, job_orders=[jo("j1", "48", "TOC")])
    t, logs, now = FakeTransport(), [], [PM]
    job = cli.make_job(None, fake, cfg, None, lambda: now[0], shift_target=target(t))
    job("shift1800")
    assert [c[2]["idempotency_key"] for c in t.calls] == ["shift-2026-11-03-1800"]
    assert {c[1]["Authorization"] for c in t.calls} == {"Bearer shift-s3cret"}
    # Same slot again on the same day: same key, nothing new queued.
    job("shift1800")
    assert len(t.queued) == 1
    # Down gateway: the job raises, the scheduler logs the type and keeps going.
    down = cli.make_job(None, fake, cfg, None, lambda: PM, shift_target=target(FakeTransport(fail=True)))
    clock = FakeClock(manila(2026, 11, 3, 17, 59))
    ran = []
    Scheduler(lambda k: (ran.append(k), down(k)), clock.clock, clock.sleep, log=logs.append,
              slots=SHIFT_SLOTS).run(stop=lambda: clock.now >= manila(2026, 11, 4, 5, 1))
    assert ran == ["shift1800", "shift0500"]
    assert any("job failed kind=shift1800 error=GatewayError" in line for line in logs)
    assert not any("shift-s3cret" in line for line in logs)


def test_cli_sends_one_slot_by_hand_with_shift_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    monkeypatch.setenv("SHIFT_SECRET", "shift-s3cret")
    monkeypatch.setenv("PSR_SECRET", "psr-s3cret")
    path = write(tmp_path, **SHIFT, database_path=str(tmp_path / "psr.db"))
    fake = RoutedNotion(projects=PROJS, contacts=PEOPLE, job_orders=[jo("j1", "48", "TOC")])
    t = FakeTransport()
    monkeypatch.setattr("t6_notion_reports.cli.GatewayClient",
                        lambda url, secret, **k: GatewayClient(url, secret, t, sleep=lambda _: None))
    assert cli.main(["shift-report", "--slot", "1800", "--config", path], notion=fake, now=PM) == 0
    assert {c[1]["Authorization"] for c in t.calls} == {"Bearer shift-s3cret"}
    assert capsys.readouterr().out == ""  # nothing printed when sent


def test_cli_send_needs_shift_enabled_and_secret_and_gateway_failure_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    monkeypatch.delenv("SHIFT_SECRET", raising=False)
    assert cli.main(["shift-report", "--slot", "1800", "--config", write(tmp_path)]) == 2
    assert "shift_reports_enabled" in capsys.readouterr().err
    assert cli.main(["shift-report", "--slot", "1800", "--config", write(tmp_path, **SHIFT)]) == 2
    assert "SHIFT_SECRET" in capsys.readouterr().err
    monkeypatch.setenv("SHIFT_SECRET", "shift-s3cret")
    monkeypatch.setattr("t6_notion_reports.cli.GatewayClient",
                        lambda url, secret, **k: GatewayClient(url, secret, FakeTransport(fail=True), sleep=lambda _: None))
    fake = RoutedNotion(projects=PROJS, contacts=PEOPLE, job_orders=[])
    path = write(tmp_path, **SHIFT, database_path=str(tmp_path / "psr.db"))
    assert cli.main(["shift-report", "--slot", "1800", "--config", path], notion=fake, now=PM) == 1
    assert "shift-s3cret" not in capsys.readouterr().err
