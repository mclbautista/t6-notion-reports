"""The results of the Studio rules that the t6 API returns with Projects and Job Orders.

Reports applies no rule. It keeps these results in memory for the run, beside the state they describe.
They are never written into the snapshots. Field names are Notion property names where a property is
meant ("Editors"), so the layout code reads them as it reads the snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectRules:
    """What ``project list`` says of one Project."""

    t6_id: str | None  # the Project's T6 ID, where the API has bound one
    status_key: str  # the Status without emoji and punctuation, lower case ("in-progress")
    status_group: str | None  # "in_progress", "on_hold", "incoming"; None: outside the gate
    snapshotted: bool  # False for Archived and Lost
    missing_roles: tuple[str, ...]  # property names, in report order
    relevant_dates: tuple[str, ...]  # property names a blank value of could be filled in from the PM Notes


@dataclass(frozen=True)
class JoRules:
    """What ``jo list`` says of one Job Order: not Done and not Cancelled, and the Health flags
    ("Blocked", "Delayed", "Urgent"), worst first."""

    active: bool
    health_flags: tuple[str, ...] = ()
