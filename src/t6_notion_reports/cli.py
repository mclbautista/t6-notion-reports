"""Command line: ``python -m t6_notion_reports snapshot``."""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timezone

from .config import ConfigError, load_config
from .t6_api import ApiNotion, MaskedRead, client_from_env
from .snapshot_job import run_snapshot
from .jo_snapshot_job import run_jo_snapshot
from . import jo_snapshots
from .shift_job import SlotSkipped, ShiftTarget, run_shift_report
from .shift_report import SLOTS as SHIFT_SLOTS
from .diff import diff_snapshots, format_diff
from .schedule import BRIEF_SLOT, MANILA, SEND_MOM_SLOT, SHIFT_SLOTS as SHIFT_JOB_SLOTS, SLOTS, Scheduler
from .brief_job import BriefFailed, run_brief
from .llm_api import LLM_TIMEOUT, ApiLlm, UnavailableLlm
from .minutes_api import MinutesApiError
from .mom_job import MomAlreadySent, run_draft_mom
from .mom_send import MomSendFailed, run_send_ready_mom
from .concern_switch import BACKUP_SUFFIX, switch_concerns
from .gateway import GatewayClient, GatewayError
from .snapshots import KINDS, connect, default_diff_pair, load_snapshot, snapshot_info


def main(argv=None, notion=None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="t6_notion_reports")
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="snapshot the Projects, read through the t6 API")
    snap.add_argument("--kind", choices=KINDS, default="manual")
    snap.add_argument("--db", default=os.environ.get("PSR_DB"), help="default: database_path from --config, else data/psr.db")
    snap.add_argument("--config", help="config.yaml; used when the flags below are not given")
    jo = sub.add_parser("jo-snapshot", help="snapshot the Job Orders, read through the t6 API")
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
    serve = sub.add_parser("serve", help="run the scheduler (Mon 11:00, Tue 23:30 and Wed 10:00 Manila)")
    serve.add_argument("--config", default="config.yaml")
    brief = sub.add_parser("brief", help="send the Wednesday brief now")
    brief.add_argument("--config", default="config.yaml")
    brief.add_argument("--db", help="default: database_path from --config")
    brief.add_argument("--dry-print", action="store_true", help="render to stdout, send nothing")
    brief.add_argument("--show-llm-input", action="store_true",
                       help="with --dry-print: print each project's full LLM payload and call no LLM")
    mom = sub.add_parser("draft-mom", help="draft the PSR minutes in Notion (re-run by hand)")
    mom.add_argument("--config", default="config.yaml")
    mom.add_argument("--db", help="default: database_path from --config")
    mom.add_argument("--date", type=date.fromisoformat, help="meeting date YYYY-MM-DD; default: today (Manila)")
    mom.add_argument("--dry-print", action="store_true", help="condense and print the bullets per project, write nothing to Notion")
    switch = sub.add_parser("switch-concerns", help="once, before the first brief that uses the API: back up the database, drop the old Concern table")
    switch.add_argument("--config", default="config.yaml")
    switch.add_argument("--db", help="default: database_path from --config")
    send = sub.add_parser("send-ready-mom", help="send approved minutes (Ready to send ticked) to the group, once")
    send.add_argument("--config", default="config.yaml")
    send.add_argument("--dry-print", action="store_true", help="render to stdout, send nothing, leave Sent at empty")
    args = parser.parse_args(argv)
    try:
        return _run(args, parser, notion, now)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except MaskedRead as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        return 1


def _run(args, parser, notion, now) -> int:
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

    if args.command == "switch-concerns":
        return _switch_concerns(args)

    if args.command == "serve":
        cfg = _load(args.config)
        if cfg is None:
            return 2
        for name, value in _brief_secrets(cfg) + _shift_secrets(cfg):
            if not value:
                print(f"config error: missing environment variable: {name}", file=sys.stderr)
                return 2
        notion = _reads(notion)
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

    db = args.db
    if args.config:
        cfg = _load(args.config)
        if cfg is None:
            return 2
        db = db or cfg.database_path
        notion = _reads(notion)
    db = db or "data/psr.db"
    notion = notion or _reads(None)
    _ensure_dir(db)
    _snapshot(connect(db), notion, args.kind, now or datetime.now(timezone.utc))
    return 0


def _reads(notion):
    """What the jobs read from and the Minutes are written through: the t6 API.

    A ``notion`` passed in (the tests' fake) is used as it is. ``T6_API_URL`` and ``T6_API_SECRET`` are required.
    """
    if notion is not None:
        return notion
    client = client_from_env()
    if client is None:
        raise ConfigError("missing environment variable: T6_API_URL")
    return ApiNotion(client)


def _jo_snapshot(args, notion, now) -> int:
    cfg = _load(args.config)
    if cfg is None:
        return 2
    notion = _reads(notion)
    db = args.db or cfg.database_path
    _ensure_dir(db)
    conn = jo_snapshots.connect(db)
    try:
        res = run_jo_snapshot(conn, notion, args.kind, now or datetime.now(timezone.utc))
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
    notion = _reads(notion)
    db = args.db or cfg.database_path
    _ensure_dir(db)
    conn = jo_snapshots.connect(db)
    try:
        run_shift_report(conn, notion, args.slot, now or datetime.now(timezone.utc), banner=_shift_banner(cfg),
                         log=lambda text: print(text, file=sys.stderr, flush=True), target=target,
                         llm=_shift_llm(cfg))
    except (GatewayError, SlotSkipped) as exc:
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
    """The shift report's task summaries through ``llm run`` with ``shift_llm_model``; None (the first three words) when LLM calls are off."""
    if cfg.shift is not None:
        return _api_llm(cfg, "shift_task_summary", cfg.shift.llm_model)
    return None


def _api_llm(cfg, task: str, model: str):
    """An ``ApiLlm`` for one task; None when ``llm_enabled`` is false or ``T6_API_URL`` is unset."""
    client = client_from_env(timeout=LLM_TIMEOUT) if cfg.llm_enabled else None
    if client is None:
        return None
    return ApiLlm(client, task, model, log=lambda text: print(text, file=sys.stderr, flush=True))


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
    notion = _reads(notion)
    gateway = None if args.dry_print else GatewayClient(cfg.brief.gateway_url, cfg.brief.psr_secret)
    conn = connect(db)
    try:
        run_brief(conn, notion, cfg.brief, now or datetime.now(timezone.utc),
                  gateway=gateway, dry_print=args.dry_print, llm=_task_llm(cfg, "brief_consolidation", args.dry_print),
                  meeting_notes_ds=cfg.meeting_notes_data_source, show_llm_input=args.show_llm_input,
                  leaves_llm=_task_llm(cfg, "brief_leaves", args.dry_print),
                  log=lambda text: print(text, file=sys.stderr if args.dry_print else sys.stdout, flush=True))
    except BriefFailed as exc:
        print(f"brief failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def _switch_concerns(args) -> int:
    cfg = _load(args.config)
    if cfg is None:
        return 2
    db = args.db or cfg.database_path
    try:
        rows = switch_concerns(db)
    except OSError as exc:  # the backup could not be made, or is already there: nothing was dropped
        print(f"switch-concerns stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"switch-concerns backup={db}{BACKUP_SUFFIX} rows_dropped={rows}", flush=True)  # counts only
    return 0


def _slots(cfg):
    slots = SLOTS + [BRIEF_SLOT] if cfg.brief else list(SLOTS)
    if cfg.brief and cfg.meeting_notes_data_source and cfg.mom_enabled:
        slots.append(SEND_MOM_SLOT)
    if cfg.shift:
        slots += SHIFT_JOB_SLOTS
    return slots


def _brief_secrets(cfg):
    if cfg.brief is None:
        return []
    return [("PSR_SECRET", cfg.brief.psr_secret)]


def _task_llm(cfg, task, dry_print=False):
    """A brief or Minutes task through ``llm run`` with ``llm_model``. Off: a dry print leaves the LLM out (raw output); a real run marks it unavailable."""
    if cfg.brief is None:
        return None
    return _api_llm(cfg, task, cfg.brief.llm_model) or (None if dry_print else UnavailableLlm())


def make_job(conn, notion, cfg, gateway, clock, shift_target=None):
    """The scheduler's job: Monday = snapshot, then (when configured and the MOM is not paused) the minutes draft."""
    def job(kind: str) -> None:
        moment = clock()
        if kind.startswith("shift"):
            conn_jo = jo_snapshots.connect(cfg.database_path)
            try:
                run_shift_report(conn_jo, notion, kind[len("shift"):], moment,
                                 banner=cfg.shift.test_banner, log=lambda text: print(text, flush=True),
                                 target=_shift_target(cfg) if shift_target is None else shift_target,
                                 llm=_shift_llm(cfg))
            finally:
                conn_jo.close()
            return
        if kind == "send_mom":
            run_send_ready_mom(notion, gateway, cfg.brief.group_chat_id,
                               cfg.brief.group_topic_id, moment, banner=cfg.brief.test_banner,
                               log=lambda text: print(text, flush=True))
            return
        if kind == "brief":
            run_brief(conn, notion, cfg.brief, moment, gateway=gateway, llm=_task_llm(cfg, "brief_consolidation"),
                      meeting_notes_ds=cfg.meeting_notes_data_source, leaves_llm=_task_llm(cfg, "brief_leaves"),
                      log=lambda text: print(text, flush=True))
            return
        _snapshot(conn, notion, kind, moment)
        if kind == "monday" and cfg.meeting_notes_data_source and cfg.mom_enabled:
            try:
                run_draft_mom(conn, notion, cfg.meeting_notes_data_source, moment.astimezone(MANILA).date(),
                              log=lambda text: print(text, flush=True), llm=_task_llm(cfg, "minutes_condense"),
                              gateway=gateway, cyril_contact_id=cfg.brief.cyril_contact_id if cfg.brief else None)
            except MinutesApiError as exc:  # logged here so the scheduler keeps running; no automatic retry
                print(f"mom draft failed: {exc}", flush=True)
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
    notion = _reads(notion)
    meeting_date = args.date or (now or datetime.now(timezone.utc)).astimezone(MANILA).date()
    conn = connect(db)
    try:
        gateway = None if args.dry_print or cfg.brief is None else GatewayClient(cfg.brief.gateway_url, cfg.brief.psr_secret)
        result = run_draft_mom(conn, notion, cfg.meeting_notes_data_source, meeting_date, log=lambda t: print(t, flush=True),
                               llm=_task_llm(cfg, "minutes_condense", args.dry_print), dry_print=args.dry_print,
                               gateway=gateway, cyril_contact_id=cfg.brief.cyril_contact_id if cfg.brief else None)
    except MomAlreadySent as exc:
        print(f"draft-mom refused: {exc}", file=sys.stderr)
        return 1
    except MinutesApiError as exc:
        print(f"draft-mom failed: {exc}", file=sys.stderr)
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
    notion = _reads(notion)
    gateway = None if args.dry_print else GatewayClient(cfg.brief.gateway_url, cfg.brief.psr_secret)
    try:
        run_send_ready_mom(notion, gateway, cfg.brief.group_chat_id,
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
            print("first snapshot, no changes yet (need a Tuesday (kind friday) snapshot and an earlier Monday one; "
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
        cfg = load_config(path)
        for key in cfg.ignored_keys:
            print(f"config key ignored: {key}", file=sys.stderr)
        return cfg
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return None


def _ensure_dir(db: str) -> None:
    os.makedirs(os.path.dirname(db) or ".", exist_ok=True)


def _snapshot(conn, notion, kind, now) -> None:
    res = run_snapshot(conn, notion, kind, now)
    # One line per run, counts only: never message text or secrets.
    print(f"snapshot id={res.snapshot_id} kind={kind} baseline={res.is_baseline} "
          f"changed={res.changed} unchanged={res.unchanged} removed={res.removed}", flush=True)
