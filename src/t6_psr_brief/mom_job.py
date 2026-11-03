"""Draft the PSR minutes in Notion: after the Monday 11:00 snapshot, or by hand with ``draft-mom``."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from .contacts import load_contacts
from .holidays import upcoming_entries
from .mom import build_draft, render_blocks
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
    conn: sqlite3.Connection, notion, projects_ds: str, contacts_ds: str, meeting_notes_ds: str,
    meeting_date: date, log: Callable[[str], None] = print,
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
    draft = build_draft(load_snapshot(conn, old_id), load_snapshot(conn, new_id), old_on, new_on, meeting_date)

    description = _plain(notion.retrieve_data_source(projects_ds).get("description") or [])
    others = upcoming_entries(description, meeting_date)

    people = _people(_previous_draft(notion, meeting_notes_ds, meeting_date))
    contacts = {c.name.lower(): c for c in load_contacts(notion, contacts_ds).values()}
    mentions = []
    for p in people:
        c = contacts.get(str(p.get("name") or "").lower())
        mentions.append(f"@{c.handle}" if c and c.handle else str(p.get("name") or ""))

    blocks = render_blocks(draft, [str(p.get("name") or "") for p in people], others, [m for m in mentions if m])

    # A re-run replaces the unsent draft: trash it first, then create the new one.
    for page in existing:
        notion.update_page(page["id"], {"in_trash": True})
    properties = {
        "Name": {"title": [{"type": "text", "text": {"content": title}}]},
        "Meeting Date": {"date": {"start": meeting_date.isoformat()}},
        "Project": {"relation": [{"id": i} for i in draft.project_ids]},
        "Team Attendees": {"people": [{"object": "user", "id": p["id"]} for p in people]},
    }
    page = notion.create_page(meeting_notes_ds, properties, blocks[:CHUNK])
    for i in range(CHUNK, len(blocks), CHUNK):
        notion.append_children(page["id"], blocks[i:i + CHUNK])
    log(f"mom date={meeting_date} shown={len(draft.entries)} unchanged={len(draft.unchanged)} "
        f"projects={len(draft.project_ids)} others={len(others)}")
    return MomResult(page["id"], len(draft.entries), len(draft.unchanged))


def _previous_draft(notion, meeting_notes_ds: str, before: date) -> dict | None:
    pages = notion.query_data_source(
        meeting_notes_ds, filter={"property": "Name", "title": {"starts_with": TITLE_PREFIX}})
    dated = []
    for p in pages:
        start = ((p["properties"].get("Meeting Date") or {}).get("date") or {}).get("start")
        if start and _title(p).startswith(TITLE_PREFIX) and not p.get("in_trash") and date.fromisoformat(start[:10]) < before:
            dated.append((start[:10], p))
    return max(dated, key=lambda t: t[0])[1] if dated else None


def _people(page: dict | None) -> list[dict]:
    if not page:
        return []
    return list((page["properties"].get("Team Attendees") or {}).get("people") or [])


def _title(page: dict) -> str:
    prop = page["properties"].get("Name") or {}
    return "".join(t.get("plain_text", "") for t in prop.get("title") or [])


def _sent_at(page: dict) -> bool:
    return bool(((page["properties"].get("Sent at") or {}).get("date") or {}).get("start"))


def _plain(rich: list[dict]) -> str:
    return "".join(t.get("plain_text", "") for t in rich)


def _local_date(taken_at: str) -> date:
    return datetime.fromisoformat(taken_at).astimezone(MANILA).date()
