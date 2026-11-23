"""How the Concern lines of the brief look. The Concerns themselves live in the t6 API (``concern observe``).

Reports keeps no Concern store. Each brief hands the run's findings to the API and renders what comes back:
the open Concerns of each project with their age, and the ones closed since the last run. The wording and the
layout are here; what is open, how old it is and when it is chronic are the API's.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

CHRONIC_MARK = "🔁 chronic"
RESOLVED_LABEL = "Resolved last week"
NOT_SEEN = "not seen this week"
SNOOZED = "snoozed"  # a human operator set it aside: it is in no line of the brief until a person wakes it
MANILA = ZoneInfo("Asia/Manila")


@dataclass(frozen=True)
class AgedConcerns:
    """What one project shows: its open Concerns with their age, and the ones closed since the last brief."""

    open: tuple[str, ...] = ()
    resolved: tuple[str, ...] = ()
    open_ids: tuple[str, ...] = ()  # the Concern ID of each line of ``open``, in order; empty when the API gave none


@dataclass(frozen=True)
class RaiseConcern:
    """A Concern line of the brief ("Open Concerns" or "Closing after PSR"): its project (the label the brief shows), text,
    owner (a Contacts page id and the name shown), the party it waits on, the date of its last evidence and its age."""

    concern_id: str
    page_id: str
    project: str
    text: str
    owner_page: str | None = None
    owner: str | None = None
    waiting_on: str | None = None
    evidence_on: date | None = None
    coordinators: tuple[str, ...] = ()  # Contacts page ids of the project's coordinators: an ownerless Concern goes to their DMs
    open_days: int | None = None  # whole days since it opened, from the API's age_seconds
    closing_by: str | None = None  # "Closing after PSR" only: who closed it (a name, or "Done line"); None when unknown
    closing_on: date | None = None  # "Closing after PSR" only: the date the close was made; None for an open Concern


def age_text(opened_on: date, weeks: int, chronic: bool) -> str:
    """"since Oct 26 (2 wks)", plus the chronic mark."""
    text = f"since {opened_on:%b} {opened_on.day} ({weeks} wk{'s' if weeks != 1 else ''})"
    return f"{text} {CHRONIC_MARK}" if chronic else text


def line_text(concern: Mapping[str, Any]) -> str:
    """One open Concern of the reply as a line: text, age, and "not seen this week" when this run's findings lacked it."""
    line = f"{concern['text']} · {age_text(date.fromisoformat(concern['opened_on']), concern['weeks'], concern['chronic'])}"
    return line if concern["seen_this_run"] else f"{line} · {NOT_SEEN}"


def aged_from_reply(reply: Mapping[str, Any], page_of: Mapping[str, str]) -> dict[str, AgedConcerns]:
    """The reply of ``concern observe`` by project page id. ``page_of`` maps a T6 project id to its page id; a project
    of the reply that is not in it is not in this brief. Only the Concerns closed since the last run are lines of a
    project: an open Concern is shown once, in "Open Concerns", and a snoozed one nowhere."""
    return {
        page_of[p["project_id"]]: AgedConcerns(resolved=tuple(c["text"] for c in p["closed_since_last_run"]))
        for p in reply["projects"] if p["project_id"] in page_of
    }


def raise_concerns(raised: list[Mapping[str, Any]], texts: Mapping[str, str], labels: Mapping[str, str], page_of: Mapping[str, str],
                   contact_of: Mapping[str, str], names: Mapping[str, str],
                   coordinators_of: Mapping[str, tuple[str, ...]] = {}) -> list[RaiseConcern]:
    """The Concerns of ``raised`` (a list of ``concern list --raise-at-psr``) that belong to a project of this brief, sorted
    by project, then oldest first.

    The API decides which Concerns are listed. An open item carries no text, so ``texts`` gives the text of each Concern ID
    from ``concern list --open``; a pending close (it has ``close_at``) carries its own text, name and date. ``labels`` is the label of each
    project page id, ``page_of`` maps a T6 project id to its page id, ``contact_of`` a T6 Person ID to a Contacts page
    id, ``names`` a Contacts page id to the name shown and ``coordinators_of`` a project page id to its coordinators' Contacts page ids."""
    out = []
    for c in raised:
        page = page_of.get(c["project_id"])
        text = c.get("text") or texts.get(c["id"])
        if page is None or text is None:
            continue
        owner = contact_of.get(c.get("owner_id") or "")
        evidence = datetime.fromtimestamp(c["evidence_at"], MANILA).date() if c.get("evidence_at") is not None else None
        age = c.get("age_seconds")
        closed = c.get("close_at")
        closing_by = "Done line" if c.get("close_source") == "done_line" and not c.get("close_by_name") else c.get("close_by_name")
        out.append(RaiseConcern(c["id"], page, labels[page], text, owner, names.get(owner) if owner else None, c.get("waiting_on") or None, evidence, coordinators_of.get(page, ()),
                                None if age is None else int(age) // 86400, closing_by,
                                None if closed is None else datetime.fromtimestamp(closed, MANILA).date()))
    # Oldest first within a project: the most days open, then the earliest evidence, then the ID.
    return sorted(out, key=lambda q: (q.project.lower(), -(q.open_days or 0), q.evidence_on or date.min, q.concern_id))
