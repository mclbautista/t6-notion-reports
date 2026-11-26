"""Draft the PSR minutes (written through the t6 API): after the Monday 11:00 snapshot, or by hand with ``draft-mom``."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from .holidays import upcoming_entries
from .telegram_html import escape
from .llm_api import LlmClient
from .gateway import GatewayError
from .minutes_api import MinutesAmbiguous, MinutesApiError, create_draft, trash_draft
from .mom import Draft, build_draft, condense_draft, render_blocks
from .schedule import MANILA
from .snapshots import load_snapshot, mom_diff_pair, snapshot_info, squash

TITLE_PREFIX = "PSR MOM"
NOTION_PAGE = "https://www.notion.so/"


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
    """Trash the half-built page (when its id is known), log one line and DM Cyril. Never a title or page text."""
    trash_error = None
    if exc.page_id:
        try:
            trash_draft(client, exc.page_id)
        except MinutesApiError as trash_exc:
            trash_error = str(trash_exc)
    log(f"mom date={meeting_date} failed reason={exc.reason} page={exc.page_id or 'unknown'}"
        + (f" trash_failed={trash_error}" if trash_error else ""))
    rerun = f"draft-mom --date {meeting_date.isoformat()}"
    if exc.page_id is None:
        lines = [f"⚠️ The Monday minutes draft failed: {exc.reason}.",
                 f"The page may exist: check Notion for PSR MOM {meeting_date.isoformat()} and trash it by hand.",
                 f"Rerun: {rerun}"]
    else:
        link = NOTION_PAGE + exc.page_id.replace("-", "")
        first = f"⚠️ The Monday minutes draft failed: {exc.reason}."
        if trash_error:
            lines = [first, f"Trashing the half-built page failed ({trash_error}): trash it by hand: {link}", f"Rerun: {rerun}"]
        else:
            lines = [first, f"The half-built page was trashed: {link}", f"Rerun: {rerun}"]
    _dm_cyril(gateway, contacts, cyril_contact_id, "\n".join(escape(line) for line in lines),
              f"psr-mom-failed-{meeting_date.isoformat()}", log)


def _dm_cyril(gateway, contacts, cyril_contact_id: str | None, text: str, key: str, log: Callable[[str], None]) -> None:
    """The brief's path: ``cyril_contact_id`` -> the Contact's Telegram ID -> ``gateway.notify``. Logs what stops it."""
    if gateway is None or not cyril_contact_id:
        log("mom failure dm skipped: no gateway or cyril_contact_id")
        return
    norm = lambda cid: cid.replace("-", "").lower()  # noqa: E731
    cyril = next((c for cid, c in contacts.items() if norm(cid) == norm(cyril_contact_id)), None)
    if cyril is None or cyril.telegram_id is None:
        log("mom failure dm skipped: Cyril has no Telegram ID")
        return
    try:
        gateway.notify(cyril.telegram_id, text, key)
    except GatewayError as exc:
        log(f"mom failure dm failed error={type(exc).__name__}")


def _log_raw(draft: Draft, new_state: dict[str, dict[str, object]], log: Callable[[str], None]) -> None:
    for e in draft.entries:
        if e.raw_fallback:
            code = squash(str(new_state.get(e.page_id, {}).get("Project Code") or "")) or e.label
            log(f"mom raw project={code} reason={e.raw_reason}")


def _local_date(taken_at: str) -> date:
    return datetime.fromisoformat(taken_at).astimezone(MANILA).date()
