"""SQLite store for Notion Job Orders snapshots (long format, one row per field).

Same pattern as ``snapshots.py`` but in its own tables, so the Projects store is untouched:
- ``jo_snapshots``: one row per run, with its kind and whether it is the baseline.
- ``jo_snapshot_pages``: one row per page in (or removed from) a snapshot, with a content hash.
  ``state_snapshot_id`` says which snapshot's ``jo_page_state`` rows hold the fields. An unchanged
  page (same hash as the previous snapshot) points at the earlier rows instead of copying them.
- ``jo_page_state``: the long-format field rows.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .snapshots import squash

MANILA = ZoneInfo("Asia/Manila")
# 1200 and 1800 are the retired slot kinds: old rows keep them, new snapshots no longer take them.
KINDS = ("0500", "1130", "1900", "1200", "1800", "manual")
RETENTION_DAYS = 90

# Spec section 3. Note the trailing space in "Blocker ": it is Notion's property name.
RELATION_FIELDS = ("Assigned", "Projects")
DATE_FIELDS = ("Date", "Deadline")
TRACKED_FIELDS = (
    "Name", "JO Number", "Task", "Date", "Deadline", "Status", "Department",
    "Blocker ", "Health",
) + RELATION_FIELDS + ("last_edited_time",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jo_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at    TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('0500', '1130', '1900', '1200', '1800', 'manual')),
    is_baseline INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jo_snapshot_pages (
    snapshot_id       INTEGER NOT NULL REFERENCES jo_snapshots(snapshot_id),
    page_id           TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    state_snapshot_id INTEGER NOT NULL,
    removed           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_id, page_id)
);
CREATE TABLE IF NOT EXISTS jo_page_state (
    snapshot_id INTEGER NOT NULL REFERENCES jo_snapshots(snapshot_id),
    page_id     TEXT NOT NULL,
    field       TEXT NOT NULL,
    value_json  TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, page_id, field)
);
CREATE TABLE IF NOT EXISTS shift_report_parts (
    report_date TEXT NOT NULL,
    slot        TEXT NOT NULL,
    parts_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (report_date, slot)
);
CREATE TABLE IF NOT EXISTS jo_task_summaries (
    page_id      TEXT NOT NULL,
    task_hash    TEXT NOT NULL,
    summary      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    PRIMARY KEY (page_id, task_hash)
);
"""


def instant(text: str) -> datetime:
    """An ISO timestamp as an aware instant, so values written at different offsets compare by time.
    A value with no offset is read as UTC."""
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def manila_day(moment: datetime) -> str:
    """The Asia/Manila calendar date of an instant, as ISO text."""
    return moment.astimezone(MANILA).date().isoformat()


def keep_report_parts(conn: sqlite3.Connection, now: datetime, slot: str, parts: list[str]) -> list[str]:
    """The parts a slot sends for its Manila date: the first run's, which stay in the database. Later runs
    of the same date and slot get those back, whatever ``parts`` they just built."""
    day = manila_day(now)
    with conn:
        conn.execute("INSERT OR IGNORE INTO shift_report_parts VALUES (?, ?, ?, ?)",
                     (day, slot, json.dumps(parts, ensure_ascii=False), now.isoformat()))
        (stored,) = conn.execute("SELECT parts_json FROM shift_report_parts WHERE report_date = ? AND slot = ?",
                                 (day, slot)).fetchone()
    return json.loads(stored)


def manila_instant(start: str | None, as_deadline: bool, time_zone: str | None = None) -> str | None:
    """Spec 3.1: an instant stays that instant (written at +08:00); a date-only value becomes
    23:59 Manila (Deadline) or 00:00 Manila (Date); a datetime with no offset is Manila time."""
    if not start:
        return None
    if len(start) == 10:
        day = date.fromisoformat(start)
        return datetime.combine(day, time(23, 59) if as_deadline else time(0, 0), MANILA).isoformat()
    moment = datetime.fromisoformat(start)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(time_zone) if time_zone else MANILA)
    return moment.astimezone(MANILA).isoformat()


@dataclass
class JoRecord:
    """One Notion Job Orders row, normalized. `page_id` is the stable identity.

    Relation fields are lists of ``{"id": ..., "name": ...}``.
    """

    page_id: str
    fields: dict[str, object] = field(default_factory=dict)

    def normalized(self) -> dict[str, object]:
        out: dict[str, object] = {}
        for name in TRACKED_FIELDS:
            value = self.fields.get(name)
            if name in RELATION_FIELDS:
                value = sorted(
                    ({"id": str(r["id"]), "name": r.get("name")} for r in value or []),
                    key=lambda r: r["id"],
                )
            elif name == "Health":
                pass  # exactly as read
            elif isinstance(value, str) and name not in DATE_FIELDS and name != "last_edited_time":
                value = squash(value)
            out[name] = value
        return out

    def content_hash(self) -> str:
        """Covers every tracked field including Health, which changes with time alone."""
        blob = json.dumps(self.normalized(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(frozen=True)
class JoSnapshotResult:
    snapshot_id: int
    is_baseline: bool
    changed: int
    unchanged: int
    removed: int
    pruned: int  # snapshots deleted by retention


@dataclass(frozen=True)
class JoSnapshot:
    """A loaded snapshot: active (not removed) pages only, each as its normalized fields."""

    snapshot_id: int
    taken_at: datetime
    kind: str
    is_baseline: bool
    pages: dict[str, dict[str, object]]


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    _widen_kind_check(conn)
    return conn


def _widen_kind_check(conn: sqlite3.Connection) -> None:
    """Databases made before the 11:30 and 19:00 slots have a CHECK without those kinds, and SQLite
    cannot alter a CHECK in place, so rebuild ``jo_snapshots`` once and keep every row."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='jo_snapshots'").fetchone()
    if row is None or "'1130'" in row[0]:
        return
    conn.executescript(
        "BEGIN;"
        + SCHEMA.split(";")[0].replace("jo_snapshots (", "jo_snapshots_new (").replace("IF NOT EXISTS ", "")
        + ";INSERT INTO jo_snapshots_new SELECT snapshot_id, taken_at, kind, is_baseline FROM jo_snapshots;"
        "DROP TABLE jo_snapshots;ALTER TABLE jo_snapshots_new RENAME TO jo_snapshots;COMMIT;")


def take_snapshot(
    conn: sqlite3.Connection,
    records: list[JoRecord],
    taken_at: datetime | None = None,
    kind: str = "manual",
) -> JoSnapshotResult:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    taken_at = taken_at or datetime.now(timezone.utc)
    prev = conn.execute("SELECT MAX(snapshot_id) FROM jo_snapshots").fetchone()[0]
    prev_rows = {} if prev is None else {
        page_id: (h, state_id)
        for page_id, h, state_id in conn.execute(
            "SELECT page_id, content_hash, state_snapshot_id FROM jo_snapshot_pages "
            "WHERE snapshot_id = ? AND removed = 0", (prev,))
    }
    changed = unchanged = 0
    with conn:
        cur = conn.execute(
            "INSERT INTO jo_snapshots (taken_at, kind, is_baseline) VALUES (?, ?, ?)",
            (taken_at.isoformat(), kind, int(prev is None)))
        snapshot_id = cur.lastrowid
        seen: set[str] = set()
        for rec in records:
            seen.add(rec.page_id)
            h = rec.content_hash()
            prior = prev_rows.get(rec.page_id)
            if prior and prior[0] == h:
                conn.execute("INSERT INTO jo_snapshot_pages VALUES (?, ?, ?, ?, 0)",
                             (snapshot_id, rec.page_id, h, prior[1]))
                unchanged += 1
                continue
            conn.executemany(
                "INSERT INTO jo_page_state VALUES (?, ?, ?, ?)",
                [(snapshot_id, rec.page_id, n, json.dumps(v, ensure_ascii=False))
                 for n, v in rec.normalized().items()])
            conn.execute("INSERT INTO jo_snapshot_pages VALUES (?, ?, ?, ?, 0)",
                         (snapshot_id, rec.page_id, h, snapshot_id))
            changed += 1
        gone = [p for p in prev_rows if p not in seen]
        for page_id in gone:
            h, state_id = prev_rows[page_id]
            conn.execute("INSERT INTO jo_snapshot_pages VALUES (?, ?, ?, ?, 1)",
                         (snapshot_id, page_id, h, state_id))
        pruned = prune(conn, taken_at)
    return JoSnapshotResult(snapshot_id, prev is None, changed, unchanged, len(gone), pruned)


def prune(conn: sqlite3.Connection, now: datetime) -> int:
    """Delete snapshots older than 90 days, except rows a newer snapshot still points at.

    Returns how many snapshots were deleted outright. Call inside a transaction.
    """
    cutoff = now - timedelta(days=RETENTION_DAYS)
    # Task summaries not used by any report for 90 days go with the snapshots.
    # Compared as instants: the stamps are ISO strings that can carry different offsets.
    stale = [(page_id, task_hash) for page_id, task_hash, used in conn.execute(
        "SELECT page_id, task_hash, last_used_at FROM jo_task_summaries") if instant(used) < cutoff]
    conn.executemany("DELETE FROM jo_task_summaries WHERE page_id = ? AND task_hash = ?", stale)
    conn.execute("DELETE FROM shift_report_parts WHERE report_date < ?", (manila_day(cutoff),))
    old = [sid for sid, taken_at in conn.execute("SELECT snapshot_id, taken_at FROM jo_snapshots")
           if instant(taken_at) < cutoff]
    if not old:
        return 0
    marks = ",".join("?" * len(old))
    conn.execute(f"DELETE FROM jo_snapshot_pages WHERE snapshot_id IN ({marks})", old)
    # State rows survive only while some remaining page row points at them; the same goes for
    # the snapshot header they hang off.
    conn.execute("DELETE FROM jo_page_state WHERE (snapshot_id, page_id) NOT IN "
                 "(SELECT state_snapshot_id, page_id FROM jo_snapshot_pages)")
    cur = conn.execute(
        f"DELETE FROM jo_snapshots WHERE snapshot_id IN ({marks}) AND snapshot_id NOT IN "
        "(SELECT state_snapshot_id FROM jo_snapshot_pages)", old)
    return cur.rowcount


def load_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> JoSnapshot:
    """Load one snapshot with each active page's normalized fields. Removed pages are absent."""
    row = conn.execute(
        "SELECT taken_at, kind, is_baseline FROM jo_snapshots WHERE snapshot_id = ?",
        (snapshot_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown Job Orders snapshot: {snapshot_id}")
    pages: dict[str, dict[str, object]] = {}
    for page_id, name, value_json in conn.execute(
        "SELECT sp.page_id, ps.field, ps.value_json FROM jo_snapshot_pages sp "
        "JOIN jo_page_state ps ON ps.snapshot_id = sp.state_snapshot_id AND ps.page_id = sp.page_id "
        "WHERE sp.snapshot_id = ? AND sp.removed = 0",
        (snapshot_id,),
    ):
        pages.setdefault(page_id, {})[name] = json.loads(value_json)
    return JoSnapshot(snapshot_id, datetime.fromisoformat(row[0]), row[1], bool(row[2]), pages)


def load_latest(conn: sqlite3.Connection) -> JoSnapshot | None:
    """The newest Job Orders snapshot, or None before the first run."""
    row = conn.execute("SELECT MAX(snapshot_id) FROM jo_snapshots").fetchone()
    return None if row[0] is None else load_snapshot(conn, row[0])


def load_bounding(
    conn: sqlite3.Connection, at_or_before: datetime, kinds: tuple[str, ...] | None = None,
) -> JoSnapshot | None:
    """The latest snapshot (by time, not insertion order) taken at or before ``at_or_before``: the one that bounds a window.

    ``kinds`` narrows it (e.g. ``("1900",)``). None when there is no such snapshot.
    """
    eligible = [
        (instant(taken_at), sid) for sid, taken_at, kind in conn.execute(
            "SELECT snapshot_id, taken_at, kind FROM jo_snapshots")
        if (kinds is None or kind in kinds) and instant(taken_at) <= at_or_before]
    # Latest by actual time (offsets differ, rows can be inserted out of order); the id breaks ties.
    return load_snapshot(conn, max(eligible)[1]) if eligible else None
