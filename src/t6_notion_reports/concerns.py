"""How the Concern lines of the brief look. The Concerns themselves live in the t6 API (``concern observe``).

Reports keeps no Concern store. Each brief hands the run's findings to the API and renders what comes back:
the open Concerns of each project with their age, and the ones closed since the last run. The wording and the
layout are here; what is open, how old it is and when it is chronic are the API's.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

CHRONIC_MARK = "🔁 chronic"
RESOLVED_LABEL = "Resolved last week"
NOT_SEEN = "not seen this week"
QUIET = "quiet"  # a Concern with no new evidence for 2 weeks: it is in "Raise at PSR", not in its project's lines
QUIET_LABEL = "no news for 2 weeks"


@dataclass(frozen=True)
class AgedConcerns:
    """What one project shows: its open Concerns with their age, and the ones closed since the last brief."""

    open: tuple[str, ...] = ()
    resolved: tuple[str, ...] = ()
    open_ids: tuple[str, ...] = ()  # the Concern ID of each line of ``open``, in order; empty when the API gave none


@dataclass(frozen=True)
class QuietConcern:
    """A Concern the API marks quiet, for "Raise at PSR": its project (the label the brief shows), text, owner (a Contacts page
    id and the name shown) and the party it waits on."""

    concern_id: str
    page_id: str
    project: str
    text: str
    owner_page: str | None = None
    owner: str | None = None
    waiting_on: str | None = None


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
    of the reply that is not in it is not in this brief. The API's order (oldest first) is kept. A quiet Concern is not
    a line of its project: "Raise at PSR" shows it."""
    return {
        page_of[p["project_id"]]: AgedConcerns(
            tuple(line_text(c) for c in p["open"] if c["state"] != QUIET), tuple(c["text"] for c in p["closed_since_last_run"]),
            tuple(c["id"] for c in p["open"] if c["state"] != QUIET),
        )
        for p in reply["projects"] if p["project_id"] in page_of
    }


def quiet_concerns(listed: list[Mapping[str, Any]], labels: Mapping[str, str], page_of: Mapping[str, str],
                   contact_of: Mapping[str, str], names: Mapping[str, str]) -> list[QuietConcern]:
    """The quiet Concerns of ``concern list`` that belong to a project of this brief, in the API's order.

    ``labels`` is the label of each project page id, ``page_of`` maps a T6 project id to its page id, ``contact_of``
    a T6 Person ID to a Contacts page id and ``names`` a Contacts page id to the name shown."""
    out = []
    for c in listed:
        page = page_of.get(c["project_id"])
        if c["state"] != QUIET or page is None:
            continue
        owner = contact_of.get(c.get("owner_id") or "")
        out.append(QuietConcern(c["id"], page, labels[page], c["text"], owner, names.get(owner) if owner else None, c.get("waiting_on") or None))
    return out
