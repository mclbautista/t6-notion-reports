"""Draft the PSR minutes (written through the t6 API): after the Monday 11:00 snapshot, or by hand with ``draft-mom``."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from .holidays import upcoming_entries
from .llm_api import LlmClient
from .minutes_api import MinutesApiError, create_draft, trash_draft
from .mom import build_draft, condense_draft, render_blocks
from .schedule import MANILA
from .snapshots import load_snapshot, mom_diff_pair, snapshot_info

TITLE_PREFIX = "PSR MOM"


class MomAlreadySent(Exception):
    """The page for this date has `Sent at` set. The draft is never overwritten."""


@dataclass(frozen=True)
class MomResult:
    page_id: str | None = None  # None: no Monday/Friday snapshot pair yet
    shown: int = 0
    unchanged: int = 0


def title_for(meeting_date: date) -> str:
    return f"{TITLE_PREFIX} {meeting_date.isoformat()}"


def run_draft_mom(
    conn: sqlite3.Connection, notion, meeting_notes_ds: str,
    meeting_date: date, log: Callable[[str], None] = print, llm: LlmClient | None = None,
    dry_print: bool = False, out: Callable[[str], None] = print,
) -> MomResult:
    pair = mom_diff_pair(conn, meeting_date)
    if pair is None:
        log("mom page=none reason=no Monday snapshot with an earlier Friday one")
        return MomResult()
    old_id, new_id = pair
    title = title_for(meeting_date)

    existing = [n for n in notion.meeting_notes(title=title, include_body=False) if n.title == title]
    if any(n.sent_at for n in existing):
        raise MomAlreadySent(f"{title} was already sent; not overwriting")

    old_on = _local_date(snapshot_info(conn, old_id).taken_at)
    new_on = _local_date(snapshot_info(conn, new_id).taken_at)
    new_state = load_snapshot(conn, new_id)
    old_state = load_snapshot(conn, old_id)
    draft = build_draft(old_state, new_state, old_on, new_on, meeting_date, notion.project_rules())
    all_contacts = notion.read_contacts()
    condensed = raw = 0
    if llm is not None:
        condensed, raw = condense_draft(draft, llm, new_state)
    if dry_print:  # nothing is written: the bullets per project, marked (raw) when condensing was rejected or failed
        for e in draft.entries:
            out(f"## {e.label}{' (raw)' if e.raw_fallback else ''}")
            for bullet in e.bullets:
                out(f"- {bullet}")
        log(f"mom dry_print=True date={meeting_date} shown={len(draft.entries)} condensed={condensed} raw={raw}")
        return MomResult("dry-print", len(draft.entries), len(draft.unchanged))  # no page: a marker, not an id

    others = upcoming_entries(notion.notices().description, meeting_date)

    previous = notion.meeting_notes(title_prefix=TITLE_PREFIX, before=meeting_date.isoformat(), latest=True, include_body=False)
    people = list(previous[0].attendees) if previous else []  # (Notion user id, name)
    contacts = {c.name.lower(): c for c in all_contacts.values()}
    mentions = []
    for _, name in people:
        c = contacts.get(name.lower())
        mentions.append(f"@{c.handle}" if c and c.handle else name)

    blocks = render_blocks(draft, [name for _, name in people], others, [m for m in mentions if m])

    page_id = create_draft(notion.client, title, meeting_date, draft.project_ids[:100], [i for i, _ in people], blocks)
    # A re-run replaces the unsent draft, but only once the new page exists. A failed trash leaves two
    # unsent drafts; the next run finds both by title and trashes them.
    for old_page in existing:
        try:
            trash_draft(notion.client, old_page.page_id)
        except MinutesApiError as exc:
            log(f"mom trash failed: {exc}")
    log(f"mom date={meeting_date} shown={len(draft.entries)} unchanged={len(draft.unchanged)} "
        f"projects={len(draft.project_ids)} others={len(others)} condensed={condensed} raw={raw}")
    return MomResult(page_id, len(draft.entries), len(draft.unchanged))


def _local_date(taken_at: str) -> date:
    return datetime.fromisoformat(taken_at).astimezone(MANILA).date()
