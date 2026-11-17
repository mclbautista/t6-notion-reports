"""Read the Job Orders through the t6 API and store one snapshot."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from .jo_snapshots import JoSnapshotResult, take_snapshot


def run_jo_snapshot(conn: sqlite3.Connection, notion, kind: str, now: datetime) -> JoSnapshotResult:
    return take_snapshot(conn, notion.job_order_records(), now, kind)
