"""Notion down at a shift slot (#27): retry ~30 minutes, one ops alert, skip the slot."""
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from fake_notion import CONTACTS, JOB_ORDERS, PROJECTS
from test_shift_report import PM, PREV_1900, at, jo, prior, world
from test_shift_send import CHAT, TOPIC, FakeTransport
from t6_notion_reports.config import Destination
from t6_notion_reports.gateway import GatewayClient
from t6_notion_reports.jo_snapshots import connect
from t6_notion_reports.shift_job import NotionUnavailable, ShiftTarget, run_shift_report

OPS = 4242
MNL = timezone(timedelta(hours=8))


class Clock:
    def __init__(self, now):
        self.now = now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


class Outage:
    """Wraps a fake Notion: every read fails until ``recover_at``."""

    def __init__(self, inner, clock, recover_at=None, error=None):
        self.inner, self.clock, self.recover_at = inner, clock, recover_at
        self.error = error or urllib.error.URLError("secret notion text")
        self.reads = 0

    def query_data_source(self, data_source_id, *args):
        self.reads += 1
        if self.recover_at is None or self.clock.now < self.recover_at:
            raise self.error
        return self.inner.query_data_source(data_source_id, *args)


def tgt(transport, ops=OPS):
    dest = (Destination(CHAT, TOPIC),)
    return ShiftTarget(GatewayClient("http://gw:8780", "s", transport, sleep=lambda _: None),
                       {"0500": dest, "1130": dest, "1900": dest}, ops)


def go(conn, notion, clock, transport, slot="1900", when=PM, ops=OPS):
    logs = []
    try:
        run_shift_report(conn, notion, JOB_ORDERS, CONTACTS, PROJECTS, slot, when, log=logs.append,
                         target=tgt(transport, ops), clock=lambda: clock.now, sleep=clock.sleep)
    except NotionUnavailable as exc:
        return exc, logs
    return None, logs


def snapshot_count(conn):
    return conn.execute("SELECT COUNT(*) FROM jo_snapshots").fetchone()[0]


def test_down_for_good_retries_about_30_minutes_then_one_alert_and_no_report():
    clock, transport, conn = Clock(PM), FakeTransport(), connect()
    prior(conn)
    before = snapshot_count(conn)
    notion = Outage(world(jo("j1")), clock)
    skipped, _ = go(conn, notion, clock, transport)
    assert isinstance(skipped, NotionUnavailable)
    assert timedelta(minutes=29) <= clock.now - PM <= timedelta(minutes=31)
    assert notion.reads > 5
    assert len(transport.calls) == 1
    url, headers, body = transport.calls[0]
    assert (body["to"], body["idempotency_key"]) == (OPS, "shift-2026-11-03-1900-alert")
    assert "topic" not in body
    assert "1900" in body["text"] and "URLError" in body["text"]
    assert "secret notion text" not in body["text"]
    assert snapshot_count(conn) == before


def test_recovery_inside_the_window_sends_the_normal_report_and_no_alert():
    clock, transport, conn = Clock(PM), FakeTransport(), connect()
    prior(conn)
    notion = Outage(world(jo("j1")), clock, recover_at=PM + timedelta(minutes=10))
    skipped, _ = go(conn, notion, clock, transport)
    assert skipped is None
    assert [c[2]["idempotency_key"] for c in transport.calls] == ["shift-2026-11-03-1900"]
    assert transport.calls[0][2]["to"] == CHAT
    assert PM + timedelta(minutes=10) <= clock.now < PM + timedelta(minutes=12)


def test_the_slot_keeps_its_own_time_even_when_notion_recovers_late():
    clock, transport, conn = Clock(PM), FakeTransport(), connect()
    prior(conn)
    go(conn, Outage(world(jo("j1")), clock, recover_at=PM + timedelta(minutes=10)), clock, transport)
    assert conn.execute("SELECT kind FROM jo_snapshots ORDER BY snapshot_id DESC").fetchone()[0] == "1900"


def test_next_slot_after_a_skipped_one_compares_with_the_latest_earlier_snapshot():
    clock, transport, conn = Clock(PM), FakeTransport(), connect()
    prior(conn, when=PREV_1900 - timedelta(days=1))
    go(conn, Outage(world(jo("j1")), clock), clock, transport)  # 19:00 skipped
    nxt = datetime(2026, 11, 4, 5, tzinfo=MNL)
    clock.now = nxt
    skipped, _ = go(conn, world(jo("j1")), clock, transport, slot="0500", when=nxt)
    assert skipped is None
    text = transport.calls[-1][2]["text"]
    assert "compared with" in text.lower() or "Compared with" in text


def test_rerunning_the_alerted_slot_queues_no_second_alert():
    clock, transport, conn = Clock(PM), FakeTransport(), connect()
    prior(conn)
    go(conn, Outage(world(), clock), clock, transport)
    clock.now = PM
    go(conn, Outage(world(), clock), clock, transport)
    assert len(transport.queued) == 1 and len(transport.calls) == 2


def test_missing_ops_chat_id_sends_the_alert_to_the_first_group_entry_marked_alert():
    clock, transport, conn = Clock(PM), FakeTransport(), connect()
    prior(conn)
    go(conn, Outage(world(), clock), clock, transport, ops=None)
    body = transport.calls[0][2]
    assert (body["to"], body["topic"]) == (CHAT, TOPIC)
    assert body["text"].startswith("🚨")


def test_other_errors_are_not_retried():
    clock, transport, conn = Clock(PM), FakeTransport(), connect()
    prior(conn)
    notion = Outage(world(), clock, error=ValueError("bad property"))
    with pytest.raises(ValueError):
        go(conn, notion, clock, transport)
    assert notion.reads == 1 and not transport.calls


def test_dry_print_does_not_retry_or_alert():
    clock, conn = Clock(PM), connect()
    notion = Outage(world(), clock)
    with pytest.raises(urllib.error.URLError):
        run_shift_report(conn, notion, JOB_ORDERS, CONTACTS, PROJECTS, "1900", PM, out=lambda _: None,
                         log=lambda _: None, clock=lambda: clock.now, sleep=clock.sleep)
    assert notion.reads == 1 and clock.now == PM


def test_retrying_slot_gives_up_before_the_next_slot_is_due():
    from t6_notion_reports.shift_job import RETRY_FOR
    from t6_notion_reports.schedule import SHIFT_SLOTS
    gaps = [(b.at.hour - a.at.hour) % 24 for a, b in zip(SHIFT_SLOTS, SHIFT_SLOTS[1:] + SHIFT_SLOTS[:1])]
    assert RETRY_FOR < timedelta(hours=min(gaps))
