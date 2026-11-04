"""The latest PSR minutes page, read as it is now, for the lines about one project (spec #40, ticket #47).

The page is the one the Monday job drafted (``mom.py``) and Leizette may have edited, so what is read
here is her version. A project's entry is the top-level bullet that starts with its label; the lines
are that bullet's children, with the "Raw source" toggle left out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .mom_job import TITLE_PREFIX
from .snapshots import squash

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


def load_minutes(notion, meeting_notes_ds: str, today: date) -> Minutes | None:
    """The newest PSR MOM page dated on or before ``today``, or None when there is none."""
    pages = []
    for page in notion.query_data_source(
        meeting_notes_ds, filter={"property": "Name", "title": {"starts_with": TITLE_PREFIX}},
    ):
        if page.get("in_trash") or not _title(page).startswith(TITLE_PREFIX):
            continue
        start = ((page["properties"].get("Meeting Date") or {}).get("date") or {}).get("start")
        if start and date.fromisoformat(start[:10]) <= today:
            pages.append((date.fromisoformat(start[:10]), page))
    if not pages:
        return None
    when, page = max(pages, key=lambda p: p[0])
    sent = ((page["properties"].get("Sent at") or {}).get("date") or {}).get("start")
    return Minutes(when, page.get("last_edited_time"), sent, _entries(notion, page["id"]))


def _entries(notion, page_id: str) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for blk in notion.list_block_children(page_id):
        text = squash(_text(blk))
        if blk["type"] in HEADING_KINDS and text.lower().startswith(UNCHANGED_PREFIX):
            break  # the Unchanged list is for Notion only
        if blk["type"] != "bulleted_list_item" or not blk.get("has_children") or not text:
            continue
        lines = _children(notion, blk)
        if lines:
            key = _key(_ENTRY.match(text).group("label"))
            out[key] = out.get(key, ()) + tuple(lines)
    return out


def _children(notion, blk: dict) -> list[str]:
    lines: list[str] = []
    for child in notion.list_block_children(blk["id"]):
        if child["type"] == "toggle":
            continue  # the raw source stays in Notion
        text = squash(_text(child))
        if text:
            lines.append(f"• {text}")
        if child.get("has_children"):
            lines += _children(notion, child)
    return lines


def _text(blk: dict) -> str:
    body = blk.get(blk["type"]) or {}
    return "".join(item.get("plain_text", "") for item in body.get("rich_text") or [])


def _title(page: dict) -> str:
    return "".join(t.get("plain_text", "") for t in (page["properties"].get("Name") or {}).get("title") or [])


def _key(label: str) -> str:
    return squash(label).casefold()
