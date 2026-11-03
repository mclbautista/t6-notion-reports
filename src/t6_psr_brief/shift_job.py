"""Run one end-of-shift slot: snapshot Job Orders, pick the bounding snapshot, build the report."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Callable

from .jo_snapshot_job import Notion, run_jo_snapshot
from .jo_snapshots import MANILA, JoSnapshot, load_bounding, load_latest
from .midshift_report import MidshiftReport, build_midshift
from .shift_report import SLOT_LABEL, SLOTS, EndOfShiftReport, build_end_of_shift

MIDSHIFT_SLOT = "1200"


def find_older(conn: sqlite3.Connection, slot: str, current: JoSnapshot) -> tuple[JoSnapshot | None, str | None]:
    """The snapshot that opens the window, and a "<slot> on <date>" note when it is a fallback.

    Expected: the 05:00 snapshot of the same Manila day (18:00 report) or the 18:00 snapshot of the
    day before (05:00 report). If that one is missing, the latest earlier snapshot of any kind.
    """
    opens_kind = SLOTS[slot].opens_kind
    day = current.taken_at.astimezone(MANILA).date()
    expected_day = day if slot == "1800" else day - timedelta(days=1)
    before = current.taken_at - timedelta(microseconds=1)
    older = load_bounding(conn, before, (opens_kind,))
    if older and older.taken_at.astimezone(MANILA).date() == expected_day:
        return older, None
    older = load_bounding(conn, before)
    if older is None:
        return None, None
    when = older.taken_at.astimezone(MANILA)
    return older, f"{SLOT_LABEL[older.kind]} on {when.strftime('%a %b %-d')}"


def nominal_window_start(slot: str, current: JoSnapshot) -> datetime:
    """Where the window would open with no earlier snapshot: 05:00 today (18:00 report) or 18:00 yesterday."""
    moment = current.taken_at.astimezone(MANILA)
    if slot == "1800":
        return moment.replace(hour=5, minute=0, second=0, microsecond=0)
    return (moment - timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)


class ShiftTarget:
    """Where a slot's report goes: a gateway client authenticated with SHIFT_SECRET, a chat and a topic."""

    def __init__(self, gateway, chat_id: int, topic_id: int | None):
        self.gateway, self.chat_id, self.topic_id = gateway, chat_id, topic_id


def shift_keys(now: datetime, slot: str, parts: int) -> list[str]:
    """``shift-<date>-<slot>`` for part 1, then ``-2``, ``-3``... The date is Asia/Manila."""
    base = f"shift-{now.astimezone(MANILA).date().isoformat()}-{slot}"
    return [base] + [f"{base}-{n}" for n in range(2, parts + 1)]


def run_shift_report(
    conn: sqlite3.Connection,
    notion: Notion,
    jo_ds: str,
    contacts_ds: str,
    projects_ds: str,
    slot: str,
    now: datetime,
    banner: bool = False,
    out: Callable[[str], None] = print,
    log: Callable[[str], None] = print,
    target: ShiftTarget | None = None,
) -> EndOfShiftReport | MidshiftReport:
    """Snapshot Job Orders (kind = slot), build that slot's report, then print it or send it.

    With a ``target`` the parts go through the gateway, in order, and nothing is printed. A gateway
    failure (after the client's own retries) raises ``GatewayError``; parts already accepted are not
    queued again by a rerun, because the keys are the same.
    """
    run_jo_snapshot(conn, notion, jo_ds, contacts_ds, projects_ds, slot, now)
    current = load_latest(conn)
    if slot == MIDSHIFT_SLOT:
        report = build_midshift(current, banner)
        received = processed = accomplished = 0
        unfinished = report.active
    else:
        older, compared_with = (None, None) if current.is_baseline else find_older(conn, slot, current)
        report = build_end_of_shift(
            slot, current, older, compared_with, banner,
            window_start=nominal_window_start(slot, current))
        received, processed, accomplished = (
            report.counts.received, report.counts.processed, report.counts.accomplished)
        unfinished = report.unfinished
    parts = len(report.messages)
    # One line per run: counts only, never message text or secrets.
    log(f"shift slot={slot} snapshot={current.snapshot_id} baseline={current.is_baseline} "
        f"unfinished={unfinished} received={received} processed={processed} "
        f"accomplished={accomplished} parts={parts}")
    if target is None:
        for number, text in enumerate(report.messages, 1):
            out(f"--- part {number}/{parts} ---\n{text}\n")
        return report
    for key, text in zip(shift_keys(now, slot, parts), report.messages):
        target.gateway.notify(target.chat_id, text, key, target.topic_id)
    return report
