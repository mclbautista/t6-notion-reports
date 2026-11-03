"""Command line: ``python -m t6_psr_brief snapshot``."""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone

from .notion import NotionClient
from .snapshot_job import run_snapshot
from .snapshots import KINDS, connect


def main(argv=None, notion=None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="t6_psr_brief")
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="snapshot the Notion Projects database")
    snap.add_argument("--kind", choices=KINDS, default="manual")
    snap.add_argument("--db", default=os.environ.get("PSR_DB", "data/psr.db"))
    snap.add_argument("--projects-ds", default=os.environ.get("PROJECTS_DS"))
    snap.add_argument("--contacts-ds", default=os.environ.get("CONTACTS_DS"))
    args = parser.parse_args(argv)

    if not (args.projects_ds and args.contacts_ds):
        parser.error("--projects-ds and --contacts-ds are required (or PROJECTS_DS / CONTACTS_DS)")
    notion = notion or NotionClient(os.environ["NOTION_TOKEN"])
    os.makedirs(os.path.dirname(args.db) or ".", exist_ok=True)
    res = run_snapshot(connect(args.db), notion, args.projects_ds, args.contacts_ds,
                       args.kind, now or datetime.now(timezone.utc))
    # One line per run, counts only: never message text or secrets.
    print(f"snapshot id={res.snapshot_id} kind={args.kind} baseline={res.is_baseline} "
          f"changed={res.changed} unchanged={res.unchanged} removed={res.removed}")
    return 0
