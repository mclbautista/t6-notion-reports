"""Sending the shift reports on schedule (#26): config, daily slots, gateway keys, banner, logs."""
from datetime import datetime, time, timedelta, timezone

import pytest
import yaml

from fake_notion import CONTACTS, JOB_ORDERS, PROJECTS
from html_check import assert_valid_html
from test_schedule import FakeClock, manila
from test_shift_report import JO_ID, PEOPLE, PROJS, RoutedNotion, at, jo, prior, world
from t6_notion_reports import cli
from t6_notion_reports.config import ConfigError, Destination, load_config
from t6_notion_reports.gateway import GatewayClient, GatewayError
from t6_notion_reports.jo_snapshots import connect
from t6_notion_reports.schedule import SHIFT_SLOTS, SLOTS, Scheduler
from t6_notion_reports.shift_job import ShiftTarget, run_shift_report

MNL = timezone(timedelta(hours=8))
PM = datetime(2026, 11, 3, 19, tzinfo=MNL)
CHAT, TOPIC = -100777, 9
USER, USER2 = 100000001, 100000002
DK1900 = f"shift-2026-11-03-1900-c{CHAT}t{TOPIC}"  # key of part 1 for the group destination
PROJECTS_ID = "1" * 8 + "-1111-1111-1111-" + "1" * 12
CONTACTS_ID = "2" * 8 + "-2222-2222-2222-" + "2" * 12


class FakeTransport:
    """The gateway's wire: records calls and dedups by idempotency key, as the real gateway does."""

    def __init__(self, fail=False):
        self.calls, self.queued, self.fail = [], {}, fail

    def __call__(self, url, headers, body):
        if body is None:  # GET /notify/{ref}: everything queued is already sent
            return 200, {"state": "sent"}
        self.calls.append((url, headers, body))
        if self.fail:
            return 503, None
        ref = self.queued.setdefault(body["idempotency_key"], len(self.queued) + 1)
        return 202, {"message_ref": ref}


def target(transport, secret="shift-s3cret", topic=TOPIC):
    dest = (Destination(CHAT, topic),)
    return ShiftTarget(GatewayClient("http://gw:8780", secret, transport, sleep=lambda _: None),
                       {"0500": dest, "1130": dest, "1900": dest})


def multi(transport, dests, secret="shift-s3cret", ops=None):
    return ShiftTarget(GatewayClient("http://gw:8780", secret, transport, sleep=lambda _: None),
                       {"0500": dests, "1130": dests, "1900": dests}, ops)


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
    send("1900", many(40), target(t))
    assert len(t.calls) > 2
    assert {c[1]["Authorization"] for c in t.calls} == {"Bearer shift-s3cret"}
    assert {(c[2]["to"], c[2]["topic"]) for c in t.calls} == {(CHAT, TOPIC)}
    keys = [c[2]["idempotency_key"] for c in t.calls]
    assert keys == [DK1900] + [f"{DK1900}-{n}" for n in range(2, len(keys) + 1)]
    for c in t.calls:
        assert c[2]["format"] == "html" and len(c[2]["text"]) <= 4096
        assert_valid_html(c[2]["text"])


def test_topic_is_optional():
    t = FakeTransport()
    send("0500", [], target(t, topic=None), when=datetime(2026, 11, 4, 5, tzinfo=MNL))
    assert "topic" not in t.calls[0][2]
    assert t.calls[0][2]["idempotency_key"] == f"shift-2026-11-04-0500-c{CHAT}"


def test_key_date_is_asia_manila_not_utc():
    t = FakeTransport()
    send("0500", [], target(t), when=datetime(2026, 11, 3, 22, tzinfo=timezone.utc))  # 06:00 on Nov 4 in Manila
    assert t.calls[0][2]["idempotency_key"] == f"shift-2026-11-04-0500-c{CHAT}t{TOPIC}"


def test_midshift_goes_through_the_same_path():
    t = FakeTransport()
    send("1130", [jo("j1", "1", "AE")], target(t), when=datetime(2026, 11, 3, 11, 30, tzinfo=MNL))
    assert [c[2]["idempotency_key"] for c in t.calls] == [f"shift-2026-11-03-1130-c{CHAT}t{TOPIC}"]


def test_sending_the_same_slot_twice_queues_nothing_new():
    t = FakeTransport()
    conn, _ = send("1900", many(40), target(t))
    first = dict(t.queued)
    send("1900", many(40), target(t), conn=conn)
    assert t.queued == first and len(t.calls) == 2 * len(first)


def test_rerun_with_changed_data_reuses_the_original_parts_and_queues_no_new_ones():
    t = FakeTransport()
    conn, _ = send("1900", [jo("j1", "1", "AE")], target(t))
    first_calls = [(c[2]["idempotency_key"], c[2]["text"]) for c in t.calls]
    assert len(first_calls) == 1
    send("1900", many(40), target(t), conn=conn)  # same slot and date, now seven-ish parts
    assert set(t.queued) == {first_calls[0][0]}
    assert [(c[2]["idempotency_key"], c[2]["text"]) for c in t.calls] == first_calls * 2


def test_rerun_with_changed_data_keeps_every_destinations_parts():
    t = FakeTransport()
    tgt = multi(t, (GROUP, DM1))
    conn, _ = send("1900", [jo("j1", "1", "AE")], tgt)
    queued = dict(t.queued)
    send("1900", many(40), tgt, conn=conn)
    assert t.queued == queued and len(queued) == 2


def test_rerun_resends_a_part_the_gateway_rejected_earlier():
    class RejectPart2Once(FakeTransport):
        def __init__(self):
            super().__init__()
            self.failing = True

        def __call__(self, url, headers, body):
            if body is not None and self.failing and body["idempotency_key"].endswith("-2"):
                self.calls.append((url, headers, body))
                return 503, None
            return super().__call__(url, headers, body)

    t = RejectPart2Once()
    conn = connect()
    with pytest.raises(GatewayError):
        send("1900", many(40), target(t), conn=conn)
    def report_keys():
        return [k for k in t.queued if not k.endswith("-undelivered")]

    assert len(report_keys()) == 1
    t.failing = False
    send("1900", many(40), target(t), conn=conn)
    assert len(report_keys()) > 2  # part 2 onwards queued on the rerun, from the stored parts


def test_gateway_failure_raises_after_retries_and_names_no_text_or_secret():
    t = FakeTransport(fail=True)
    with pytest.raises(GatewayError) as err:
        send("1900", [jo("j1", "48", "TOC", task="Secret task")], target(t))
    assert len([c for c in t.calls if not c[2]["idempotency_key"].endswith("-undelivered")]) == 4  # the client's own retries
    assert "shift-s3cret" not in str(err.value) and "Secret task" not in str(err.value)


# ---- destinations -------------------------------------------------------------------------------

DM1, DM2, GROUP = Destination(USER, dm=True), Destination(USER2, dm=True), Destination(CHAT, TOPIC)


class SelectiveTransport(FakeTransport):
    """Rejects (503) every send addressed to ``reject``."""

    def __init__(self, reject):
        super().__init__()
        self.reject = reject

    def __call__(self, url, headers, body):
        if body is not None and body["to"] in self.reject:
            self.calls.append((url, headers, body))
            return 503, None
        return super().__call__(url, headers, body)


def test_dm_has_no_topic_group_has_topic_and_keys_carry_the_destination():
    t = FakeTransport()
    send("1900", [jo("j1", "48", "TOC")], multi(t, (DM1, GROUP)))
    assert [(c[2]["to"], c[2].get("topic"), c[2]["idempotency_key"]) for c in t.calls] == [
        (USER, None, f"shift-2026-11-03-1900-u{USER}"),
        (CHAT, TOPIC, f"shift-2026-11-03-1900-c{CHAT}t{TOPIC}")]
    assert "topic" not in t.calls[0][2]


def test_every_part_goes_to_every_destination_in_config_order_and_a_rerun_repeats_the_keys():
    t = FakeTransport()
    tgt = multi(t, (DM2, DM1, GROUP))
    conn, _ = send("1900", many(40), tgt)
    n = len(t.calls) // 3
    assert n > 1
    sent = [(c[2]["to"], c[2]["idempotency_key"]) for c in t.calls]
    expect = []
    for d, tag in ((USER2, f"u{USER2}"), (USER, f"u{USER}"), (CHAT, f"c{CHAT}t{TOPIC}")):
        base = f"shift-2026-11-03-1900-{tag}"
        expect += [(d, base)] + [(d, f"{base}-{k}") for k in range(2, n + 1)]
    assert sent == expect
    texts = [c[2]["text"] for c in t.calls]
    assert texts[:n] == texts[n:2 * n] == texts[2 * n:]
    first = dict(t.queued)
    send("1900", many(40), tgt, conn=conn)
    assert t.queued == first and [c[2]["idempotency_key"] for c in t.calls[3 * n:]] == [k for _, k in expect]


def test_each_slot_goes_only_to_its_own_list():
    t = FakeTransport()
    tgt = ShiftTarget(GatewayClient("http://gw:8780", "s", t, sleep=lambda _: None),
                      {"1130": (GROUP,), "1900": (DM1,), "0500": (DM2,)})
    send("1900", [], tgt)
    assert {c[2]["to"] for c in t.calls} == {USER}
    t.calls.clear()
    send("0500", [], tgt, when=datetime(2026, 11, 4, 5, tzinfo=MNL))
    assert {c[2]["to"] for c in t.calls} == {USER2}


@pytest.mark.parametrize("dests", [(), None])
def test_empty_or_missing_list_sends_nothing_and_logs_one_skip_line(dests):
    t, logs = FakeTransport(), []
    tgt = ShiftTarget(GatewayClient("http://gw:8780", "s", t, sleep=lambda _: None), {} if dests is None else {"1900": ()})
    send("1900", [jo("j1", "48", "TOC")], tgt, logs=logs)
    assert t.calls == []
    assert [line for line in logs if "no destinations" in line] == ["shift slot=1900 has no destinations; not sent"]


def test_a_rejected_destination_does_not_stop_the_others_and_is_recorded():
    t, logs = SelectiveTransport({USER}), []
    tgt = multi(t, (DM1, DM2, GROUP))
    send("1900", many(40), tgt, logs=logs)
    by = {}
    for c in t.calls:
        if c[2]["idempotency_key"].endswith("-undelivered"):
            continue  # the alert for USER goes to the first group
        by.setdefault(c[2]["to"], []).append(c[2]["idempotency_key"])
    parts = len(by[CHAT])
    assert parts > 1 and len(by[USER2]) == parts
    assert all(c[2]["to"] != USER or c[2]["idempotency_key"] == f"shift-2026-11-03-1900-u{USER}" for c in t.calls)
    assert len(logs) == 1 and "destinations=3" in logs[0]


def test_whole_gateway_down_still_raises_after_trying_every_destination():
    t = FakeTransport(fail=True)
    tgt = multi(t, (DM1, GROUP))
    with pytest.raises(GatewayError):
        send("1900", [jo("j1", "48", "TOC")], tgt)
    assert len([c for c in t.calls if not c[2]["idempotency_key"].endswith("-undelivered")]) == 8


# ---- delivery check and undelivered alert (#88) -------------------------------------------------

OPS = -100555


class StatusTransport(FakeTransport):
    """Also answers GET /notify/{ref} (body None) from a script: ``script[ref]`` is a list of states, one per
    poll; the last one repeats. ``"error"`` answers 500. A ref with no script is ``sent`` at once."""

    def __init__(self, script=None, reject=()):
        super().__init__()
        self.script, self.reject, self.polls = script or {}, reject, []

    def __call__(self, url, headers, body):
        if body is None:
            ref = int(url.rsplit("/", 1)[1])
            self.polls.append(ref)
            seq = self.script.get(ref, ["sent"])
            state = seq[min(self.polls.count(ref) - 1, len(seq) - 1)]
            return (500, None) if state == "error" else (200, {"state": state})
        if body["to"] in self.reject:
            self.calls.append((url, headers, body))
            return 503, None
        return super().__call__(url, headers, body)

    def ref_of(self, to):
        return [self.queued[c[2]["idempotency_key"]] for c in self.calls if c[2]["to"] == to and c[2]["idempotency_key"] in self.queued]

    def alerts(self):
        return [c[2] for c in self.calls if c[2]["idempotency_key"].endswith("-undelivered")]


class StepClock:
    """Fake clock and sleep: sleeping moves the clock."""

    def __init__(self):
        self.now, self.sleeps = datetime(2026, 11, 3, 11, tzinfo=timezone.utc), []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


def checked(t, dests, ops=OPS, slot="1900", jos=(), clock=None):
    clock = clock or StepClock()
    tgt = multi(t, dests, ops=ops)
    conn, logs = connect(), []
    prior(conn)
    run_shift_report(conn, world(*jos), JOB_ORDERS, CONTACTS, PROJECTS, slot, PM, log=logs.append, target=tgt,
                     clock=clock, sleep=clock.sleep)
    return logs, clock


def test_all_sent_at_once_means_no_alert_and_no_waiting():
    t = StatusTransport()
    logs, clock = checked(t, (DM1, GROUP))
    assert t.alerts() == [] and clock.sleeps == [] and sorted(t.polls) == [1, 2]
    assert "undelivered=0" in logs[0]


def test_polling_stops_early_once_everything_is_final():
    t = StatusTransport({1: ["queued", "queued", "sent"], 2: ["queued", "sent"]})
    logs, clock = checked(t, (DM1, GROUP))
    assert clock.sleeps == [15, 15] and t.alerts() == []
    assert t.polls == [1, 2, 1, 2, 1]  # a ref that is final is not asked again
    assert "undelivered=0" in logs[0]


def test_failed_ref_is_alerted_by_id_with_state_failed_and_no_report_text():
    t = StatusTransport({1: ["queued", "failed"]})
    logs, _ = checked(t, (DM1, DM2), jos=[jo("j1", "48", "TOC", task="Secret task")])
    (alert,) = t.alerts()
    assert alert["to"] == OPS and "topic" not in alert
    assert alert["idempotency_key"] == "shift-2026-11-03-1900-undelivered"
    assert "1900" in alert["text"] and f"{USER} (failed)" in alert["text"] and str(USER2) not in alert["text"]
    assert "Secret task" not in alert["text"] and "TOC" not in alert["text"]
    assert "undelivered=1" in logs[0]


def test_ref_still_queued_after_five_minutes_is_alerted_as_still_queued():
    t = StatusTransport({2: ["queued"]})
    logs, clock = checked(t, (DM1, GROUP))
    (alert,) = t.alerts()
    assert f"chat {CHAT} topic {TOPIC} (still queued)" in alert["text"] and str(USER) not in alert["text"]
    assert sum(clock.sleeps) == 300 and set(clock.sleeps) == {15}
    assert "undelivered=1" in logs[0]


def test_status_call_erroring_throughout_counts_as_undelivered_and_unknown():
    t = StatusTransport({1: ["error"]})
    logs, clock = checked(t, (DM1,))
    (alert,) = t.alerts()
    assert f"{USER} (unknown)" in alert["text"] and sum(clock.sleeps) == 300
    assert "undelivered=1" in logs[0]


def test_a_status_error_that_recovers_is_not_undelivered():
    t = StatusTransport({1: ["error", "sent"]})
    _, clock = checked(t, (DM1,))
    assert t.alerts() == [] and clock.sleeps == [15]


def test_destination_rejected_at_send_is_alerted_as_rejected_while_others_are_delivered():
    t = StatusTransport(reject={USER})
    logs, _ = checked(t, (DM1, DM2, GROUP))
    assert len(t.ref_of(USER2)) >= 1 and len(t.ref_of(CHAT)) >= 1
    (alert,) = t.alerts()
    assert f"{USER} (rejected)" in alert["text"] and str(USER2) not in alert["text"] and str(CHAT) not in alert["text"]
    assert "destinations=3" in logs[0] and "undelivered=1" in logs[0]


def test_one_alert_lists_every_undelivered_destination_even_with_many_parts():
    t = StatusTransport({1: ["failed"], 2: ["failed"]}, reject=set())
    t.script.update({r: ["failed"] for r in range(1, 100) if r % 2})
    checked(t, (DM1, DM2), jos=many(40))
    (alert,) = t.alerts()
    assert str(USER) in alert["text"] and alert["text"].count("(failed)") >= 1


def test_every_destination_rejected_still_alerts_then_raises():
    t = StatusTransport(reject={USER, CHAT, OPS})
    tgt = multi(t, (DM1, GROUP), ops=None)
    conn, logs, clock = connect(), [], StepClock()
    prior(conn)
    with pytest.raises(GatewayError):
        run_shift_report(conn, world(), JOB_ORDERS, CONTACTS, PROJECTS, "1900", PM, log=logs.append, target=tgt,
                         clock=clock, sleep=clock.sleep)
    assert clock.sleeps == [] and any("undelivered=2" in line for line in logs)
    assert len([c for c in t.calls if c[2]["idempotency_key"].endswith("-undelivered")]) > 0  # an alert was tried


def test_alert_follows_the_shared_rule_ops_chat_then_first_group_then_log_only():
    t = StatusTransport({1: ["failed"]})
    checked(t, (DM1, GROUP), ops=None)
    (alert,) = t.alerts()
    assert (alert["to"], alert["topic"]) == (CHAT, TOPIC)
    t = StatusTransport({1: ["failed"]})
    logs, _ = checked(t, (DM1,), ops=None)
    assert t.alerts() == [] and any("alert not sent" in line for line in logs)
    assert not any(str(USER) in line for line in logs)


def test_rerun_resends_the_same_alert_key():
    t = StatusTransport({1: ["failed"]})
    conn, clock = connect(), StepClock()
    prior(conn)
    tgt = multi(t, (DM1,), ops=OPS)
    for _ in range(2):
        run_shift_report(conn, world(), JOB_ORDERS, CONTACTS, PROJECTS, "1900", PM, log=lambda _: None, target=tgt,
                         clock=clock, sleep=clock.sleep)
    assert [a["idempotency_key"] for a in t.alerts()] == ["shift-2026-11-03-1900-undelivered"] * 2


def test_alert_gateway_failure_does_not_lose_the_run_line():
    t = StatusTransport({1: ["failed"]}, reject={OPS})
    logs, _ = checked(t, (DM1,))
    assert any("undelivered=1" in line for line in logs)


def test_skipped_slot_and_dry_print_do_no_polling():
    t = StatusTransport()
    tgt = ShiftTarget(GatewayClient("http://gw:8780", "s", t, sleep=lambda _: None), {"1900": ()}, OPS)
    conn, clock = connect(), StepClock()
    prior(conn)
    run_shift_report(conn, world(), JOB_ORDERS, CONTACTS, PROJECTS, "1900", PM, log=lambda _: None, target=tgt,
                     clock=clock, sleep=clock.sleep)
    run_shift_report(conn, world(), JOB_ORDERS, CONTACTS, PROJECTS, "1900", PM, log=lambda _: None, out=lambda _: None,
                     clock=clock, sleep=clock.sleep)
    assert t.calls == [] and t.polls == [] and clock.sleeps == []


def test_run_log_has_counts_without_ids_keys_or_text():
    t = StatusTransport({1: ["failed"]})
    logs, _ = checked(t, (DM1, GROUP), jos=[jo("j1", "48", "TOC", task="Secret task")])
    (line,) = logs
    assert "destinations=2 " in line and line.endswith("undelivered=1")
    for bad in (str(USER), str(CHAT), str(OPS), "shift-2026", "Secret task", "failed"):
        assert bad not in line


# ---- banner and log -----------------------------------------------------------------------------

@pytest.mark.parametrize("banner", [True, False])
def test_banner_on_every_part_only_when_on(banner):
    t = FakeTransport()
    send("1900", many(40), target(t), banner=banner)
    assert len(t.calls) > 1
    assert all(("🧪 TEST" in c[2]["text"]) is banner for c in t.calls)


def test_log_line_has_counts_only():
    t, logs = FakeTransport(), []
    send("1900", [jo("j1", "48", "TOC", task="Secret task")], target(t), logs=logs)
    assert len(logs) == 1 and logs[0].startswith("shift slot=1900 snapshot=")
    for field in ("destinations=1", "baseline=", "unfinished=1", "received=", "processed=", "accomplished=", "parts=1"):
        assert field in logs[0]
    assert "Secret task" not in logs[0] and "shift-s3cret" not in logs[0]
    assert str(CHAT) not in logs[0] and "shift-2026" not in logs[0]


# ---- config -------------------------------------------------------------------------------------

BASE = {"projects_data_source": PROJECTS_ID, "contacts_data_source": CONTACTS_ID, "jo_data_source": JO_ID}
SHIFT = {"shift_reports_enabled": True, "gateway_url": "http://gw:8780",
         "shift_destinations": {"mid": [{"chat": CHAT, "topic": TOPIC}], "day": [{"chat": CHAT, "topic": TOPIC}],
                                "night": [{"chat": CHAT, "topic": TOPIC}]}}
ENV = {"NOTION_TOKEN": "tok", "SHIFT_SECRET": " shift-s3cret "}


def write(tmp_path, **cfg):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({**BASE, **cfg}))
    return str(path)


def test_shift_settings_load_without_the_briefing_keys(tmp_path):
    cfg = load_config(write(tmp_path, **SHIFT, ops_chat_id=5, test_banner=True), ENV)
    s = cfg.shift
    assert (s.gateway_url, s.destinations["1900"], s.ops_chat_id, s.test_banner, s.shift_secret) == (
        "http://gw:8780", (Destination(CHAT, TOPIC),), 5, True, "shift-s3cret")
    assert cfg.brief is None


def test_briefing_and_shift_can_share_gateway_and_banner(tmp_path):
    cfg = load_config(write(tmp_path, **SHIFT, psr_group_chat_id=-1, test_banner=True,
                            cyril_contact_id=PROJECTS_ID), {**ENV, "PSR_SECRET": "psr"})
    assert cfg.brief.psr_secret == "psr" and cfg.shift.shift_secret == "shift-s3cret"
    assert cfg.shift.test_banner and cfg.brief.test_banner


@pytest.mark.parametrize("over,key", [
    ({"shift_destinations": None}, "shift_destinations"),
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
    cfg = load_config(write(tmp_path, shift_reports_enabled=False, shift_chat_id="junk", shift_destinations="junk"), ENV)
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


def test_fake_clock_fires_all_three_every_day_and_friday_1900_fires_on_its_own():
    fake, fired = FakeClock(manila(2026, 11, 12, 0)), []
    end = manila(2026, 11, 15, 0)
    Scheduler(lambda k: fired.append((fake.now, k)), fake.clock, fake.sleep, log=lambda _: None,
              slots=SLOTS + SHIFT_SLOTS).run(stop=lambda: fake.now >= end)
    assert fired == [
        (manila(2026, 11, 12, 5), "shift0500"), (manila(2026, 11, 12, 11, 30), "shift1130"),
        (manila(2026, 11, 12, 19), "shift1900"),
        (manila(2026, 11, 13, 5), "shift0500"), (manila(2026, 11, 13, 11, 30), "shift1130"),
        (manila(2026, 11, 13, 19), "shift1900"), (manila(2026, 11, 13, 23, 30), "friday"),
        (manila(2026, 11, 14, 5), "shift0500"), (manila(2026, 11, 14, 11, 30), "shift1130"),
        (manila(2026, 11, 14, 19), "shift1900"),
    ]


def test_scheduler_job_snapshots_and_sends_and_a_gateway_failure_does_not_stop_the_loop(tmp_path, monkeypatch):
    cfg = load_config(write(tmp_path, **SHIFT, database_path=str(tmp_path / "psr.db")), ENV)
    fake = RoutedNotion(projects=PROJS, contacts=PEOPLE, job_orders=[jo("j1", "48", "TOC")])
    t, logs, now = FakeTransport(), [], [PM]
    job = cli.make_job(None, fake, cfg, None, lambda: now[0], shift_target=target(t))
    job("shift1900")
    assert [c[2]["idempotency_key"] for c in t.calls] == [DK1900]
    assert {c[1]["Authorization"] for c in t.calls} == {"Bearer shift-s3cret"}
    # Same slot again on the same day: same key, nothing new queued.
    job("shift1900")
    assert len(t.queued) == 1
    # Down gateway: the job raises, the scheduler logs the type and keeps going.
    down = cli.make_job(None, fake, cfg, None, lambda: PM, shift_target=target(FakeTransport(fail=True)))
    clock = FakeClock(manila(2026, 11, 3, 17, 59))
    ran = []
    Scheduler(lambda k: (ran.append(k), down(k)), clock.clock, clock.sleep, log=logs.append,
              slots=SHIFT_SLOTS).run(stop=lambda: clock.now >= manila(2026, 11, 4, 5, 1))
    assert ran == ["shift1900", "shift0500"]
    assert any("job failed kind=shift1900 error=GatewayError" in line for line in logs)
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
    assert cli.main(["shift-report", "--slot", "1900", "--config", path], notion=fake, now=PM) == 0
    assert {c[1]["Authorization"] for c in t.calls} == {"Bearer shift-s3cret"}
    assert capsys.readouterr().out == ""  # nothing printed when sent


def test_cli_send_needs_shift_enabled_and_secret_and_gateway_failure_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    monkeypatch.delenv("SHIFT_SECRET", raising=False)
    assert cli.main(["shift-report", "--slot", "1900", "--config", write(tmp_path)]) == 2
    assert "shift_reports_enabled" in capsys.readouterr().err
    assert cli.main(["shift-report", "--slot", "1900", "--config", write(tmp_path, **SHIFT)]) == 2
    assert "SHIFT_SECRET" in capsys.readouterr().err
    monkeypatch.setenv("SHIFT_SECRET", "shift-s3cret")
    monkeypatch.setattr("t6_notion_reports.cli.GatewayClient",
                        lambda url, secret, **k: GatewayClient(url, secret, FakeTransport(fail=True), sleep=lambda _: None))
    fake = RoutedNotion(projects=PROJS, contacts=PEOPLE, job_orders=[])
    path = write(tmp_path, **SHIFT, database_path=str(tmp_path / "psr.db"))
    assert cli.main(["shift-report", "--slot", "1900", "--config", path], notion=fake, now=PM) == 1
    assert "shift-s3cret" not in capsys.readouterr().err
