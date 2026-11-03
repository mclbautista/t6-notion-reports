"""Command line: ``python -m t6_psr_brief snapshot``."""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timezone

from .config import ConfigError, load_config
from .notion import NotionClient
from .snapshot_job import run_snapshot
from .diff import diff_snapshots, format_diff
from .schedule import BRIEF_SLOT, MANILA, SLOTS, Scheduler
from .brief_job import BriefFailed, run_brief
from .mom_job import MomAlreadySent, run_draft_mom
from .gateway import GatewayClient
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
    brief = sub.add_parser("brief", help="send the Sunday briefing now")
    brief.add_argument("--config", default="config.yaml")
    brief.add_argument("--db", help="default: database_path from --config")
    brief.add_argument("--dry-print", action="store_true", help="render to stdout, send nothing")
    mom = sub.add_parser("draft-mom", help="draft the PSR minutes in Notion (re-run by hand)")
    mom.add_argument("--config", default="config.yaml")
    mom.add_argument("--db", help="default: database_path from --config")
    mom.add_argument("--date", type=date.fromisoformat, help="meeting date YYYY-MM-DD; default: today (Manila)")
    args = parser.parse_args(argv)

    if args.command == "diff":
        return _diff(args)

    if args.command == "brief":
        return _brief(args, notion, now)

    if args.command == "draft-mom":
        return _draft_mom(args, notion, now)

    if args.command == "serve":
        cfg = _load(args.config)
        if cfg is None:
            return 2
        if cfg.brief and not cfg.brief.psr_secret:
            print("config error: missing environment variable: PSR_SECRET", file=sys.stderr)
            return 2
        notion = notion or NotionClient(cfg.notion_token)
        _ensure_dir(cfg.database_path)
        conn = connect(cfg.database_path)
        gateway = GatewayClient(cfg.brief.gateway_url, cfg.brief.psr_secret) if cfg.brief else None

        print(f"serving db={cfg.database_path} brief={cfg.brief is not None}", flush=True)
        clock = lambda: datetime.now(timezone.utc)
        Scheduler(
            make_job(conn, notion, cfg, gateway, clock),
            clock=clock,
            sleep=time.sleep,
            log=lambda line: print(line, flush=True),
            slots=SLOTS + [BRIEF_SLOT] if cfg.brief else SLOTS,
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


def _brief(args, notion, now) -> int:
    cfg = _load(args.config)
    if cfg is None:
        return 2
    if cfg.brief is None:
        print("config error: missing config key: gateway_url (briefing settings)", file=sys.stderr)
        return 2
    if not args.dry_print and not cfg.brief.psr_secret:
        print("config error: missing environment variable: PSR_SECRET", file=sys.stderr)
        return 2
    db = args.db or cfg.database_path
    if not os.path.exists(db):
        print(f"no database at {db}", file=sys.stderr)
        return 2
    notion = notion or NotionClient(cfg.notion_token)
    gateway = None if args.dry_print else GatewayClient(cfg.brief.gateway_url, cfg.brief.psr_secret)
    conn = connect(db)
    try:
        run_brief(conn, notion, cfg.contacts_data_source, cfg.brief, now or datetime.now(timezone.utc),
                  gateway=gateway, dry_print=args.dry_print,
                  log=lambda text: print(text, file=sys.stderr if args.dry_print else sys.stdout, flush=True))
    except BriefFailed as exc:
        print(f"brief failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def make_job(conn, notion, cfg, gateway, clock):
    """The scheduler's job: Monday = snapshot, then (when configured) the minutes draft."""
    def job(kind: str) -> None:
        moment = clock()
        if kind == "brief":
            run_brief(conn, notion, cfg.contacts_data_source, cfg.brief, moment, gateway=gateway,
                      log=lambda text: print(text, flush=True))
            return
        _snapshot(conn, notion, cfg.projects_data_source, cfg.contacts_data_source, kind, moment)
        if kind == "monday" and cfg.meeting_notes_data_source:
            run_draft_mom(conn, notion, cfg.projects_data_source, cfg.contacts_data_source,
                          cfg.meeting_notes_data_source, moment.astimezone(MANILA).date(),
                          log=lambda text: print(text, flush=True))
    return job


def _draft_mom(args, notion, now) -> int:
    cfg = _load(args.config)
    if cfg is None:
        return 2
    if not cfg.meeting_notes_data_source:
        print("config error: missing config key: meeting_notes_data_source", file=sys.stderr)
        return 2
    db = args.db or cfg.database_path
    if not os.path.exists(db):
        print(f"no database at {db}", file=sys.stderr)
        return 2
    notion = notion or NotionClient(cfg.notion_token)
    meeting_date = args.date or (now or datetime.now(timezone.utc)).astimezone(MANILA).date()
    conn = connect(db)
    try:
        result = run_draft_mom(conn, notion, cfg.projects_data_source, cfg.contacts_data_source,
                               cfg.meeting_notes_data_source, meeting_date, log=lambda t: print(t, flush=True))
    except MomAlreadySent as exc:
        print(f"draft-mom refused: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if result.page_id is None:
        print("no minutes drafted: need a Monday snapshot with an earlier Friday one", file=sys.stderr)
        return 1
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
