"""Read the Projects through the t6 API and store one snapshot."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from .snapshots import SnapshotResult, take_snapshot


def run_snapshot(conn: sqlite3.Connection, notion, kind: str, now: datetime) -> SnapshotResult:
    """Archived and Lost Projects are never snapshotted: the API says which (``snapshotted``)."""
    records = [r for r in notion.project_records() if r.rules.snapshotted]
    return take_snapshot(conn, records, now, kind)
