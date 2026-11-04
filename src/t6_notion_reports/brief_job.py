"""The Saturday briefing job: diff Friday against Monday, build the messages, send them."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .briefing import OutboundMessage, Settings, build_briefing, digest_missing
from .config import BriefSettings
from .contacts import Contact, load_contacts
from .diff import diff_snapshots, display_names, sort_projects, unchanged_project
from .gateway import GatewayError
from .llm import LlmClient
from .owed import OwedOutcome, find_owed
from .schedule import MANILA
from .snapshots import default_diff_pair, latest_snapshot_ids, load_snapshot, snapshot_info
from .status_gate import gate, is_archiving, looks_like_in_progress


class BriefFailed(Exception):
    """At least one message could not be queued. Everything else was still attempted."""


@dataclass(frozen=True)
class BriefResult:
    baseline: bool = False
    messages: tuple[OutboundMessage, ...] = ()
    sent: int = 0
    failed: int = 0
    changed: int = 0
    owed_failed: int = 0


def run_brief(
    conn: sqlite3.Connection,
    notion,
    contacts_ds: str,
    brief: BriefSettings,
    now: datetime,
    gateway=None,
    llm: LlmClient | None = None,
    dry_print: bool = False,
    out: Callable[[str], None] = print,
    log: Callable[[str], None] = print,
) -> BriefResult:
    """Send the briefing now. With ``dry_print`` (or no ``gateway``), print and send nothing.

    With ``llm``, each project also gets its owed items; without one the owed section is left out.
    """
    pair = default_diff_pair(conn)
    first = False
    if pair is None:
        latest = latest_snapshot_ids(conn, 1)
        if not latest:
            out("first snapshot, no changes yet (need a Friday snapshot and an earlier Monday one)")
            log("brief baseline=True sent=0 failed=0")
            return BriefResult(baseline=True)
        # Only one comparison point exists: brief without a changes section.
        first = True
        old_id = new_id = latest[0]
    else:
        old_id, new_id = pair
    old_state, new_state = load_snapshot(conn, old_id), load_snapshot(conn, new_id)
    old_state, new_state = apply_gate(old_state, new_state, log)
    old_on = snapshot_info(conn, old_id).taken_at
    new_on = snapshot_info(conn, new_id).taken_at
    changes = [] if first else diff_snapshots(
        old_state, new_state,
        datetime.fromisoformat(old_on).astimezone(MANILA).date(),
        datetime.fromisoformat(new_on).astimezone(MANILA).date(),
    )
    changed = len(changes)
    owed: dict[str, OwedOutcome] | None = None
    owed_failed: list[str] = []
    if llm is not None:
        owed = {page_id: find_owed(llm, state) for page_id, state in new_state.items()}
        names = display_names(old_state, new_state)
        owed_failed = sorted(names[p] for p, o in owed.items() if o.failed)
        failed_ids = sorted(p for p, o in owed.items() if o.failed)
        shown = {c.page_id for c in changes}
        new_day = datetime.fromisoformat(new_on).astimezone(MANILA).date()
        changes = sort_projects(changes + [
            unchanged_project(p, names, new_state[p], new_day)
            for p, o in owed.items() if (o.items or o.failed) and p not in shown
        ])
        for page_id in failed_ids:
            log(f"brief owed items unavailable project={page_id}")
    contacts = load_contacts(notion, contacts_ds)
    cyril_id = brief.cyril_contact_id or (brief.digest_recipients[0] if brief.digest_recipients else "")
    cyril = contacts.get(cyril_id) or Contact(cyril_id, "Cyril")
    digest_contacts = tuple(
        contacts.get(cid) or Contact(cid, cid)
        for cid in brief.digest_recipients
    ) or (cyril,)
    settings = Settings(cyril, brief.group_chat_id, brief.group_topic_id, brief.test_banner, digest_recipients=digest_contacts)
    messages = build_briefing(
        changes, coordinators_in(old_state, new_state), contacts, settings, now.astimezone(MANILA).date(),
        owed, owed_failed, active_coordinators_in(new_state), first=first,
    )
    no_digest = digest_missing(settings)

    if dry_print or gateway is None:
        for m in messages:
            out(f"--- to={m.to} topic={m.topic} key={m.key} ({m.recipient}) ---\n{m.text}\n")
        log(f"brief dry_print=True messages={len(messages)} changed={changed} owed_failed={len(owed_failed)}")
        if no_digest:
            raise BriefFailed("No digest recipient has a TelegramUserID: digest not built")
        return BriefResult(messages=tuple(messages), changed=changed, owed_failed=len(owed_failed))

    sent = failed = 0
    for m in messages:
        try:
            gateway.notify(m.to, m.text, m.key, m.topic)
            sent += 1
        except GatewayError as exc:
            failed += 1
            log(f"brief send failed key={m.key} error={exc}")
    log(f"brief sent={sent} failed={failed} messages={len(messages)} changed={changed} owed_failed={len(owed_failed)}")
    if no_digest:
        raise BriefFailed("No digest recipient has a TelegramUserID: DMs sent, digest not sent")
    if failed:
        raise BriefFailed(f"{failed} of {len(messages)} messages were not queued")
    return BriefResult(messages=tuple(messages), sent=sent, changed=changed, owed_failed=len(owed_failed))


def coordinators_in(*states: dict[str, dict[str, object]]) -> dict[str, str]:
    """Contact page id → name for everyone who coordinates a project in any of the snapshots."""
    people: dict[str, str] = {}
    for state in states:
        for project in state.values():
            for ref in project.get("T6 Post Coordinator") or []:
                if isinstance(ref, dict):
                    people.setdefault(str(ref["id"]), str(ref.get("name") or ref["id"]))
    return people



def apply_gate(old, new, log: Callable[[str], None] = print):
    """Drop every project outside the status gate from both snapshots, judged by its latest state.

    Runs before the LLM or any message. An excluded Status that looks like an In-Progress option is
    logged by project name, so the Notion data can be fixed.
    """
    names = display_names(old, new)
    latest = {**old, **new}
    keep = set()
    for page_id, state in latest.items():
        if gate(state.get("Status")) is not None:
            keep.add(page_id)
        elif looks_like_in_progress(state.get("Status")):
            log(f"brief status excluded looks like In-Progress project={names[page_id]} status={state.get('Status')!r}")
    return ({p: s for p, s in old.items() if p in keep}, {p: s for p, s in new.items() if p in keep})


def active_coordinators_in(state: dict[str, dict[str, object]]) -> set[str]:
    """Contact page ids of coordinators with an In-Progress (any kind) or Archiving project."""
    return {
        str(ref["id"])
        for project in state.values()
        if gate(project.get("Status")) == "in_progress" or is_archiving(project.get("Status"))
        for ref in project.get("T6 Post Coordinator") or [] if isinstance(ref, dict)
    }
