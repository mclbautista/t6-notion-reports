"""The one-time switch of the Concerns to the API: back up the database file, then drop the old Concern table.

Run by hand on the host, once, before the first brief that uses the API (owner's yes). The old store is not moved:
the Concerns are rebuilt from the PM Notes, and every age starts on the day of the switch. Rollback: put the previous
image tag back and copy the backup over the database file.
"""
from __future__ import annotations

import os
import sqlite3

BACKUP_SUFFIX = ".pre-concerns-switch"


def switch_concerns(db: str) -> int:
    """Copy ``db`` to ``db + BACKUP_SUFFIX``, then drop its ``concerns`` table. Returns the rows dropped (0 when there
    was no table). Nothing is dropped if the copy cannot be made, or an earlier backup is already there."""
    backup = db + BACKUP_SUFFIX
    if not os.path.exists(db):
        raise FileNotFoundError("no database")
    if os.path.exists(backup):
        raise FileExistsError("a backup of an earlier switch is already there")
    source = sqlite3.connect(db)
    try:
        copy = sqlite3.connect(backup)
        try:
            source.backup(copy)
        finally:
            copy.close()
        if not source.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'concerns'").fetchone():
            return 0
        (rows,) = source.execute("SELECT COUNT(*) FROM concerns").fetchone()
        with source:
            source.execute("DROP TABLE concerns")
        return rows
    finally:
        source.close()
