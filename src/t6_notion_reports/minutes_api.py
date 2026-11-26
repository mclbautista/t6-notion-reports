"""The Minutes writes, through the t6 API (``meeting-notes`` create, append, trash, mark-sent).

The API has no preview step for these verbs. ``create`` is not idempotent, so nothing here retries it:
a failed run is repeated by the next run, which looks the title up first (``run_draft_mom``'s
``existing``) and trashes what it finds once the new page exists. The messages of ``MinutesApiError``
name the verb and the API's rule, never a title or page text.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from t6.client import APIError, Unavailable
from t6.verbs import (
    Ambiguous, Done, MeetingNotesAppendInput, MeetingNotesCreateInput,
    MeetingNotesMarkSentInput, MeetingNotesTrashInput, NoteBlock, NoteSpan, Refused,
)

CHUNK = 100  # the server does not split a large body: the first 100 blocks go with create, the rest with append


class MinutesApiError(OSError):
    """The API did not do the write: unreachable, refused, or unclear."""


class MinutesAmbiguous(MinutesApiError):
    """The API could not confirm the write (t6-core#699). ``page_id`` is the page it may have left, when known."""

    def __init__(self, what: str, reason: str, page_id: str | None, mismatched: list[str]):
        super().__init__(f"{what} ambiguous")
        self.reason, self.page_id, self.mismatched = reason, page_id, mismatched


def _done(what: str, call, inputs) -> dict[str, Any]:
    try:
        outcome = call(inputs)
    except APIError as exc:
        raise MinutesApiError(f"{what} http status={exc.status}") from None
    if isinstance(outcome, Unavailable):
        raise MinutesApiError(f"{what} unreachable")
    if isinstance(outcome, Refused):
        raise MinutesApiError(f"{what} refused rule={outcome.rule}")
    if isinstance(outcome, Ambiguous):
        pending = next((c for c in outcome.candidates if isinstance(c, dict)), {})
        raise MinutesAmbiguous(what, str(pending.get("reason") or "unknown"), pending.get("page_id") or None,
                               list(pending.get("mismatched") or []))
    if not isinstance(outcome, Done):
        raise MinutesApiError(f"{what} answered {outcome.outcome}")
    return outcome.result


def create_draft(client, title: str, meeting_date: date, project_ids: list[str], attendee_ids: list[str],
                 blocks: list[dict]) -> str:
    """Create the page with the first CHUNK blocks, append the rest in CHUNKs, and return the page id.

    ``blocks`` are in the shape ``mom.render_blocks`` gives; ``to_note_blocks`` converts them.
    """
    body = to_note_blocks(blocks)
    created = _done("meeting-notes create", client.meeting_notes_create, MeetingNotesCreateInput(
        title=title, meeting_date=meeting_date.isoformat(), project_ids=project_ids,
        attendee_ids=attendee_ids, blocks=body[:CHUNK]))
    page_id = created["meeting_note"]["page_id"]
    for i in range(CHUNK, len(body), CHUNK):
        try:
            _done("meeting-notes append", client.meeting_notes_append,
                  MeetingNotesAppendInput(note=page_id, blocks=body[i:i + CHUNK]))
        except MinutesAmbiguous as exc:
            exc.page_id = exc.page_id or page_id  # the half-built page is the one just created
            raise
    return page_id


def trash_draft(client, page_id: str) -> None:
    _done("meeting-notes trash", client.meeting_notes_trash, MeetingNotesTrashInput(note=page_id))


def mark_sent(client, page_id: str, when: datetime) -> None:
    """Set Sent at. A note that already has one keeps it, so a retry is safe. ``when`` carries a UTC offset."""
    _done("meeting-notes mark-sent", client.meeting_notes_mark_sent,
          MeetingNotesMarkSentInput(note=page_id, at=when))


def to_note_blocks(blocks: list[dict]) -> list[NoteBlock]:
    """Notion-shaped blocks (``type``, rich text, ``children``) as the API's ``kind``, ``text``, ``spans``."""
    out = []
    for blk in blocks:
        kind = blk["type"]
        content = blk[kind]
        spans = [_span(item) for item in content.get("rich_text") or []]
        out.append(NoteBlock(
            kind=kind, text="".join(s.text for s in spans), spans=spans,
            checked=bool(content.get("checked")) if kind == "to_do" else None,
            children=to_note_blocks(content.get("children") or [])))
    return out


def _span(item: dict) -> NoteSpan:
    text = item["text"]
    marks = item.get("annotations") or {}
    return NoteSpan(text=text["content"], url=(text.get("link") or {}).get("url"),
                    **{k: bool(marks.get(k)) for k in ("bold", "italic", "underline", "strikethrough", "code")})
