"""Deterministic flags for one project (spec #40, ticket #45): code only, no LLM.

Which date is flagged depends on the Status:

    In-Progress, In-Progress TVC   blank Playdate / Submission Deadline; either one overdue
    Airing                         blank Playdate / Submission Deadline; never overdue
    Standby                        none
    Archiving                      blank or overdue Archival Deadline
    Leads                          Playdate in the past, or less than ``lead_warning_days`` away

Any Status with a blank Project Health gets the "missing health update" note, as its last flag.

A date can be overdue only strictly before today (Asia/Manila). The overdue flags come first, so the
worst news is the first line under the project. Date sanity problems (Submission before Playdate, Archival
before Submission) are not flags but Concerns, judged on the precise dates a property or the PM Notes give.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .schedule import MANILA
from .snapshots import squash

PLAYDATE = "Playdate"
SUBMISSION = "Submission Deadline"
ARCHIVAL = "Archival Deadline"
# Every required role; the missing ones are listed together on one "Missing roles" line (Cyril, #60).
ROLES = ("Editors", "Assistant Editor", "Online Editor", "Colorist", "Sound Tech", "T6 Post Coordinator",
         "Director", "Cinematographer")
DEFAULT_LEAD_WARNING_DAYS = 30
MISSING_HEALTH = "⚠️ missing health update"

_DATED_IN_PROGRESS = {"🚨 In-Progress", "🏎. In-Progress TVC"}
_AIRING = "Airing"


@dataclass(frozen=True)
class ProjectFlags:
    lines: tuple[str, ...] = ()  # overdue first, then missing dates, Lead warnings and the roles line
    concerns: tuple[str, ...] = ()  # date sanity problems, one line each

    def __bool__(self) -> bool:
        return bool(self.lines or self.concerns)


def project_flags(
    state: dict[str, object], today: date, lead_warning_days: int = DEFAULT_LEAD_WARNING_DAYS,
    stated: dict[str, date | None] | None = None,
) -> ProjectFlags:
    """Flags and date-sanity Concerns for a project's snapshot state, as of ``today`` (Manila).

    ``stated`` holds dates the PM Notes give for blank properties (property name -> day, or None for a
    fuzzy value). A stated date is not missing; a precise one is judged like the property's own, a fuzzy
    one never overdue. Date sanity looks at the precise dates, from the property or the notes.
    """
    status = squash(str(state.get("Status") or ""))
    dates = {name: stored_day(state.get(name)) for name in (PLAYDATE, SUBMISSION, ARCHIVAL)}
    stated = stated or {}
    for name, day in stated.items():
        if dates.get(name) is None:
            dates[name] = day
    overdue: list[str] = []
    other: list[str] = []

    def check(name: str, *, blank: bool, late: bool) -> None:
        when = dates[name]
        if when is None:
            if blank and name not in stated:
                other.append(f"⚠️ No {name}")
        elif late and when < today:
            days = (today - when).days
            overdue.append(f"🚨 {name} overdue by {days} day{'s' if days != 1 else ''}")

    if status in _DATED_IN_PROGRESS or status == _AIRING:
        for name in (PLAYDATE, SUBMISSION):
            check(name, blank=True, late=status != _AIRING)
        if (missing := [r for r in ROLES if not state.get(r)]):
            other.append("⚠️ Missing roles: " + ", ".join(missing))
    elif status == "📦 Archiving":
        check(ARCHIVAL, blank=True, late=True)
    elif status == "🌱 Leads":
        when = dates[PLAYDATE]
        if when is not None:
            check(PLAYDATE, blank=False, late=True)
            ahead = (when - today).days
            if 0 <= ahead < lead_warning_days:
                other.append("⚠️ Playdate today: check with the client" if ahead == 0 else
                             f"⚠️ Playdate in {ahead} day{'s' if ahead != 1 else ''}: check with the client")
    if not squash(str(state.get("Project Health") or "")):
        other.append(MISSING_HEALTH)  # every gated Status: the project also sorts last in its section
    # Sanity uses the effective precise dates: a fuzzy one from the notes is None here.
    return ProjectFlags(tuple(overdue + other), tuple(_sanity(dates)))


def relevant_dates(status: object) -> tuple[str, ...]:
    """The date properties that matter for a Status: the ones a blank value could be filled in for."""
    status = squash(str(status or ""))
    if status in _DATED_IN_PROGRESS or status == _AIRING:
        return (PLAYDATE, SUBMISSION)
    if status == "📦 Archiving":
        return (ARCHIVAL,)
    if status == "🌱 Leads":
        return (PLAYDATE,)
    return ()


def _sanity(dates: dict[str, date | None]) -> list[str]:
    out: list[str] = []
    playdate, submission, archival = dates[PLAYDATE], dates[SUBMISSION], dates[ARCHIVAL]
    if playdate and submission and submission < playdate:
        out.append(f"{SUBMISSION} ({_show(submission)}) is before {PLAYDATE} ({_show(playdate)})")
    if submission and archival and archival < submission:
        out.append(f"{ARCHIVAL} ({_show(archival)}) is before {SUBMISSION} ({_show(submission)})")
    return out


def _show(when: date) -> str:
    return f"{when:%b} {when.day}, {when.year}"


def stored_day(value: object) -> date | None:
    """The Manila day of a stored date property ({"start": ..., "end": ...}); None when blank or unreadable."""
    start = value.get("start") if isinstance(value, dict) else None
    if not start:
        return None
    try:
        if len(str(start)) <= 10:
            return date.fromisoformat(str(start))
        stamp = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        return (stamp.astimezone(MANILA) if stamp.tzinfo else stamp).date()
    except ValueError:
        return None
