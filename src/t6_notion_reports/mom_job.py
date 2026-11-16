"""Draft the PSR minutes (written through the t6 API): after the Monday 11:00 snapshot, or by hand with ``draft-mom``."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from .contacts import load_contacts
from .holidays import upcoming_entries
from .llm import LlmClient
from .minutes_api import MinutesApiError, create_draft, previous_attendees, trash_draft
from .mom import build_draft, condense_draft, render_blocks
from .redact import Redactor
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
    conn: sqlite3.Connection, notion, client, projects_ds: str, contacts_ds: str, meeting_notes_ds: str,
    meeting_date: date, log: Callable[[str], None] = print, llm: LlmClient | None = None,
) -> MomResult:
    pair = mom_diff_pair(conn, meeting_date)
    if pair is None:
        log("mom page=none reason=no Monday snapshot with an earlier Friday one")
        return MomResult()
    old_id, new_id = pair
    title = title_for(meeting_date)

    existing = [p for p in notion.query_data_source(
        meeting_notes_ds, filter={"property": "Name", "title": {"equals": title}})
        if _title(p) == title and not p.get("in_trash")]
    if any(_sent_at(p) for p in existing):
        raise MomAlreadySent(f"{title} was already sent; not overwriting")

    old_on = _local_date(snapshot_info(conn, old_id).taken_at)
    new_on = _local_date(snapshot_info(conn, new_id).taken_at)
    new_state = load_snapshot(conn, new_id)
    old_state = load_snapshot(conn, old_id)
    draft = build_draft(old_state, new_state, old_on, new_on, meeting_date)
    all_contacts = load_contacts(notion, contacts_ds)
    condensed = raw = 0
    if llm is not None:
        condensed, raw = condense_draft(draft, llm, new_state, Redactor.build(all_contacts.values(), (old_state, new_state)))

    description = _plain(notion.retrieve_data_source(projects_ds).get("description") or [])
    others = upcoming_entries(description, meeting_date)

    people = previous_attendees(client, TITLE_PREFIX, meeting_date)
    contacts = {c.name.lower(): c for c in all_contacts.values()}
    mentions = []
    for _, name in people:
        c = contacts.get(name.lower())
        mentions.append(f"@{c.handle}" if c and c.handle else name)

    blocks = render_blocks(draft, [name for _, name in people], others, [m for m in mentions if m])

    page_id = create_draft(client, title, meeting_date, draft.project_ids[:100], [i for i, _ in people], blocks)
    # A re-run replaces the unsent draft, but only once the new page exists. A failed trash leaves two
    # unsent drafts; the next run finds both by title and trashes them.
    for old_page in existing:
        try:
            trash_draft(client, old_page["id"])
        except MinutesApiError as exc:
            log(f"mom trash failed: {exc}")
    log(f"mom date={meeting_date} shown={len(draft.entries)} unchanged={len(draft.unchanged)} "
        f"projects={len(draft.project_ids)} others={len(others)} condensed={condensed} raw={raw}")
    return MomResult(page_id, len(draft.entries), len(draft.unchanged))


def _title(page: dict) -> str:
    prop = page["properties"].get("Name") or {}
    return "".join(t.get("plain_text", "") for t in prop.get("title") or [])


def _sent_at(page: dict) -> bool:
    return bool(((page["properties"].get("Sent at") or {}).get("date") or {}).get("start"))


def _plain(rich: list[dict]) -> str:
    return "".join(t.get("plain_text", "") for t in rich)


def _local_date(taken_at: str) -> date:
    return datetime.fromisoformat(taken_at).astimezone(MANILA).date()
