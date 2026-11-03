"""The Sunday briefing job: diff Friday against Monday, build the messages, send them."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .briefing import OutboundMessage, Settings, build_briefing
from .config import BriefSettings
from .contacts import Contact, load_contacts
from .diff import diff_snapshots
from .gateway import GatewayError
from .schedule import MANILA
from .snapshots import default_diff_pair, load_snapshot, snapshot_info


class BriefFailed(Exception):
    """At least one message could not be queued. Everything else was still attempted."""


@dataclass(frozen=True)
class BriefResult:
    baseline: bool = False
    messages: tuple[OutboundMessage, ...] = ()
    sent: int = 0
    failed: int = 0
    changed: int = 0


def run_brief(
    conn: sqlite3.Connection,
    notion,
    contacts_ds: str,
    brief: BriefSettings,
    now: datetime,
    gateway=None,
    dry_print: bool = False,
    out: Callable[[str], None] = print,
    log: Callable[[str], None] = print,
) -> BriefResult:
    """Send the briefing now. With ``dry_print`` (or no ``gateway``), print and send nothing."""
    pair = default_diff_pair(conn)
    if pair is None:
        out("first snapshot, no changes yet (need a Friday snapshot and an earlier Monday one)")
        log("brief baseline=True sent=0 failed=0")
        return BriefResult(baseline=True)
    old_id, new_id = pair
    old_state, new_state = load_snapshot(conn, old_id), load_snapshot(conn, new_id)
    old_on = snapshot_info(conn, old_id).taken_at
    new_on = snapshot_info(conn, new_id).taken_at
    changes = diff_snapshots(
        old_state, new_state,
        datetime.fromisoformat(old_on).astimezone(MANILA).date(),
        datetime.fromisoformat(new_on).astimezone(MANILA).date(),
    )
    contacts = load_contacts(notion, contacts_ds)
    cyril = contacts.get(brief.cyril_contact_id) or Contact(brief.cyril_contact_id, "Cyril")
    messages = build_briefing(
        changes, coordinators_in(old_state, new_state), contacts,
        Settings(cyril, brief.group_chat_id, brief.group_topic_id, brief.test_banner),
        now.astimezone(MANILA).date(),
    )

    if dry_print or gateway is None:
        for m in messages:
            out(f"--- to={m.to} topic={m.topic} key={m.key} ({m.recipient}) ---\n{m.text}\n")
        log(f"brief dry_print=True messages={len(messages)} changed={len(changes)}")
        return BriefResult(messages=tuple(messages), changed=len(changes))

    sent = failed = 0
    for m in messages:
        try:
            gateway.notify(m.to, m.text, m.key, m.topic)
            sent += 1
        except GatewayError as exc:
            failed += 1
            log(f"brief send failed key={m.key} error={exc}")
    log(f"brief sent={sent} failed={failed} messages={len(messages)} changed={len(changes)}")
    if failed:
        raise BriefFailed(f"{failed} of {len(messages)} messages were not queued")
    return BriefResult(messages=tuple(messages), sent=sent, changed=len(changes))


def coordinators_in(*states: dict[str, dict[str, object]]) -> dict[str, str]:
    """Contact page id → name for everyone who coordinates a project in any of the snapshots."""
    people: dict[str, str] = {}
    for state in states:
        for project in state.values():
            for ref in project.get("T6 Post Coordinator") or []:
                if isinstance(ref, dict):
                    people.setdefault(str(ref["id"]), str(ref.get("name") or ref["id"]))
    return people

