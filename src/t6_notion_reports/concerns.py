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


@dataclass(frozen=True)
class AgedConcerns:
    """What one project shows: its open Concerns with their age, and the ones closed since the last brief."""

    open: tuple[str, ...] = ()
    resolved: tuple[str, ...] = ()


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
    of the reply that is not in it is not in this brief. The API's order (oldest first) is kept."""
    return {
        page_of[p["project_id"]]: AgedConcerns(
            tuple(line_text(c) for c in p["open"]), tuple(c["text"] for c in p["closed_since_last_run"]),
        )
        for p in reply["projects"] if p["project_id"] in page_of
    }
