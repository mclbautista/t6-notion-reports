"""Run one shift report slot: snapshot Job Orders, pick the bounding snapshot, build the report."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Sequence

from .config import Destination
from .jo_snapshot_job import Notion, run_jo_snapshot
from .jo_snapshots import MANILA, JoSnapshot, load_bounding, load_latest
from .contacts import load_contacts
from .llm import LlmClient
from .redact import Redactor
from .shift_report import is_active
from .task_summary import TaskSummarizer
from .task_summary_llm import LlmTaskSummarizer, SummaryStats, summarize_tasks
from .shift_report import SLOT_LABEL, SLOTS, EndOfShiftReport, build_end_of_shift

# Notion unreachable at a slot: keep trying this long, then alert once and skip the slot. The slots
# are at least six hours apart, so a retrying slot is always done before the next one is due.
RETRY_FOR = timedelta(minutes=30)
RETRY_EVERY = timedelta(minutes=1)


class NotionUnavailable(Exception):
    """Notion could not be read for the whole retry window; the slot was skipped after one alert."""


def find_older(conn: sqlite3.Connection, slot: str, current: JoSnapshot) -> tuple[JoSnapshot | None, str | None]:
    """The snapshot that opens the window, and a "<slot> on <date>" note when it is a fallback.

    Expected: the 05:00 snapshot of the same Manila day (11:30 report), the 11:30 snapshot of the same
    day (19:00 report) or the 19:00 snapshot of the day before (05:00 report). If that one is missing, the latest earlier snapshot of any kind
    (which can be an old 12:00 or 18:00 one from before the slots moved).
    """
    opens_kind = SLOTS[slot].opens_kind
    day = current.taken_at.astimezone(MANILA).date()
    expected_day = day - timedelta(days=1) if slot == "0500" else day
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
    """Where the window would open with no earlier snapshot: 05:00 today (11:30 report), 11:30 today
    (19:00 report) or 19:00 yesterday (05:00 report)."""
    moment = current.taken_at.astimezone(MANILA)
    if slot == "1130":
        return moment.replace(hour=5, minute=0, second=0, microsecond=0)
    if slot == "1900":
        return moment.replace(hour=11, minute=30, second=0, microsecond=0)
    return (moment - timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)


class ShiftTarget:
    """Where reports go: a gateway client authenticated with SHIFT_SECRET, the destinations per slot, ``ops_chat_id``."""

    def __init__(self, gateway, destinations: Mapping[str, Sequence[Destination]], ops_chat_id: int | None = None):
        self.gateway, self.destinations, self.ops_chat_id = gateway, destinations, ops_chat_id

    def first_destination(self, slot: str) -> Destination | None:
        """Interim until the per-destination send (#86): the report goes to the first destination of the slot only."""
        dests = self.destinations.get(slot, ())
        return dests[0] if dests else None

    def first_group(self, slot: str) -> Destination | None:
        return next((d for d in self.destinations.get(slot, ()) if not d.dm), None)


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
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
    summarizer: TaskSummarizer | None = None,
    llm: LlmClient | None = None,
) -> EndOfShiftReport:
    """Snapshot Job Orders (kind = slot), build that slot's report, then print it or send it.

    With a ``target`` the parts go through the gateway, in order, and nothing is printed. A gateway
    failure (after the client's own retries) raises ``GatewayError``; parts already accepted are not
    queued again by a rerun, because the keys are the same.

    When sending, a Notion that cannot be read is retried for ``RETRY_FOR``; if it stays down one
    alert is queued and ``NotionUnavailable`` is raised. Nothing is stored and nothing is caught up
    later. A dry print fails at once, as it always has.

    Task summaries: ``summarizer``, or Haiku through ``llm`` when given, runs after the snapshot and
    before rendering, with a cache. With neither, a Task shows its first three words.
    """
    if target is None:
        run_jo_snapshot(conn, notion, jo_ds, contacts_ds, projects_ds, slot, now)
    else:
        _snapshot_with_retry(conn, notion, jo_ds, contacts_ds, projects_ds, slot, now, target, log, clock, sleep)
    current = load_latest(conn)
    older, compared_with = (None, None) if current.is_baseline else find_older(conn, slot, current)
    stats = SummaryStats()
    if summarizer is None and llm is not None:
        summarizer = _llm_summarizer(notion, contacts_ds, current, llm)
    if summarizer is not None:
        tasks = {p: f.get("Task") for p, f in current.pages.items() if is_active(f)}
        summarizer, stats = summarize_tasks(conn, summarizer, tasks, now)
    report = build_end_of_shift(
        slot, current, older, compared_with, banner,
        window_start=nominal_window_start(slot, current), summarizer=summarizer)
    received, processed, accomplished = report.counts.received, report.counts.processed, report.counts.accomplished
    unfinished = report.unfinished
    parts = len(report.messages)
    # One line per run: counts only, never message text or secrets.
    log(f"shift slot={slot} snapshot={current.snapshot_id} baseline={current.is_baseline} "
        f"unfinished={unfinished} received={received} processed={processed} "
        f"accomplished={accomplished} parts={parts} "
        f"summaries={stats.new} cached={stats.cached} fallback={stats.fallback}")
    if target is None:
        for number, text in enumerate(report.messages, 1):
            out(f"--- part {number}/{parts} ---\n{text}\n")
        return report
    first = target.first_destination(slot)
    if first is None:
        log(f"shift slot={slot} has no destinations; not sent")
        return report
    for key, text in zip(shift_keys(now, slot, parts), report.messages):
        target.gateway.notify(first.chat, text, key, first.topic)
    return report


def _llm_summarizer(notion: Notion, contacts_ds: str, current: JoSnapshot, llm: LlmClient) -> TaskSummarizer | None:
    """Haiku behind the name masker: Contacts people and the projects on the Job Orders are hidden.
    If the masker cannot be built there is no summarizer, so nothing unmasked is ever sent."""
    try:
        labels = {r["id"]: r["name"] for f in current.pages.values() for r in f.get("Projects") or [] if r.get("name")}
        states = {pid: {"Name": name, "Project Code": name} for pid, name in labels.items()}
        redactor = Redactor.build(load_contacts(notion, contacts_ds).values(), [states])
    except Exception:
        return None
    return LlmTaskSummarizer(llm, redactor)


def _snapshot_with_retry(conn, notion, jo_ds, contacts_ds, projects_ds, slot, now, target, log, clock, sleep) -> None:
    """Read Notion until it answers or ``RETRY_FOR`` is up. Only transport errors count as an outage."""
    give_up = clock() + RETRY_FOR
    attempts = 0
    while True:
        attempts += 1
        try:
            run_jo_snapshot(conn, notion, jo_ds, contacts_ds, projects_ds, slot, now)
            return
        except OSError as exc:  # URLError, HTTPError and timeouts; type only, messages may hold data
            error = type(exc).__name__
            if clock() + RETRY_EVERY > give_up:
                break
            sleep(RETRY_EVERY.total_seconds())
    log(f"shift slot={slot} notion unavailable attempts={attempts} error={error}; skipping the slot")
    _send_alert(target, slot, now, error)
    raise NotionUnavailable(f"slot {slot} skipped after {attempts} attempts ({error})")


def _send_alert(target: ShiftTarget, slot: str, now: datetime, error: str) -> None:
    """One ops alert per slot (key ``shift-<date>-<slot>-alert``). Names the slot and the error type only.

    Without ``ops_chat_id`` it goes to the first group entry of the slot's list; with none, it is not sent.
    """
    minutes = int(RETRY_FOR.total_seconds() // 60)
    text = f"🚨 Shift report skipped for slot {slot}: Notion unreachable for {minutes} min (last error: {error})."
    key = f"shift-{now.astimezone(MANILA).date().isoformat()}-{slot}-alert"
    if target.ops_chat_id is not None:
        target.gateway.notify(target.ops_chat_id, text, key)
    else:
        group = target.first_group(slot)
        if group is not None:
            target.gateway.notify(group.chat, text, key, group.topic)
