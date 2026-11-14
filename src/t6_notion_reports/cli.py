"""Command line: ``python -m t6_notion_reports snapshot``."""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timezone

from .config import ConfigError, load_config
from .notion import NotionClient
from .snapshot_job import run_snapshot
from .jo_snapshot_job import run_jo_snapshot
from . import jo_snapshots
from .shift_job import NotionUnavailable, ShiftTarget, run_shift_report
from .shift_report import SLOTS as SHIFT_SLOTS
from .diff import diff_snapshots, format_diff
from .schedule import BRIEF_SLOT, MANILA, SEND_MOM_SLOT, SHIFT_SLOTS as SHIFT_JOB_SLOTS, SLOTS, Scheduler
from .brief_job import BriefFailed, run_brief
from .llm import OpenRouterClient, UnavailableLlm
from .mom_job import MomAlreadySent, run_draft_mom
from .mom_send import MomSendFailed, run_send_ready_mom
from .gateway import GatewayClient, GatewayError
from .snapshots import KINDS, connect, default_diff_pair, load_snapshot, snapshot_info


def main(argv=None, notion=None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="t6_notion_reports")
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="snapshot the Notion Projects database")
    snap.add_argument("--kind", choices=KINDS, default="manual")
    snap.add_argument("--db", default=os.environ.get("PSR_DB"), help="default: database_path from --config, else data/psr.db")
    snap.add_argument("--config", help="config.yaml; used when the flags below are not given")
    snap.add_argument("--projects-ds", default=os.environ.get("PROJECTS_DS"))
    snap.add_argument("--contacts-ds", default=os.environ.get("CONTACTS_DS"))
    jo = sub.add_parser("jo-snapshot", help="snapshot the Notion Job Orders database")
    jo.add_argument("--kind", choices=jo_snapshots.KINDS, default="manual")
    jo.add_argument("--config", default="config.yaml")
    jo.add_argument("--db", help="default: database_path from --config")
    shift = sub.add_parser("shift-report", help="build one 11:30, 19:00 or 05:00 Job Orders report")
    shift.add_argument("--slot", choices=sorted(SHIFT_SLOTS), required=True)
    shift.add_argument("--config", default="config.yaml")
    shift.add_argument("--db", help="default: database_path from --config")
    shift.add_argument("--dry-print", action="store_true", help="render to stdout, send nothing")
    dif = sub.add_parser("diff", help="print what changed between two snapshots")
    dif.add_argument("--db", default=os.environ.get("PSR_DB"), help="default: data/psr.db")
    dif.add_argument("--old", type=int, help="snapshot id; default: latest Monday before the latest Friday")
    dif.add_argument("--new", type=int, help="snapshot id; default: latest Friday")
    serve = sub.add_parser("serve", help="run the scheduler (Mon 11:00, Fri 23:30 and Sat 12:00 Manila)")
    serve.add_argument("--config", default="config.yaml")
    brief = sub.add_parser("brief", help="send the Saturday briefing now")
    brief.add_argument("--config", default="config.yaml")
    brief.add_argument("--db", help="default: database_path from --config")
    brief.add_argument("--dry-print", action="store_true", help="render to stdout, send nothing")
    brief.add_argument("--show-llm-input", action="store_true",
                       help="with --dry-print: print each project's full LLM payload and call no LLM")
    mom = sub.add_parser("draft-mom", help="draft the PSR minutes in Notion (re-run by hand)")
    mom.add_argument("--config", default="config.yaml")
    mom.add_argument("--db", help="default: database_path from --config")
    mom.add_argument("--date", type=date.fromisoformat, help="meeting date YYYY-MM-DD; default: today (Manila)")
    send = sub.add_parser("send-ready-mom", help="send approved minutes (Ready to send ticked) to the group, once")
    send.add_argument("--config", default="config.yaml")
    send.add_argument("--dry-print", action="store_true", help="render to stdout, send nothing, leave Sent at empty")
    args = parser.parse_args(argv)

    if args.command == "diff":
        return _diff(args)

    if args.command == "jo-snapshot":
        return _jo_snapshot(args, notion, now)

    if args.command == "shift-report":
        return _shift_report(args, notion, now)

    if args.command == "brief":
        return _brief(args, notion, now)

    if args.command == "draft-mom":
        return _draft_mom(args, notion, now)

    if args.command == "send-ready-mom":
        return _send_ready_mom(args, notion, now)

    if args.command == "serve":
        cfg = _load(args.config)
        if cfg is None:
            return 2
        for name, value in _brief_secrets(cfg) + _shift_secrets(cfg):
            if not value:
                print(f"config error: missing environment variable: {name}", file=sys.stderr)
                return 2
        notion = notion or NotionClient(cfg.notion_token)
        _ensure_dir(cfg.database_path)
        conn = connect(cfg.database_path)
        gateway = GatewayClient(cfg.brief.gateway_url, cfg.brief.psr_secret) if cfg.brief else None

        print(f"serving db={cfg.database_path} brief={cfg.brief is not None} shift={cfg.shift is not None}",
              flush=True)
        clock = lambda: datetime.now(timezone.utc)
        Scheduler(
            make_job(conn, notion, cfg, gateway, clock),
            clock=clock,
            sleep=time.sleep,
            log=lambda line: print(line, flush=True),
            slots=_slots(cfg),
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


def _jo_snapshot(args, notion, now) -> int:
    cfg = _load(args.config)
    if cfg is None:
        return 2
    if not cfg.jo_data_source:
        print("config error: missing config key: jo_data_source", file=sys.stderr)
        return 2
    notion = notion or NotionClient(cfg.notion_token)
    db = args.db or cfg.database_path
    _ensure_dir(db)
    conn = jo_snapshots.connect(db)
    try:
        res = run_jo_snapshot(conn, notion, cfg.jo_data_source, cfg.contacts_data_source,
                              cfg.projects_data_source, args.kind, now or datetime.now(timezone.utc))
    finally:
        conn.close()
    # One line per run, counts only: never field values or secrets.
    print(f"jo-snapshot id={res.snapshot_id} kind={args.kind} baseline={res.is_baseline} "
          f"changed={res.changed} unchanged={res.unchanged} removed={res.removed} pruned={res.pruned}",
          flush=True)
    return 0


def _shift_report(args, notion, now) -> int:
    cfg = _load(args.config)
    if cfg is None:
        return 2
    if not cfg.jo_data_source:
        print("config error: missing config key: jo_data_source", file=sys.stderr)
        return 2
    target = None
    if not args.dry_print:
        if cfg.shift is None:
            print("config error: shift_reports_enabled is not true; use --dry-print or enable it", file=sys.stderr)
            return 2
        for name, value in _shift_secrets(cfg):
            if not value:
                print(f"config error: missing environment variable: {name}", file=sys.stderr)
                return 2
        target = _shift_target(cfg)
    notion = notion or NotionClient(cfg.notion_token)
    db = args.db or cfg.database_path
    _ensure_dir(db)
    conn = jo_snapshots.connect(db)
    try:
        run_shift_report(conn, notion, cfg.jo_data_source, cfg.contacts_data_source, cfg.projects_data_source,
                         args.slot, now or datetime.now(timezone.utc), banner=_shift_banner(cfg),
                         log=lambda text: print(text, file=sys.stderr, flush=True), target=target,
                         llm=_shift_llm(cfg))
    except (GatewayError, NotionUnavailable) as exc:
        print(f"shift-report failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def _shift_banner(cfg) -> bool:
    return cfg.shift.test_banner if cfg.shift else bool(cfg.brief and cfg.brief.test_banner)


def _shift_secrets(cfg):
    if cfg.shift is None:
        return []
    return [("SHIFT_SECRET", cfg.shift.shift_secret)]


def _shift_target(cfg) -> ShiftTarget:
    """A gateway client authenticated with SHIFT_SECRET, never PSR_SECRET."""
    shift = cfg.shift
    return ShiftTarget(GatewayClient(shift.gateway_url, shift.shift_secret), shift.destinations, shift.ops_chat_id)


def _shift_llm(cfg):
    """``shift_llm_model`` for the task summaries when the OpenRouter key is set; None means the first three words."""
    if cfg.shift is not None and cfg.shift.openrouter_api_key:
        return OpenRouterClient(cfg.shift.openrouter_api_key, cfg.shift.llm_model)
    return None


def _brief(args, notion, now) -> int:
    cfg = _load(args.config)
    if cfg is None:
        return 2
    if cfg.brief is None:
        print("config error: missing config key: gateway_url (briefing settings)", file=sys.stderr)
        return 2
    if args.show_llm_input and not args.dry_print:
        print("config error: --show-llm-input needs --dry-print", file=sys.stderr)
        return 2
    if not args.dry_print:
        for name, value in _brief_secrets(cfg):
            if not value:
                print(f"config error: missing environment variable: {name}", file=sys.stderr)
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
                  gateway=gateway, dry_print=args.dry_print, llm=_llm(cfg, args.dry_print),
                  meeting_notes_ds=cfg.meeting_notes_data_source, show_llm_input=args.show_llm_input,
                  projects_ds=cfg.projects_data_source, matching=cfg.matching,
                  log=lambda text: print(text, file=sys.stderr if args.dry_print else sys.stdout, flush=True))
    except BriefFailed as exc:
        print(f"brief failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def _slots(cfg):
    slots = SLOTS + [BRIEF_SLOT] if cfg.brief else list(SLOTS)
    if cfg.brief and cfg.meeting_notes_data_source:
        slots.append(SEND_MOM_SLOT)
    if cfg.shift:
        slots += SHIFT_JOB_SLOTS
    return slots


def _brief_secrets(cfg):
    if cfg.brief is None:
        return []
    return [("PSR_SECRET", cfg.brief.psr_secret)]


def _llm(cfg, dry_print=False):
    """No key: a dry print leaves the LLM out (raw changes); a real send marks every project "(LLM unavailable)"."""
    if cfg.brief is None:
        return None
    if cfg.brief.openrouter_api_key:
        return OpenRouterClient(cfg.brief.openrouter_api_key, cfg.brief.llm_model)
    return None if dry_print else UnavailableLlm()


def make_job(conn, notion, cfg, gateway, clock, shift_target=None):
    """The scheduler's job: Monday = snapshot, then (when configured) the minutes draft."""
    def job(kind: str) -> None:
        moment = clock()
        if kind.startswith("shift"):
            conn_jo = jo_snapshots.connect(cfg.database_path)
            try:
                run_shift_report(conn_jo, notion, cfg.jo_data_source, cfg.contacts_data_source,
                                 cfg.projects_data_source, kind[len("shift"):], moment,
                                 banner=cfg.shift.test_banner, log=lambda text: print(text, flush=True),
                                 target=_shift_target(cfg) if shift_target is None else shift_target,
                                 llm=_shift_llm(cfg))
            finally:
                conn_jo.close()
            return
        if kind == "send_mom":
            run_send_ready_mom(notion, cfg.meeting_notes_data_source, gateway, cfg.brief.group_chat_id,
                               cfg.brief.group_topic_id, moment, banner=cfg.brief.test_banner,
                               log=lambda text: print(text, flush=True))
            return
        if kind == "brief":
            run_brief(conn, notion, cfg.contacts_data_source, cfg.brief, moment, gateway=gateway, llm=_llm(cfg),
                      meeting_notes_ds=cfg.meeting_notes_data_source, projects_ds=cfg.projects_data_source,
                      matching=cfg.matching, log=lambda text: print(text, flush=True))
            return
        _snapshot(conn, notion, cfg.projects_data_source, cfg.contacts_data_source, kind, moment)
        if kind == "monday" and cfg.meeting_notes_data_source:
            run_draft_mom(conn, notion, cfg.projects_data_source, cfg.contacts_data_source,
                          cfg.meeting_notes_data_source, moment.astimezone(MANILA).date(),
                          log=lambda text: print(text, flush=True), llm=_llm(cfg))
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
                               cfg.meeting_notes_data_source, meeting_date, log=lambda t: print(t, flush=True),
                               llm=_llm(cfg))
    except MomAlreadySent as exc:
        print(f"draft-mom refused: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if result.page_id is None:
        print("no minutes drafted: need a Monday snapshot with an earlier Friday one", file=sys.stderr)
        return 1
    return 0


def _send_ready_mom(args, notion, now) -> int:
    cfg = _load(args.config)
    if cfg is None:
        return 2
    if cfg.brief is None:
        print("config error: missing config key: gateway_url (briefing settings)", file=sys.stderr)
        return 2
    if not cfg.meeting_notes_data_source:
        print("config error: missing config key: meeting_notes_data_source", file=sys.stderr)
        return 2
    if not args.dry_print:
        for name, value in _brief_secrets(cfg):
            if not value:
                print(f"config error: missing environment variable: {name}", file=sys.stderr)
                return 2
    notion = notion or NotionClient(cfg.notion_token)
    gateway = None if args.dry_print else GatewayClient(cfg.brief.gateway_url, cfg.brief.psr_secret)
    try:
        run_send_ready_mom(notion, cfg.meeting_notes_data_source, gateway, cfg.brief.group_chat_id,
                           cfg.brief.group_topic_id, now or datetime.now(timezone.utc),
                           banner=cfg.brief.test_banner, dry_print=args.dry_print,
                           log=lambda text: print(text, file=sys.stderr if args.dry_print else sys.stdout, flush=True))
    except MomSendFailed as exc:
        print(f"send-ready-mom failed: {exc}", file=sys.stderr)
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
