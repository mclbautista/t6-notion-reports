"""Leave clash Concerns (spec #40, ticket #49).

Once per brief run, one LLM call (through ``llm run``) parses the "Upcoming Holidays and Leaves" lines of the Projects
database description into entries (who, kind, start, end, quote). The call sees those lines and
today's date and nothing about projects. Code checks each entry: the quote must be an exact input
line (a leading bullet mark aside) and the dates must be valid, with start no later than end; an entry that fails is dropped alone.

Each leave entry's name is resolved to a Contacts page by the t6 API (``person find``, one call for all the
names of the run; a match that needs review is unmatched). A leave clashes with a project when that page is in the project's team relations (not Prod Coor/Supe, a client) and the
leave starts on or before the project's Playdate, Submission Deadline or Archival Deadline and ends no more
than ``clash_days`` days before it. A leave that starts after the date never clashes.
A holiday or an office closure (kind holiday, or who ALL) never clashes. The Concern text comes from a
code template; names that matched nobody are listed for the owner's digest only.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from pydantic import BaseModel

from .flags import ARCHIVAL, PLAYDATE, SUBMISSION, stored_day
from .llm_api import LlmClient, LlmError, same_text
from .snapshots import RELATION_FIELDS, squash

DEFAULT_CLASH_DAYS = 14
# The team relations a leave can clash through. Prod Coor/Supe is the production's post supervisor, a client.
TEAM_FIELDS = tuple(field for field in RELATION_FIELDS if field != "Prod Coor/Supe")
ALL = "ALL"
KINDS = ("leave", "holiday")
UNAVAILABLE_NOTE = "⚠️ Leaves unavailable: no leave clash Concerns this run"
NO_HEADING_NOTE = "⚠️ Leaves unavailable: no Upcoming Holidays and Leaves heading in the Projects description"
_BULLET = re.compile(r"^[•\-*·–]+\s*")
UNMATCHED_NOTE = "⚠️ Unmatched leave names (add an alias in Contacts): "

SYSTEM = """You read the "Upcoming Holidays and Leaves" list of a post-production studio. You are given its lines (each is free text) and today's date.
Return one entry per person per line. If a line names several people, give each person their own entry with the same quote.
- who: the person's name or nickname exactly as the line writes it, or "ALL" when the line is for everyone (a holiday or an office closure).
- kind: "leave" when a person is away, "holiday" for a public holiday or an office closure.
- start and end: ISO dates (YYYY-MM-DD). A single day has the same start and end. A date without a year is the nearest one to today's date.
- quote: the line copied exactly, character for character.
Use only what the lines say. Skip a line that has no readable date. If nothing applies, return an empty list."""


class LeaveEntry(BaseModel):
    who: str = ""
    kind: str = ""
    start: str = ""
    end: str = ""
    quote: str = ""


class LeaveParse(BaseModel):
    entries: list[LeaveEntry] = []


@dataclass(frozen=True)
class Leave:
    who: str  # as written, or ALL
    kind: str  # leave | holiday
    start: date
    end: date


@dataclass(frozen=True)
class LeaveResult:
    entries: tuple[Leave, ...] = ()
    failed: bool = False


def payload(lines: Iterable[str], today: date) -> dict:
    """What goes to the API: the leave lines and today's date, and nothing about projects. The API masks the names."""
    return {"today": today.isoformat(), "lines": list(lines)}


def parse_leaves(llm: LlmClient, lines: list[str], today: date) -> LeaveResult:
    """One call. Never raises: a failed call is ``failed``; an entry that fails its checks is dropped."""
    data = payload(lines, today)
    try:
        reply = llm.complete(SYSTEM, json.dumps(data, ensure_ascii=False), LeaveParse)
    except LlmError:
        return LeaveResult(failed=True)
    # Exact lines: a leading bullet mark and the line's ends may differ, nothing inside it.
    exact = [_BULLET.sub("", ln.strip()) for ln in data["lines"]]
    kept: list[Leave] = []
    for item in reply.value.entries:
        who = item.who
        if (item.kind not in KINDS or not squash(who)
                or not any(same_text(_BULLET.sub("", item.quote.strip()), line) for line in exact)):
            continue
        try:
            start, end = date.fromisoformat(item.start), date.fromisoformat(item.end)
        except ValueError:
            continue
        if start <= end:
            kept.append(Leave(squash(who), item.kind, start, end))
    # An entry the API had to drop (a placeholder it could not restore) is only a failure when nothing else was kept.
    return LeaveResult(tuple(dict.fromkeys(kept)), failed=bool(reply.dropped) and not kept)


@dataclass(frozen=True)
class LeaveClashes:
    concerns: dict[str, tuple[str, ...]]  # project page id -> clash Concern texts
    unmatched: tuple[str, ...] = ()  # leave names no Contacts page matched


def leave_clashes(
    entries: Iterable[Leave],
    notion,
    states: dict[str, dict[str, object]],
    today: date,
    clash_days: int = DEFAULT_CLASH_DAYS,
) -> LeaveClashes:
    """Clash Concerns by project, and the leave names nobody matched. Leaves already over are ignored.

    ``notion.find_people`` answers for all the names at once; if it raises (the API is down) so does this.
    """
    current = [e for e in entries if e.kind == "leave" and e.who != ALL and e.end >= today]
    names = list(dict.fromkeys(e.who for e in current))
    people = dict(zip(names, notion.find_people(names))) if names else {}
    concerns: dict[str, list[str]] = {}
    unmatched: list[str] = []
    for leave in current:
        person = people[leave.who]
        if person is None:
            if leave.who not in unmatched:
                unmatched.append(leave.who)
            continue
        for page_id, state in states.items():
            ids = {person.page_id, person.t6_id}
            if not any(isinstance(r, dict) and str(r.get("id")) in ids
                       for field in TEAM_FIELDS for r in state.get(field) or []):
                continue
            near = _nearest_date(leave, state, clash_days)
            if near is not None:
                name, day = near
                concerns.setdefault(page_id, []).append(
                    f"{person.name} is on leave {_span(leave)}, within {clash_days} days of the {name} ({_show(day)})"
                )
    return LeaveClashes({p: tuple(dict.fromkeys(t)) for p, t in concerns.items()}, tuple(unmatched))


def _nearest_date(leave: Leave, state: dict[str, object], clash_days: int) -> tuple[str, date] | None:
    """The project date closest on or after ``leave.start`` within ``clash_days`` after ``leave.end``."""
    best: tuple[int, str, date] | None = None
    for name in (PLAYDATE, SUBMISSION, ARCHIVAL):
        day = stored_day(state.get(name))
        if day is None or day < leave.start:
            continue
        gap = max((day - leave.end).days, 0)
        if gap <= clash_days and (best is None or gap < best[0]):
            best = (gap, name, day)
    return None if best is None else best[1:]


def _span(leave: Leave) -> str:
    if leave.start == leave.end:
        return f"{leave.start:%b} {leave.start.day}"
    if leave.start.month == leave.end.month and leave.start.year == leave.end.year:
        return f"{leave.start:%b} {leave.start.day}–{leave.end.day}"
    return f"{leave.start:%b} {leave.start.day} – {leave.end:%b} {leave.end.day}"


def _show(when: date) -> str:
    return f"{when:%b} {when.day}, {when.year}"


