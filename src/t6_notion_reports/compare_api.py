"""Compare the Notion path and the t6 API path on live data, sending nothing (#108 deploy check).

For each path the Projects and Job Orders snapshots and the brief are built once, in a copy of the
database, and written to ``out_dir``. The brief is the dry-print with ``show_llm_input``: its messages and
each project's LLM payload (which holds the Minutes lines and the leave lines), with no LLM call and no
gateway. The files hold names and stay in ``out_dir``. What is returned and printed is only whether each
output is equal, the count of differing lines, and the seconds each path took.
"""
from __future__ import annotations

import difflib
import json
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import jo_snapshots
from .brief_job import BriefFailed, run_brief
from .jo_snapshot_job import run_jo_snapshot
from .snapshot_job import run_snapshot
from .snapshots import connect, load_snapshot


@dataclass(frozen=True)
class Comparison:
    name: str
    differing: int  # lines in one output and not the other
    old_seconds: float
    new_seconds: float

    def line(self) -> str:
        return (f"{self.name} equal={self.differing == 0} differing_lines={self.differing} "
                f"old_s={self.old_seconds:.1f} new_s={self.new_seconds:.1f}")


def run_compare(cfg, old_reader, new_reader, out_dir: str, now: datetime) -> list[Comparison]:
    """``old_reader`` is the Notion client; ``new_reader`` an ``ApiNotion`` with ``snapshots=True``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    texts: dict[str, dict[str, str]] = {"old": {}, "new": {}}
    seconds: dict[tuple[str, str], float] = {}
    for label, reader in (("old", old_reader), ("new", new_reader)):
        db = out / f"{label}.db"
        if os.path.exists(cfg.database_path):
            shutil.copyfile(cfg.database_path, db)
        else:
            db.unlink(missing_ok=True)
        started = time.monotonic()
        conn = connect(str(db))
        try:
            snap = run_snapshot(conn, reader, cfg.projects_data_source, cfg.contacts_data_source, "friday", now)
            texts[label]["projects"] = _dump(load_snapshot(conn, snap.snapshot_id))
        finally:
            conn.close()
        seconds[(label, "projects")] = time.monotonic() - started
        started = time.monotonic()
        conn = jo_snapshots.connect(str(db))
        try:
            jo = run_jo_snapshot(conn, reader, cfg.jo_data_source, cfg.contacts_data_source,
                                 cfg.projects_data_source, "manual", now)
            texts[label]["job_orders"] = _dump(jo_snapshots.load_snapshot(conn, jo.snapshot_id).pages)
        finally:
            conn.close()
        seconds[(label, "job_orders")] = time.monotonic() - started
        started = time.monotonic()
        conn = connect(str(db))
        printed: list[str] = []
        try:
            run_brief(conn, reader, cfg.contacts_data_source, cfg.brief, now, gateway=None, llm=None,
                      dry_print=True, show_llm_input=True, out=printed.append, log=lambda _: None,
                      meeting_notes_ds=cfg.meeting_notes_data_source, projects_ds=cfg.projects_data_source,
                      matching=cfg.matching)
        except BriefFailed:
            pass  # a missing digest recipient is raised after the messages are printed: the same on both paths
        finally:
            conn.close()
        texts[label]["brief"] = "\n".join(printed)
        seconds[(label, "brief")] = time.monotonic() - started
    results = []
    for name in ("projects", "job_orders", "brief"):
        old, new = texts["old"][name], texts["new"][name]
        (out / f"{name}.old.txt").write_text(old, encoding="utf-8")
        (out / f"{name}.new.txt").write_text(new, encoding="utf-8")
        diff = difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
        differing = sum(1 for d in diff if d[:1] in "+-" and d[:3] not in ("+++", "---"))
        results.append(Comparison(name, differing, seconds[("old", name)], seconds[("new", name)]))
    return results


def _dump(state: dict) -> str:
    return "\n".join(f"{page_id}\t{json.dumps(fields, sort_keys=True, ensure_ascii=False)}"
                     for page_id, fields in sorted(state.items()))
