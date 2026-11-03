"""Command line: ``python -m t6_psr_brief snapshot``."""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

from .config import ConfigError, load_config
from .notion import NotionClient
from .schedule import Scheduler
from .snapshot_job import run_snapshot
from .snapshots import KINDS, connect


def main(argv=None, notion=None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="t6_psr_brief")
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="snapshot the Notion Projects database")
    snap.add_argument("--kind", choices=KINDS, default="manual")
    snap.add_argument("--db", default=os.environ.get("PSR_DB"), help="default: database_path from --config, else data/psr.db")
    snap.add_argument("--config", help="config.yaml; used when the flags below are not given")
    snap.add_argument("--projects-ds", default=os.environ.get("PROJECTS_DS"))
    snap.add_argument("--contacts-ds", default=os.environ.get("CONTACTS_DS"))
    serve = sub.add_parser("serve", help="run the scheduler (Mon 11:00 and Fri 18:00 Manila)")
    serve.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)

    if args.command == "serve":
        cfg = _load(args.config)
        if cfg is None:
            return 2
        notion = notion or NotionClient(cfg.notion_token)
        _ensure_dir(cfg.database_path)
        conn = connect(cfg.database_path)
        print(f"serving db={cfg.database_path}", flush=True)
        Scheduler(
            lambda kind: _snapshot(conn, notion, cfg.projects_data_source,
                                   cfg.contacts_data_source, kind, datetime.now(timezone.utc)),
            clock=lambda: datetime.now(timezone.utc),
            sleep=time.sleep,
            log=lambda line: print(line, flush=True),
        ).run()
        return 0

    projects_ds, contacts_ds, db = args.projects_ds, args.contacts_ds, args.db
    if args.config:
        cfg = _load(args.config)
        if cfg is None:
            return 2
        projects_ds = projects_ds or cfg.projects_data_source
        contacts_ds = contacts_ds or cfg.contacts_data_source
        notion = notion or NotionClient(cfg.notion_token)
        db = db or cfg.database_path
    db = db or "data/psr.db"
    if not (projects_ds and contacts_ds):
        parser.error("--config, or --projects-ds and --contacts-ds (or PROJECTS_DS / CONTACTS_DS), is required")
    notion = notion or NotionClient(os.environ["NOTION_TOKEN"])
    _ensure_dir(db)
    _snapshot(connect(db), notion, projects_ds, contacts_ds, args.kind, now or datetime.now(timezone.utc))
    return 0


def _load(path: str):
    try:
        return load_config(path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return None


def _ensure_dir(db: str) -> None:
    os.makedirs(os.path.dirname(db) or ".", exist_ok=True)


def _snapshot(conn, notion, projects_ds, contacts_ds, kind, now) -> None:
    res = run_snapshot(conn, notion, projects_ds, contacts_ds, kind, now)
    # One line per run, counts only: never message text or secrets.
    print(f"snapshot id={res.snapshot_id} kind={kind} baseline={res.is_baseline} "
          f"changed={res.changed} unchanged={res.unchanged} removed={res.removed}", flush=True)
