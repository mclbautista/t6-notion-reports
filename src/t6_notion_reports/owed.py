"""Owed items (spec section 7.2): what a coordinator still owes on a project, inferred by an LLM.

The LLM proposes; code disposes. Every item must cite an exact PM Notes line, or an empty property,
and an item whose citation does not check out is dropped here, not trusted to the model.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from .llm import LlmClient, LlmError
from .snapshots import squash

MAX_ITEMS = 15
PROPERTY_KINDS = ("missing_date", "missing_role")  # the kinds that may cite an empty property
# What the LLM reads (spec 7.2), by stored field name, and so what an item may call "empty".
INPUT_FIELDS = (
    "Stage", "Status", "Health", "Deadline",
    "Editors", "Online Editor", "Colorist", "Sound Tech", "Post Supe", "T6 Post Coordinator",
)

SYSTEM = f"""You help a post-production coordinator see what they still owe on one project before the weekly status meeting.
You are given the project's PM Notes (a dated log, newest "As of" block last) and its Stage, Status, Health, Deadline and team.
Return the items the coordinator can act on, of exactly these kinds:
- missing_date: the next deadline or milestone has no date (for example "TLR piclock TBC").
- missing_role: offline, online, color, sound or VFX has nobody named, in the properties or in the notes.
- open_item: a line such as "Pending", "TBC", "Waiting", "Confirm" or "Follow up" that is still open.
- stale_field: Stage or Status is contradicted by the notes (Stage "Preprod" but the notes say "Shoot today").
Every item needs a citation, one of:
- notes_line: one line copied exactly, character for character, from the PM Notes.
- empty_property: the name of an input property that is empty.
Never invent a citation. Do not list an empty property unless it matters to one of the four kinds. If nothing is owed, return an empty list.
Write each "text" as one short sentence for the coordinator, in the language of the notes (Taglish is fine)."""


class OwedItem(BaseModel):
    kind: Literal["missing_date", "missing_role", "open_item", "stale_field"]
    text: str
    notes_line: str | None = None
    empty_property: str | None = None


class OwedItems(BaseModel):
    items: list[OwedItem]


@dataclass(frozen=True)
class OwedOutcome:
    """Owed items for one project: the valid ones (at most 15), how many more there were, or a failure."""

    items: tuple[OwedItem, ...] = ()
    more: int = 0
    failed: bool = False


def find_owed(llm: LlmClient, state: dict[str, object]) -> OwedOutcome:
    """Ask the LLM about one project's snapshot state. Never raises: a failure is an outcome."""
    try:
        reply = llm.complete(SYSTEM, _prompt(state), OwedItems)
    except LlmError:
        return OwedOutcome(failed=True)
    valid = [item for item in reply.items if _citation_holds(item, state)]
    return OwedOutcome(tuple(valid[:MAX_ITEMS]), max(0, len(valid) - MAX_ITEMS))


def _prompt(state: dict[str, object]) -> str:
    facts = {name: _plain(state.get(name)) for name in INPUT_FIELDS}
    return json.dumps({"project": state.get("Name"), "pm_notes": state.get("PM Notes") or "", **facts}, ensure_ascii=False)


def _plain(value: object) -> object:
    if isinstance(value, list):
        return [str(r.get("name") or r["id"]) if isinstance(r, dict) else str(r) for r in value]
    return value


def _citation_holds(item: OwedItem, state: dict[str, object]) -> bool:
    """At least one citation, and every citation given checks out."""
    if not item.text.strip() or (item.notes_line is None and item.empty_property is None):
        return False
    if item.notes_line is not None and not _line_in_notes(item.notes_line, state.get("PM Notes")):
        return False
    if item.empty_property is not None:
        if item.kind not in PROPERTY_KINDS or item.empty_property not in INPUT_FIELDS or not is_empty(state.get(item.empty_property)):
            return False
    return True


def _line_in_notes(cited: str, notes: object) -> bool:
    wanted = _bare(cited)
    return bool(wanted) and isinstance(notes, str) and any(_bare(ln) == wanted for ln in notes.splitlines())


def _bare(text: str) -> str:
    return squash(text).lstrip("•-* ").strip()


def is_empty(value: object) -> bool:
    if isinstance(value, dict):
        return not value.get("start")
    if isinstance(value, str):
        return not squash(value)
    return not value
