"""Draft the PSR minutes in Notion: after the Monday 11:00 snapshot, or by hand with ``draft-mom``."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from .holidays import upcoming_entries
from .llm import LlmClient
from .mom import build_draft, condense_draft, render_blocks
from .redact import Redactor
from .schedule import MANILA
from .snapshots import load_snapshot, mom_diff_pair, snapshot_info

TITLE_PREFIX = "PSR MOM"
CHUNK = 100  # Notion accepts at most 100 child blocks per request


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
    draft = build_draft(old_state, new_state, old_on, new_on, meeting_date)
    all_contacts = notion.read_contacts()
    condensed = raw = 0
    if llm is not None:
        condensed, raw = condense_draft(draft, llm, new_state, Redactor.build(all_contacts.values(), (old_state, new_state)))

    others = upcoming_entries(notion.notices().description, meeting_date)

    people = _people(_previous_draft(notion, meeting_date))
    contacts = {c.name.lower(): c for c in all_contacts.values()}
    mentions = []
    for p in people:
        c = contacts.get(str(p.get("name") or "").lower())
        mentions.append(f"@{c.handle}" if c and c.handle else str(p.get("name") or ""))

    blocks = render_blocks(draft, [str(p.get("name") or "") for p in people], others, [m for m in mentions if m])

    properties = {
        "Name": {"title": [{"type": "text", "text": {"content": title}}]},
        "Meeting Date": {"date": {"start": meeting_date.isoformat()}},
        "Project": {"relation": [{"id": i} for i in draft.project_ids[:100]]},
        "Team Attendees": {"people": [{"object": "user", "id": p["id"]} for p in people]},
    }
    page = notion.create_page(meeting_notes_ds, properties, blocks[:CHUNK])
    for i in range(CHUNK, len(blocks), CHUNK):
        notion.append_children(page["id"], blocks[i:i + CHUNK])
    # A re-run replaces the unsent draft, but only once the new page exists.
    for old_page in existing:
        notion.update_page(old_page.page_id, {"in_trash": True})
    log(f"mom date={meeting_date} shown={len(draft.entries)} unchanged={len(draft.unchanged)} "
        f"projects={len(draft.project_ids)} others={len(others)} condensed={condensed} raw={raw}")
    return MomResult(page["id"], len(draft.entries), len(draft.unchanged))


def _previous_draft(notion, before: date):
    pages = [n for n in notion.meeting_notes(title_prefix=TITLE_PREFIX, include_body=False)
             if n.title.startswith(TITLE_PREFIX) and n.meeting_day and n.meeting_day < before]
    return max(pages, key=lambda n: n.meeting_day) if pages else None


def _people(note) -> list[dict]:
    return [] if note is None else [{"id": i, "name": n} for i, n in note.attendees]


def _local_date(taken_at: str) -> date:
    return datetime.fromisoformat(taken_at).astimezone(MANILA).date()
