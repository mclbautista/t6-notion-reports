"""SQLite store for Notion Projects snapshots (long format, one row per field)."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Fields approved by Cyril (2026-11-02): Stage, Status, PM Notes, Deadline, team.
# Names match the Notion Projects schema. Relation fields hold resolved names.
TRACKED_FIELDS = (
    "Name",
    "Project Code",
    "Stage",
    "Status",
    "PM Notes",
    "Deadline",
    "T6 Post Coordinator",
    "T6 Team",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_state (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(snapshot_id),
    page_id     TEXT NOT NULL,
    field       TEXT NOT NULL,
    value_json  TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, page_id, field)
);
"""


@dataclass
class ProjectRecord:
    """One Notion Projects row, normalized. `page_id` is the stable identity."""

    page_id: str
    fields: dict[str, object] = field(default_factory=dict)

    def normalized(self) -> dict[str, object]:
        out: dict[str, object] = {}
        for name in TRACKED_FIELDS:
            value = self.fields.get(name)
            if isinstance(value, str):
                value = " ".join(value.split()) if name != "PM Notes" else value.strip()
            elif isinstance(value, (list, tuple, set)):
                value = sorted(str(v) for v in value)
            out[name] = value
        return out


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def take_snapshot(
    conn: sqlite3.Connection,
    records: list[ProjectRecord],
    taken_at: datetime | None = None,
) -> int:
    taken_at = taken_at or datetime.now(timezone.utc)
    with conn:
        cur = conn.execute("INSERT INTO snapshots (taken_at) VALUES (?)", (taken_at.isoformat(),))
        snapshot_id = cur.lastrowid
        rows = [
            (snapshot_id, rec.page_id, name, json.dumps(value, ensure_ascii=False))
            for rec in records
            for name, value in rec.normalized().items()
        ]
        conn.executemany("INSERT INTO project_state VALUES (?, ?, ?, ?)", rows)
    return snapshot_id


def load_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> dict[str, dict[str, object]]:
    state: dict[str, dict[str, object]] = {}
    for page_id, name, value_json in conn.execute(
        "SELECT page_id, field, value_json FROM project_state WHERE snapshot_id = ?",
        (snapshot_id,),
    ):
        state.setdefault(page_id, {})[name] = json.loads(value_json)
    return state


def latest_snapshot_ids(conn: sqlite3.Connection, n: int = 2) -> list[int]:
    """Newest first."""
    return [r[0] for r in conn.execute(
        "SELECT snapshot_id FROM snapshots ORDER BY snapshot_id DESC LIMIT ?", (n,))]
