"""Leave clash Concerns (spec #40, ticket #49).

Once per brief run, one LLM call parses the "Upcoming Holidays and Leaves" lines of the Projects
database description into entries (who, kind, start, end, quote). The call sees those lines and
today's date and nothing about projects. Code checks each entry: the quote must be an exact input
line (a leading bullet mark aside) and the dates must be valid, with start no later than end; an entry that fails is dropped alone.

Each leave entry's name is resolved to a Contacts page with ``people_match`` (onboarded Contacts
first). A leave clashes with a project when that page is in the project's team relations and the
leave comes within ``clash_days`` of the project's Playdate, Submission Deadline or Archival Deadline.
A holiday or an office closure (kind holiday, or who ALL) never clashes. The Concern text comes from a
code template; names that matched nobody are listed for Cyril's digest only.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from pydantic import BaseModel
from t6.privacy import Masked, Session
from t6.domain.dateformats import flag_date

from .contacts import Contact
from .flags import ARCHIVAL, PLAYDATE, SUBMISSION, stored_day
from .llm import LlmClient, LlmError
from .people_match import MatchSettings, match_person
from .masking import Masker, mask_and_read_back
from .snapshots import RELATION_FIELDS, squash

DEFAULT_CLASH_DAYS = 14
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


def payload(lines: Iterable[str], today: date, session: Session) -> dict:
    """What leaves the machine: the leave lines and today's date, and nothing about projects."""
    return {"today": today.isoformat(), "lines": [session.mask(line).text for line in lines]}


def parse_leaves(llm: LlmClient, lines: list[str], today: date, *, masker: Masker = Masker()) -> LeaveResult:
    """One call. Never raises: a failed call is ``failed``; an entry that fails its checks is dropped."""
    session = masker.session()
    data = payload(lines, today, session)
    try:
        reply = llm.complete("leaves-v1", SYSTEM, Masked(json.dumps(data, ensure_ascii=False)), LeaveParse, session)
    except LlmError:
        return LeaveResult(failed=True)
    # The reply has its names restored (an id reads "[id]"), so the quote is checked against the lines as
    # they read back. Exact lines: a leading bullet mark and the line's ends may differ, nothing inside it.
    exact = {_BULLET.sub("", mask_and_read_back(session, ln)[1].strip()) for ln in lines}
    kept: list[Leave] = []
    for item in reply.entries:
        who = item.who
        if item.kind not in KINDS or not squash(who) or _BULLET.sub("", item.quote.strip()) not in exact:
            continue
        try:
            start, end = date.fromisoformat(item.start), date.fromisoformat(item.end)
        except ValueError:
            continue
        if start <= end:
            kept.append(Leave(squash(who), item.kind, start, end))
    return LeaveResult(tuple(dict.fromkeys(kept)))


@dataclass(frozen=True)
class LeaveClashes:
    concerns: dict[str, tuple[str, ...]]  # project page id -> clash Concern texts
    unmatched: tuple[str, ...] = ()  # leave names no Contacts page matched


def leave_clashes(
    entries: Iterable[Leave],
    contacts: Iterable[Contact],
    states: dict[str, dict[str, object]],
    today: date,
    clash_days: int = DEFAULT_CLASH_DAYS,
    matching: MatchSettings = MatchSettings(),
) -> LeaveClashes:
    """Clash Concerns by project, and the leave names nobody matched. Leaves already over are ignored."""
    contacts = list(contacts)
    concerns: dict[str, list[str]] = {}
    unmatched: list[str] = []
    for leave in entries:
        if leave.kind != "leave" or leave.who == ALL or leave.end < today:
            continue
        person = match_person(leave.who, contacts, matching)
        if person is None:
            if leave.who not in unmatched:
                unmatched.append(leave.who)
            continue
        for page_id, state in states.items():
            if not any(isinstance(r, dict) and str(r.get("id")) == person.page_id
                       for field in RELATION_FIELDS for r in state.get(field) or []):
                continue
            near = _nearest_date(leave, state, clash_days)
            if near is not None:
                name, day = near
                concerns.setdefault(page_id, []).append(
                    f"{person.name} is on leave {_span(leave)}, within {clash_days} days of the {name} ({_show(day)})"
                )
    return LeaveClashes({p: tuple(dict.fromkeys(t)) for p, t in concerns.items()}, tuple(unmatched))


def _nearest_date(leave: Leave, state: dict[str, object], clash_days: int) -> tuple[str, date] | None:
    """The project date closest to the leave, if any is within ``clash_days`` of it (inclusive)."""
    best: tuple[int, str, date] | None = None
    for name in (PLAYDATE, SUBMISSION, ARCHIVAL):
        day = stored_day(state.get(name))
        if day is None:
            continue
        gap = max((leave.start - day).days, (day - leave.end).days, 0)
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
    return flag_date(when)


