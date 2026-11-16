"""The latest PSR minutes page, read as it is now, for the lines about one project (spec #40, ticket #47).

The page is the one the Monday job drafted (``mom.py``) and the minutes owner may have edited, so what is read
here is her version. A project's entry is the top-level bullet that starts with its label; the lines
are that bullet's children, with the "Raw source" toggle left out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .mom_job import TITLE_PREFIX
from .snapshots import squash
from .t6_api import Block

# Suffixes the draft adds after a project's label: "(Health not set)", "(raw)", " - last updated Oct 26".
_ENTRY = re.compile(r"^(?P<label>.*?)(?: \((?:Health not set|raw)\))*(?: - (?:last updated .*|no notes yet))?$")
HEADING_KINDS = {"heading_1", "heading_2", "heading_3"}
UNCHANGED_PREFIX = "unchanged"


@dataclass(frozen=True)
class Minutes:
    """The latest minutes page: its dates and, by project label, the bullets written about each project."""

    page_date: date
    last_edited: str | None  # Notion's own timestamp, as returned
    sent_at: str | None
    by_label: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def lines_for(self, label: str) -> tuple[str, ...]:
        return self.by_label.get(_key(label), ())


def load_minutes(notion, today: date) -> Minutes | None:
    """The newest PSR MOM page dated on or before ``today``, or None when there is none."""
    pages = [n for n in notion.meeting_notes(title_prefix=TITLE_PREFIX)
             if n.title.startswith(TITLE_PREFIX) and n.meeting_day and n.meeting_day <= today]
    if not pages:
        return None
    note = max(pages, key=lambda n: n.meeting_day)
    return Minutes(note.meeting_day, note.last_edited, note.sent_at, _entries(note.body))


def _entries(body: tuple[Block, ...]) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for blk in body:
        text = squash(blk.text)
        if blk.kind in HEADING_KINDS and text.lower().startswith(UNCHANGED_PREFIX):
            break  # the Unchanged list is for Notion only
        if blk.kind != "bulleted_list_item" or not blk.children or not text:
            continue
        lines = _children(blk)
        if lines:
            key = _key(_ENTRY.match(text).group("label"))
            out[key] = out.get(key, ()) + tuple(lines)
    return out


def _children(blk: Block) -> list[str]:
    lines: list[str] = []
    for child in blk.children:
        if child.kind == "toggle":
            continue  # the raw source stays in Notion
        text = squash(child.text)
        if text:
            lines.append(f"• {text}")
        lines += _children(child)
    return lines


def _key(label: str) -> str:
    return squash(label).casefold()
