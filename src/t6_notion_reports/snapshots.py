"""SQLite store for Notion Projects snapshots (long format, one row per field).

Layout:
- ``snapshots``: one row per run, with its kind and whether it is the baseline.
- ``snapshot_projects``: one row per project in (or removed from) a snapshot, with a content hash.
  ``state_snapshot_id`` says which snapshot's ``project_state`` rows hold the fields. An unchanged
  project (same hash as the previous snapshot) points at the earlier rows instead of copying them.
- ``project_state``: the long-format field rows.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

KINDS = ("monday", "friday", "manual")

RELATION_FIELDS = (
    "T6 Post Coordinator", "Editors", "Online Editor", "Colorist", "Sound Tech", "Post Supe",
    "Assistant Editor", "Director", "Cinematographer",
)

# Notion's own last-edited time for the page. Stored for the briefing; never reported as a change.
LAST_EDITED = "Last Edited"

# Spec section 3.1. Stored names match the Notion property names.
TRACKED_FIELDS = (
    "Name", "Project Code", "Stage", "Status", "Project Health", "PM Notes",
    "Playdate", "Submission Deadline", "Archival Deadline", LAST_EDITED,
) + RELATION_FIELDS

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at    TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('monday', 'friday', 'manual')),
    is_baseline INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS snapshot_projects (
    snapshot_id       INTEGER NOT NULL REFERENCES snapshots(snapshot_id),
    page_id           TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    state_snapshot_id INTEGER NOT NULL,
    removed           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_id, page_id)
);
CREATE TABLE IF NOT EXISTS project_state (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(snapshot_id),
    page_id     TEXT NOT NULL,
    field       TEXT NOT NULL,
    value_json  TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, page_id, field)
);
CREATE TABLE IF NOT EXISTS concerns (
    page_id     TEXT NOT NULL,
    key         TEXT NOT NULL,
    text        TEXT NOT NULL,
    first_on    TEXT NOT NULL,
    last_on     TEXT NOT NULL,
    resolved_on TEXT,
    PRIMARY KEY (page_id, key)
);
"""


def squash(value: str) -> str:
    """Collapse whitespace runs. Used to compare, never to display."""
    return " ".join(value.split())


@dataclass
class ProjectRecord:
    """One Notion Projects row, normalized. `page_id` is the stable identity.

    Relation fields are lists of ``{"id": ..., "name": ...}``.
    """

    page_id: str
    fields: dict[str, object] = field(default_factory=dict)

    def normalized(self) -> dict[str, object]:
        """What is stored. Status keeps its raw value for display (emoji, odd whitespace)."""
        out: dict[str, object] = {}
        for name in TRACKED_FIELDS:
            value = self.fields.get(name)
            if name == "PM Notes" and isinstance(value, str):
                value = value.strip()
            elif name == "Status":
                pass
            elif isinstance(value, str):
                value = squash(value)
            elif name in RELATION_FIELDS:
                value = sorted(
                    ({"id": str(r["id"]), "name": r.get("name")} for r in value or []),
                    key=lambda r: r["id"],
                )
            out[name] = value
        return out

    def content_hash(self) -> str:
        """Hash of the compare form: whitespace-only edits do not change it."""
        compare = {
            k: (squash(v) if isinstance(v, str) else v)
            for k, v in self.normalized().items()
        }
        blob = json.dumps(compare, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(frozen=True)
class SnapshotInfo:
    snapshot_id: int
    taken_at: str
    kind: str
    is_baseline: bool
    removed_page_ids: list[str]


@dataclass(frozen=True)
class SnapshotResult:
    snapshot_id: int
    is_baseline: bool
    changed: int
    unchanged: int
    removed: int


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def take_snapshot(
    conn: sqlite3.Connection,
    records: list[ProjectRecord],
    taken_at: datetime | None = None,
    kind: str = "manual",
) -> SnapshotResult:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    taken_at = taken_at or datetime.now(timezone.utc)
    previous = latest_snapshot_ids(conn, 1)
    prev_id = previous[0] if previous else None
    prev_rows = {} if prev_id is None else {
        page_id: (h, state_id)
        for page_id, h, state_id in conn.execute(
            "SELECT page_id, content_hash, state_snapshot_id FROM snapshot_projects "
            "WHERE snapshot_id = ? AND removed = 0", (prev_id,))
    }
    changed = unchanged = 0
    with conn:
        cur = conn.execute(
            "INSERT INTO snapshots (taken_at, kind, is_baseline) VALUES (?, ?, ?)",
            (taken_at.isoformat(), kind, int(prev_id is None)))
        snapshot_id = cur.lastrowid
        seen: set[str] = set()
        for rec in records:
            seen.add(rec.page_id)
            h = rec.content_hash()
            prior = prev_rows.get(rec.page_id)
            if prior and prior[0] == h:
                conn.execute("INSERT INTO snapshot_projects VALUES (?, ?, ?, ?, 0)",
                             (snapshot_id, rec.page_id, h, prior[1]))
                unchanged += 1
                continue
            conn.executemany(
                "INSERT INTO project_state VALUES (?, ?, ?, ?)",
                [(snapshot_id, rec.page_id, n, json.dumps(v, ensure_ascii=False))
                 for n, v in rec.normalized().items()])
            conn.execute("INSERT INTO snapshot_projects VALUES (?, ?, ?, ?, 0)",
                         (snapshot_id, rec.page_id, h, snapshot_id))
            changed += 1
        gone = [p for p in prev_rows if p not in seen]
        for page_id in gone:
            h, state_id = prev_rows[page_id]
            conn.execute("INSERT INTO snapshot_projects VALUES (?, ?, ?, ?, 1)",
                         (snapshot_id, page_id, h, state_id))
    return SnapshotResult(snapshot_id, prev_id is None, changed, unchanged, len(gone))


def load_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> dict[str, dict[str, object]]:
    """Active projects only. Removed projects are absent, so diffs report them as removed."""
    state: dict[str, dict[str, object]] = {}
    for page_id, name, value_json in conn.execute(
        "SELECT sp.page_id, ps.field, ps.value_json FROM snapshot_projects sp "
        "JOIN project_state ps ON ps.snapshot_id = sp.state_snapshot_id AND ps.page_id = sp.page_id "
        "WHERE sp.snapshot_id = ? AND sp.removed = 0",
        (snapshot_id,),
    ):
        state.setdefault(page_id, {})[name] = json.loads(value_json)
    return state


def snapshot_info(conn: sqlite3.Connection, snapshot_id: int) -> SnapshotInfo:
    taken_at, kind, baseline = conn.execute(
        "SELECT taken_at, kind, is_baseline FROM snapshots WHERE snapshot_id = ?",
        (snapshot_id,)).fetchone()
    removed = [r[0] for r in conn.execute(
        "SELECT page_id FROM snapshot_projects WHERE snapshot_id = ? AND removed = 1 "
        "ORDER BY page_id", (snapshot_id,))]
    return SnapshotInfo(snapshot_id, taken_at, kind, bool(baseline), removed)


def latest_snapshot_ids(conn: sqlite3.Connection, n: int = 2) -> list[int]:
    """Newest first."""
    return [r[0] for r in conn.execute(
        "SELECT snapshot_id FROM snapshots ORDER BY snapshot_id DESC LIMIT ?", (n,))]


def default_diff_pair(conn: sqlite3.Connection) -> tuple[int, int] | None:
    """(old, new): the Monday snapshot before the latest Friday snapshot, and that Friday.

    None when either does not exist yet.
    """
    friday = conn.execute(
        "SELECT snapshot_id FROM snapshots WHERE kind = 'friday' ORDER BY snapshot_id DESC LIMIT 1"
    ).fetchone()
    if not friday:
        return None
    monday = conn.execute(
        "SELECT snapshot_id FROM snapshots WHERE kind = 'monday' AND snapshot_id < ? "
        "ORDER BY snapshot_id DESC LIMIT 1", (friday[0],)).fetchone()
    return (monday[0], friday[0]) if monday else None


def previous_friday(conn: sqlite3.Connection, friday_id: int) -> int | None:
    """The Friday snapshot before ``friday_id``: the start of the "since the last brief" window."""
    row = conn.execute(
        "SELECT snapshot_id FROM snapshots WHERE kind = 'friday' AND snapshot_id < ? "
        "ORDER BY snapshot_id DESC LIMIT 1", (friday_id,)).fetchone()
    return row[0] if row else None


def mom_diff_pair(conn: sqlite3.Connection, on_or_before: date) -> tuple[int, int] | None:
    """(old, new) for the minutes: the newest Monday snapshot taken (Manila date) on or before
    ``on_or_before``, and the Friday snapshot before it. None when either does not exist yet."""
    from .schedule import MANILA  # local import: schedule does not depend on snapshots

    monday = next((sid for sid, taken_at in conn.execute(
        "SELECT snapshot_id, taken_at FROM snapshots WHERE kind = 'monday' ORDER BY snapshot_id DESC")
        if datetime.fromisoformat(taken_at).astimezone(MANILA).date() <= on_or_before), None)
    if monday is None:
        return None
    friday = conn.execute(
        "SELECT snapshot_id FROM snapshots WHERE kind = 'friday' AND snapshot_id < ? "
        "ORDER BY snapshot_id DESC LIMIT 1", (monday,)).fetchone()
    return (friday[0], monday) if friday else None
