"""Run one shift report slot: snapshot Job Orders, pick the bounding snapshot, build the report."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Sequence

from .config import Destination
from .gateway import GatewayError
from .notion import error_name, is_outage
from .jo_snapshot_job import Notion, run_jo_snapshot
from .jo_snapshots import MANILA, JoSnapshot, keep_report_parts, load_bounding, load_latest
from .contacts import load_contacts
from .llm import LlmClient
from .masking import Masker
from .task_summary import TaskSummarizer
from .task_summary_llm import LlmTaskSummarizer, SummaryStats, summarize_tasks
from .shift_report import SLOT_LABEL, SLOTS, EndOfShiftReport, build_end_of_shift, is_active

# Notion unreachable at a slot: keep trying this long, then alert once and skip the slot. The slots
# are at least six hours apart, so a retrying slot is always done before the next one is due.
RETRY_FOR = timedelta(minutes=30)
RETRY_EVERY = timedelta(minutes=1)

# Delivery check after the last send: ask the gateway about every queued message this often, for this long.
POLL_EVERY = timedelta(seconds=15)
POLL_FOR = timedelta(minutes=5)


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


@dataclass(frozen=True)
class DestinationResult:
    """What sending one report to one destination did: the gateway ``message_ref`` of each queued part, in
    part order, and whether the gateway rejected it (then ``refs`` holds only the parts queued before that)."""

    destination: Destination
    refs: tuple[int, ...]
    rejected: bool


class ShiftTarget:
    """Where reports go: a gateway client authenticated with SHIFT_SECRET, the destinations per slot, ``ops_chat_id``."""

    def __init__(self, gateway, destinations: Mapping[str, Sequence[Destination]], ops_chat_id: int | None = None):
        self.gateway, self.destinations, self.ops_chat_id = gateway, destinations, ops_chat_id

    def first_group(self, slot: str) -> Destination | None:
        return next((d for d in self.destinations.get(slot, ()) if not d.dm), None)

    def send_alert(self, slot: str, text: str, key: str, log=print) -> bool:
        """The one alert-destination rule: ``ops_chat_id``, else the slot's first group entry (with its topic),
        else nothing is sent and a log line (slot only, no ids, keys or text) says so. True if it was sent."""
        if self.ops_chat_id is not None:
            self.gateway.notify(self.ops_chat_id, text, key)
            return True
        group = self.first_group(slot)
        if group is None:
            log(f"shift slot={slot} alert not sent: no ops_chat_id and no group entry")
            return False
        self.gateway.notify(group.chat, text, key, group.topic)
        return True


def destination_tag(dest: Destination) -> str:
    """``u<user id>`` for a DM, ``c<chat>`` or ``c<chat>t<topic>`` for a group. Only ever sent to the gateway."""
    if dest.dm:
        return f"u{dest.chat}"
    return f"c{dest.chat}" if dest.topic is None else f"c{dest.chat}t{dest.topic}"


def shift_keys(now: datetime, slot: str, parts: int, dest: Destination) -> list[str]:
    """``shift-<date>-<slot>-<dest>`` for part 1, then ``-2``, ``-3``... The date is Asia/Manila."""
    base = f"shift-{now.astimezone(MANILA).date().isoformat()}-{slot}-{destination_tag(dest)}"
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
    if target is not None:
        # A rerun reuses the first run's parts, so changed data never adds parts to a report already (partly)
        # sent; keys are the same, the gateway drops accepted parts, and a part it rejected earlier is queued now.
        report = replace(report, messages=keep_report_parts(conn, now, slot, report.messages))
        parts = len(report.messages)
    destinations = () if target is None else tuple(target.destinations.get(slot) or ())
    # One line per run: counts only, never message text, ids, keys or secrets.
    head = (f"shift slot={slot} snapshot={current.snapshot_id} baseline={current.is_baseline} "
            + ("" if target is None else f"destinations={len(destinations)} ") +
            f"unfinished={unfinished} received={received} processed={processed} "
            f"accomplished={accomplished} parts={parts} "
            f"summaries={stats.new} cached={stats.cached} fallback={stats.fallback}")
    if target is None:
        log(head)
        for number, text in enumerate(report.messages, 1):
            out(f"--- part {number}/{parts} ---\n{text}\n")
        return report
    if not destinations:
        log(f"{head} undelivered=0")
        log(f"shift slot={slot} has no destinations; not sent")
        return report
    results = tuple(_send_to(target.gateway, d, report.messages, shift_keys(now, slot, parts, d)) for d in destinations)
    undelivered = _check_delivery(target, results, clock, sleep)
    if undelivered:
        _send_undelivered_alert(target, slot, now, undelivered, log)
    log(f"{head} undelivered={len(undelivered)}")
    if all(r.rejected for r in results):  # the gateway itself is down: say so, as a single-destination send always did
        raise GatewayError(f"gateway rejected every destination of slot {slot}")
    return report


def _check_delivery(target: ShiftTarget, results: Sequence[DestinationResult], clock, sleep) -> list[tuple[Destination, str]]:
    """Poll every queued ref every ``POLL_EVERY`` until all are sent or failed, or ``POLL_FOR`` is up (stopping early
    when all are final). Returns the undelivered destinations in config order with their state: ``rejected`` (the
    gateway refused it at send), ``failed``, ``still queued`` or ``unknown`` (the status call never answered)."""
    state = {ref: "queued" for r in results for ref in r.refs}
    give_up = clock() + POLL_FOR
    while True:
        for ref in [ref for ref, st in state.items() if st not in ("sent", "failed")]:
            state[ref] = target.gateway.delivery_status(ref)
        if all(st in ("sent", "failed") for st in state.values()) or clock() + POLL_EVERY > give_up:
            break
        sleep(POLL_EVERY.total_seconds())
    undelivered = []
    for r in results:
        states = {state[ref] for ref in r.refs}
        word = ("rejected" if r.rejected else "failed" if "failed" in states else
                "still queued" if "queued" in states else "unknown" if "unknown" in states else None)
        if word:
            undelivered.append((r.destination, word))
    return undelivered


def _send_undelivered_alert(target: ShiftTarget, slot: str, now: datetime, undelivered, log) -> None:
    """One alert per slot run (key ``shift-<date>-<slot>-undelivered``): the slot and each undelivered destination by
    id with its state. No report text. Goes through the shared alert-destination rule."""
    names = [f"user {d.chat}" if d.dm else f"chat {d.chat}" + ("" if d.topic is None else f" topic {d.topic}")
             for d, _ in undelivered]
    lines = "\n".join(f"{name} ({word})" for name, (_, word) in zip(names, undelivered))
    text = f"🚨 Shift report for slot {slot} was not delivered to {len(undelivered)} destination(s):\n{lines}"
    key = f"shift-{now.astimezone(MANILA).date().isoformat()}-{slot}-undelivered"
    try:
        target.send_alert(slot, text, key, log)
    except GatewayError:
        log(f"shift slot={slot} undelivered alert not sent: gateway rejected it")


def _send_to(gateway, dest: Destination, messages: Sequence[str], keys: Sequence[str]) -> DestinationResult:
    """Every part to one destination, in order. A rejection (after the client's retries) ends this destination only."""
    refs: list[int] = []
    for key, text in zip(keys, messages):
        try:
            refs.append(gateway.notify(dest.chat, text, key, dest.topic))
        except GatewayError:
            return DestinationResult(dest, tuple(refs), True)
    return DestinationResult(dest, tuple(refs), False)


def _llm_summarizer(notion: Notion, contacts_ds: str, current: JoSnapshot, llm: LlmClient) -> TaskSummarizer | None:
    """Haiku behind the name masker: Contacts people and the projects on the Job Orders are hidden.
    If the masker cannot be built there is no summarizer, so nothing unmasked is ever sent."""
    try:
        labels = {r["id"]: r["name"] for f in current.pages.values() for r in f.get("Projects") or [] if r.get("name")}
        states = {pid: {"Name": name, "Project Code": name} for pid, name in labels.items()}
        masker = Masker.build(load_contacts(notion, contacts_ds).values(), [states])
    except Exception:
        return None
    return LlmTaskSummarizer(llm, masker)


def _snapshot_with_retry(conn, notion, jo_ds, contacts_ds, projects_ds, slot, now, target, log, clock, sleep) -> None:
    """Read Notion until it answers or ``RETRY_FOR`` is up. Only transport errors count as an outage."""
    give_up = clock() + RETRY_FOR
    attempts = 0
    while True:
        attempts += 1
        try:
            run_jo_snapshot(conn, notion, jo_ds, contacts_ds, projects_ds, slot, now)
            return
        except Exception as exc:  # noqa: BLE001
            if not is_outage(exc):
                raise
            error = error_name(exc)  # the type or Notion's error code; messages may hold data
            if clock() + RETRY_EVERY > give_up:
                break
            sleep(RETRY_EVERY.total_seconds())
    log(f"shift slot={slot} notion unavailable attempts={attempts} error={error}; skipping the slot")
    _send_alert(target, slot, now, error, log)
    raise NotionUnavailable(f"slot {slot} skipped after {attempts} attempts ({error})")


def _send_alert(target: ShiftTarget, slot: str, now: datetime, error: str, log=print) -> None:
    """One ops alert per slot (key ``shift-<date>-<slot>-alert``). Names the slot and the error type only.

    Without ``ops_chat_id`` it goes to the first group entry of the slot's list; with none, it is not sent.
    """
    minutes = int(RETRY_FOR.total_seconds() // 60)
    text = f"🚨 Shift report skipped for slot {slot}: Notion unreachable for {minutes} min (last error: {error})."
    key = f"shift-{now.astimezone(MANILA).date().isoformat()}-{slot}-alert"
    try:
        target.send_alert(slot, text, key, log)
    except GatewayError:
        log(f"shift slot={slot} notion alert not sent: gateway rejected it")
