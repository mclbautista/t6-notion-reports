"""Command line: ``python -m t6_psr_brief snapshot``."""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

from .config import ConfigError, load_config
from .notion import NotionClient
from .snapshot_job import run_snapshot
from .diff import diff_snapshots, format_diff
from .schedule import MANILA, Scheduler
from .snapshots import KINDS, connect, default_diff_pair, load_snapshot, snapshot_info


def main(argv=None, notion=None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="t6_psr_brief")
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="snapshot the Notion Projects database")
    snap.add_argument("--kind", choices=KINDS, default="manual")
    snap.add_argument("--db", default=os.environ.get("PSR_DB"), help="default: database_path from --config, else data/psr.db")
    snap.add_argument("--config", help="config.yaml; used when the flags below are not given")
    snap.add_argument("--projects-ds", default=os.environ.get("PROJECTS_DS"))
    snap.add_argument("--contacts-ds", default=os.environ.get("CONTACTS_DS"))
    dif = sub.add_parser("diff", help="print what changed between two snapshots")
    dif.add_argument("--db", default=os.environ.get("PSR_DB"), help="default: data/psr.db")
    dif.add_argument("--old", type=int, help="snapshot id; default: latest Monday before the latest Friday")
    dif.add_argument("--new", type=int, help="snapshot id; default: latest Friday")
    serve = sub.add_parser("serve", help="run the scheduler (Mon 11:00 and Fri 18:00 Manila)")
    serve.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)

    if args.command == "diff":
        return _diff(args)

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


def _diff(args) -> int:
    db = args.db or "data/psr.db"
    if not os.path.exists(db):
        print(f"no database at {db}", file=sys.stderr)
        return 2
    conn = connect(db)
    try:
        return _diff_conn(conn, args)
    finally:
        conn.close()


def _diff_conn(conn, args) -> int:
    if args.old is None and args.new is None:
        pair = default_diff_pair(conn)
        if pair is None:
            print("first snapshot, no changes yet (need a Friday snapshot and an earlier Monday one; "
                  "or pass --old and --new)")
            return 0
        old_id, new_id = pair
    elif args.old is None or args.new is None:
        print("pass both --old and --new, or neither", file=sys.stderr)
        return 2
    else:
        old_id, new_id = args.old, args.new
    if not all(conn.execute("SELECT 1 FROM snapshots WHERE snapshot_id = ?", (i,)).fetchone()
               for i in (old_id, new_id)):
        print("unknown snapshot id", file=sys.stderr)
        return 2
    old_info, new_info = snapshot_info(conn, old_id), snapshot_info(conn, new_id)
    old_on, new_on = _local_date(old_info.taken_at), _local_date(new_info.taken_at)
    print(f"diff snapshot {old_id} ({old_info.kind}, {old_on}) → {new_id} ({new_info.kind}, {new_on})")
    print(format_diff(diff_snapshots(load_snapshot(conn, old_id), load_snapshot(conn, new_id), old_on, new_on)))
    return 0


def _local_date(taken_at: str):
    return datetime.fromisoformat(taken_at).astimezone(MANILA).date()


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
