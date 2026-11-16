"""Flags for one project (spec #40, ticket #45): the API decides, this module words them.

``project flags`` (and the Project result of ``project list``) say which date flags, which missing roles and which
date sanity problems a project has, from its Status and dates. Reports keeps the wording and the show format of
dates. A date the PM Notes give for a blank property is sent as ``stated``: not missing, and a precise one is judged
like the property's own.

The overdue flags come first, so the worst news is the first line under the project. Date sanity problems are not flags
but Concerns, judged on the precise dates a property or the PM Notes give.

A blank Project Health gets the "missing health update" note, as its last flag. The API's ``health_missing`` reads the
Notion property "Health", not "Project Health" that reports reads, so this one is still decided here (an API gap).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping

from .rules import ProjectRules
from .schedule import MANILA
from .snapshots import squash

PLAYDATE = "Playdate"
SUBMISSION = "Submission Deadline"
ARCHIVAL = "Archival Deadline"
DEFAULT_LEAD_WARNING_DAYS = 30
MISSING_HEALTH = "⚠️ missing health update"

_FIELD = {"playdate": PLAYDATE, "submission_deadline": SUBMISSION, "archival_deadline": ARCHIVAL}


@dataclass(frozen=True)
class ProjectFlags:
    lines: tuple[str, ...] = ()  # overdue first, then missing dates, Lead warnings and the roles line
    concerns: tuple[str, ...] = ()  # date sanity problems, one line each
    from_notes: tuple[str, ...] = ()  # those concerns that rest on a date only the PM Notes gave

    def __bool__(self) -> bool:
        return bool(self.lines or self.concerns)


def project_flags(state: dict[str, object], rules: ProjectRules, result: Mapping[str, Any]) -> ProjectFlags:
    """The flag lines and date-sanity Concerns of a project: the ``project flags`` ``result``, worded."""
    overdue: list[str] = []
    other: list[str] = []
    for flag in result["flags"]:
        name = _FIELD.get(flag["field"])
        if name is None:  # the API's health flag: see the module note
            continue
        days = flag.get("days")
        if flag["kind"] == "overdue":
            overdue.append(f"🚨 {name} overdue by {days} day{'s' if days != 1 else ''}")
        elif flag["kind"] == "upcoming":
            other.append("⚠️ Playdate today: check with the client" if days == 0 else
                         f"⚠️ Playdate in {days} day{'s' if days != 1 else ''}: check with the client")
        else:
            other.append(f"⚠️ No {name}")
    if rules.missing_roles:
        other.append("⚠️ Missing roles: " + ", ".join(rules.missing_roles))
    if not squash(str(state.get("Project Health") or "")):
        other.append(MISSING_HEALTH)  # every gated Status: the project also sorts last in its section
    concerns = [(_sanity_text(p), p["from_notes"]) for p in result["sanity"]]
    return ProjectFlags(
        tuple(overdue + other), tuple(t for t, _ in concerns), tuple(t for t, from_notes in concerns if from_notes),
    )


def _sanity_text(problem: Mapping[str, Any]) -> str:
    first, second = (_FIELD[problem[k]] for k in ("first", "second"))
    first_on, second_on = (date.fromisoformat(problem[k]) for k in ("first_date", "second_date"))
    return f"{first} ({_show(first_on)}) is before {second} ({_show(second_on)})"


def stored_decides(state: dict[str, object], concern: str) -> bool:
    """Whether both dates a date-sanity Concern compares are stored properties now, so its ordering can
    be judged without the PM Notes."""
    stored = {n for n in (PLAYDATE, SUBMISSION, ARCHIVAL) if stored_day(state.get(n)) is not None}
    return any(
        concern.startswith(f"{first} (") and f") is before {second} (" in concern and {first, second} <= stored
        for first, second in ((SUBMISSION, PLAYDATE), (ARCHIVAL, SUBMISSION))
    )


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
