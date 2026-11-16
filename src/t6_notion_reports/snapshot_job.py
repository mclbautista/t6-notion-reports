"""Read the Projects through the t6 API and store one snapshot."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from .snapshots import SnapshotResult, take_snapshot

# Never snapshotted (spec section 4). Matched on the word, ignoring emoji and whitespace.
EXCLUDED_STATUSES = {"archived", "lost"}


def _is_excluded(status: object) -> bool:
    return re.sub(r"[^\w\s]", "", str(status or "")).strip().lower() in EXCLUDED_STATUSES


def run_snapshot(conn: sqlite3.Connection, notion, kind: str, now: datetime) -> SnapshotResult:
    records = [r for r in notion.project_records() if not _is_excluded(r.fields.get("Status"))]
    return take_snapshot(conn, records, now, kind)
