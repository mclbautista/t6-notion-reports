"""Draft the PSR minutes (written through the t6 API): after the Monday 11:00 snapshot, or by hand with ``draft-mom``."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from .contacts import contact_by_id
from .holidays import upcoming_entries
from .telegram_html import escape
from .llm_api import LlmClient
from .gateway import GatewayError
from .minutes_api import MinutesAmbiguous, MinutesApiError, create_draft, trash_draft
from .mom import Draft, build_draft, condense_draft, render_blocks
from .schedule import MANILA
from .shift_report import page_url
from .snapshots import load_snapshot, mom_diff_pair, snapshot_info, squash

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
    gateway=None, cyril_contact_id: str | None = None,
) -> MomResult:
    """Draft the minutes. A write the API cannot confirm (Ambiguous) trashes the half-built page when its id is known,
    DMs Cyril through ``gateway`` (when given, with ``cyril_contact_id``) and raises; nothing is retried."""
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
        _log_raw(draft, new_state, log)
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

    if len(draft.project_ids) > 100:
        log(f"mom date={meeting_date} project_cap=100 dropped={len(draft.project_ids) - 100}")
    try:
        page_id = create_draft(notion.client, title, meeting_date, draft.project_ids[:100], [i for i, _ in people], blocks)
    except MinutesAmbiguous as exc:
        _failed_draft(notion.client, exc, meeting_date, all_contacts, gateway, cyril_contact_id, log)
        raise
    # A re-run replaces the unsent draft, but only once the new page exists. A failed trash leaves two
    # unsent drafts; the next run finds both by title and trashes them.
    for old_page in existing:
        try:
            trash_draft(notion.client, old_page.page_id)
        except MinutesApiError as exc:
            log(f"mom trash failed: {exc}")
    log(f"mom date={meeting_date} shown={len(draft.entries)} unchanged={len(draft.unchanged)} "
        f"projects={len(draft.project_ids)} others={len(others)} condensed={condensed} raw={raw}")
    _log_raw(draft, new_state, log)
    return MomResult(page_id, len(draft.entries), len(draft.unchanged))


def _failed_draft(client, exc: MinutesAmbiguous, meeting_date: date, contacts, gateway, cyril_contact_id: str | None,
                  log: Callable[[str], None]) -> None:
    """Trash the half-built page (when its id is known), log one line and DM Cyril: the reason, the page link and the
    rerun command, never page text."""
    trash_error = None
    if exc.page_id:
        try:
            trash_draft(client, exc.page_id)
        except MinutesApiError as trash_exc:
            trash_error = str(trash_exc)
    log(f"mom date={meeting_date} failed reason={exc.reason} page={exc.page_id or 'unknown'}"
        f" mismatched={','.join(exc.mismatched) or 'none'}" + (f" trash_failed={trash_error}" if trash_error else ""))
    if exc.page_id is None:
        where = f"The page may exist: check Notion for {title_for(meeting_date)} and trash it by hand."
    elif trash_error:
        where = f"Trashing the half-built page failed ({trash_error}): trash it by hand: {page_url(exc.page_id)}"
    else:
        where = f"The half-built page was trashed: {page_url(exc.page_id)}"
    lines = (f"⚠️ The Monday minutes draft failed: {exc.reason}.", where,
             f"Rerun: draft-mom --date {meeting_date.isoformat()}")
    key = f"psr-mom-failed-{meeting_date.isoformat()}"
    cyril = contact_by_id(contacts, cyril_contact_id) if cyril_contact_id else None
    if gateway is None or cyril is None or cyril.telegram_id is None:
        log("mom failure dm skipped: no gateway, cyril_contact_id or Telegram ID")
        return
    try:
        gateway.notify(cyril.telegram_id, "\n".join(escape(line) for line in lines), key)
    except GatewayError as exc_gw:
        log(f"mom failure dm failed error={type(exc_gw).__name__}")


def _log_raw(draft: Draft, new_state: dict[str, dict[str, object]], log: Callable[[str], None]) -> None:
    for e in draft.entries:
        if e.raw_fallback:
            code = squash(str(new_state.get(e.page_id, {}).get("Project Code") or "")) or e.label
            log(f"mom raw project={code} reason={e.raw_reason}")


def _local_date(taken_at: str) -> date:
    return datetime.fromisoformat(taken_at).astimezone(MANILA).date()
