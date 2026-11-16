"""The Minutes writes, through the t6 API (``meeting-notes`` create, append, trash, mark-sent, latest).

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
    Ambiguous, Done, MeetingNotesAppendInput, MeetingNotesCreateInput, MeetingNotesListInput,
    MeetingNotesMarkSentInput, MeetingNotesTrashInput, NoteBlock, NoteSpan, Refused,
)

CHUNK = 100  # the server does not split a large body: the first 100 blocks go with create, the rest with append


class MinutesApiError(OSError):
    """The API did not do the write: unreachable, refused, or unclear."""


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
        raise MinutesApiError(f"{what} ambiguous")
    if not isinstance(outcome, Done):
        raise MinutesApiError(f"{what} answered {outcome.outcome}")
    return outcome.result


def previous_attendees(client, title_prefix: str, before: date) -> list[tuple[str, str]]:
    """(Notion user id, name) of the Team Attendees of the newest page titled ``title_prefix`` dated before ``before``."""
    notes = _done("meeting-notes list", client.meeting_notes_list, MeetingNotesListInput(
        title_prefix=title_prefix, before=before.isoformat(), latest=True, include_body=False))["meeting_notes"]
    if not notes:
        return []
    ids, names = notes[0].get("attendee_ids") or [], notes[0].get("attendee_names") or []
    return [(i, names[n] if n < len(names) else "") for n, i in enumerate(ids)]


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
        _done("meeting-notes append", client.meeting_notes_append,
              MeetingNotesAppendInput(note=page_id, blocks=body[i:i + CHUNK]))
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
