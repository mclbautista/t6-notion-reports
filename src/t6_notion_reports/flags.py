"""Deterministic flags for one project (spec #40, ticket #45): code only, no LLM.

t6-core decides which date is flagged for a Status (``t6.domain.project_rules.date_flags``) and who is
a required role (``missing_roles``); this module reads a snapshot state as a ``Project`` and words the
flags. In short:

    In-Progress, In-Progress TVC   blank Playdate / Submission Deadline; either one overdue
    Airing                         blank Playdate / Submission Deadline; never overdue
    Standby                        none
    Archiving                      blank or overdue Archival Deadline
    Incoming (Leads, Lead,
    Negotiations, Proposal Made)   Playdate in the past, or less than ``lead_warning_days`` away

Any Status with a blank Project Health gets the "missing health update" note, as its last flag.

A date can be overdue only strictly before today (Asia/Manila). The overdue flags come first, so the
worst news is the first line under the project. Date sanity problems (Submission before Playdate, Archival
before Submission) are not flags but Concerns, judged on the precise dates a property or the PM Notes give.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from t6.domain import Project
from t6.domain.dateformats import flag_date
from t6.domain.project_rules import (
    DEFAULT_LEAD_WARNING_DAYS, StatusGroup, date_flags, health_missing, missing_roles, status_gate, status_key,
)

from .schedule import MANILA
from .snapshots import squash

PLAYDATE = "Playdate"
SUBMISSION = "Submission Deadline"
ARCHIVAL = "Archival Deadline"
MISSING_HEALTH = "⚠️ missing health update"

# The wording is this app's. A Project attribute from t6-core -> the label a flag shows.
_DATE_LABELS = {"playdate": PLAYDATE, "submission_deadline": SUBMISSION, "archival_deadline": ARCHIVAL}
_ROLE_LABELS = {
    "editor_ids": "Editors", "assistant_editor_ids": "Assistant Editor", "online_editor_ids": "Online Editor",
    "colorist_ids": "Colorist", "sound_tech_ids": "Sound Tech", "post_coordinator_ids": "T6 Post Coordinator",
    "director_ids": "Director", "cinematographer_ids": "Cinematographer",
}
ROLES = tuple(_ROLE_LABELS.values())  # the label of every required role, in report order


def project_from_state(page_id: str, state: dict[str, object], stated: dict[str, date | None] | None = None) -> Project:
    """The Project a snapshot state describes, to run t6-core's rules on.

    ``stated`` holds dates the PM Notes give for blank date properties (label -> day, None for a fuzzy
    value); a stored date wins.
    """
    stated = stated or {}

    def day(label: str) -> date | None:
        return stored_day(state.get(label)) or stated.get(label)

    def people(label: str) -> tuple[str, ...]:
        return tuple(str(r["id"]) for r in state.get(label) or [] if isinstance(r, dict) and r.get("id"))  # type: ignore[union-attr]

    return Project(
        page_id=page_id or "-",
        name=squash(str(state.get("Name") or "")),
        project_code=squash(str(state.get("Project Code") or "")) or None,
        status=squash(str(state.get("Status") or "")) or None,
        stage=squash(str(state.get("Stage") or "")) or None,
        health=squash(str(state.get("Project Health") or "")) or None,
        playdate=day(PLAYDATE), submission_deadline=day(SUBMISSION), archival_deadline=day(ARCHIVAL),
        **{attribute: people(label) for attribute, label in _ROLE_LABELS.items()},
    )


@dataclass(frozen=True)
class ProjectFlags:
    lines: tuple[str, ...] = ()  # overdue first, then missing dates, Lead warnings and the roles line
    concerns: tuple[str, ...] = ()  # date sanity problems, one line each
    from_notes: tuple[str, ...] = ()  # those concerns that rest on a date only the PM Notes gave

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
    stated = stated or {}
    project = project_from_state("", state, stated)
    dates = {name: stored_day(state.get(name)) or stated.get(name) for name in (PLAYDATE, SUBMISSION, ARCHIVAL)}
    overdue: list[str] = []
    other: list[str] = []
    for flag in date_flags(project, today, lead_warning_days):
        name = _DATE_LABELS[flag.field]
        days = flag.days or 0
        if flag.kind == "overdue":
            overdue.append(f"🚨 {name} overdue by {days} day{'s' if days != 1 else ''}")
        elif flag.kind == "missing":
            if name not in stated:
                other.append(f"⚠️ No {name}")
        elif days == 0:
            other.append("⚠️ Playdate today: check with the client")
        else:
            other.append(f"⚠️ Playdate in {days} day{'s' if days != 1 else ''}: check with the client")
    if missing := [_ROLE_LABELS[role] for role in missing_roles(project)]:
        other.append("⚠️ Missing roles: " + ", ".join(missing))
    if health_missing(project):
        other.append(MISSING_HEALTH)  # every gated Status: the project also sorts last in its section
    # Sanity uses the effective precise dates: a fuzzy one from the notes is None here.
    concerns = _sanity(dates)
    stored = _sanity({n: d if stored_day(state.get(n)) is not None else None for n, d in dates.items()})
    return ProjectFlags(tuple(overdue + other), tuple(concerns), tuple(c for c in concerns if c not in stored))


def relevant_dates(status: object) -> tuple[str, ...]:
    """The date properties that matter for a Status: the ones a blank value could be filled in for."""
    group = status_gate(status)
    if group is StatusGroup.IN_PROGRESS:
        return (PLAYDATE, SUBMISSION)
    if status_key(status) == "archiving":
        return (ARCHIVAL,)
    if group is StatusGroup.INCOMING:
        return (PLAYDATE,)
    return ()


def stored_decides(state: dict[str, object], concern: str) -> bool:
    """Whether both dates a date-sanity Concern compares are stored properties now, so its ordering can
    be judged without the PM Notes."""
    stored = {n for n in (PLAYDATE, SUBMISSION, ARCHIVAL) if stored_day(state.get(n)) is not None}
    return any(
        concern.startswith(f"{first} (") and f") is before {second} (" in concern and {first, second} <= stored
        for first, second in ((SUBMISSION, PLAYDATE), (ARCHIVAL, SUBMISSION))
    )


def _sanity(dates: dict[str, date | None]) -> list[str]:
    out: list[str] = []
    playdate, submission, archival = dates[PLAYDATE], dates[SUBMISSION], dates[ARCHIVAL]
    if playdate and submission and submission < playdate:
        out.append(f"{SUBMISSION} ({_show(submission)}) is before {PLAYDATE} ({_show(playdate)})")
    if submission and archival and archival < submission:
        out.append(f"{ARCHIVAL} ({_show(archival)}) is before {SUBMISSION} ({_show(submission)})")
    return out


def _show(when: date) -> str:
    return flag_date(when)


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
