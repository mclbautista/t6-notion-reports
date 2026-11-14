"""The status gate: which briefing section a project belongs to, or none.

t6-core owns the rule (``t6.domain.project_rules.status_gate``): In-Progress, In-Progress TVC and Airing
are in progress; Standby and Archiving are on hold; Leads, Lead, Negotiations and Proposal Made are
incoming. A project outside the gate is dropped before anything else runs, so it never reaches the LLM
or a message. This module only names the sections as the briefing does.
"""
from __future__ import annotations

import re

from t6.domain.project_rules import StatusGroup, is_snapshotted, status_gate, status_key

IN_PROGRESS = "in_progress"
ON_HOLD = "on_hold"
LEADS = "leads"  # the section for every incoming Status

_SECTION = {StatusGroup.IN_PROGRESS: IN_PROGRESS, StatusGroup.ON_HOLD: ON_HOLD, StatusGroup.INCOMING: LEADS}

_LOOKS_IN_PROGRESS = re.compile(r"in[\s\-_]*progress", re.IGNORECASE)

__all__ = ["IN_PROGRESS", "LEADS", "ON_HOLD", "gate", "is_archiving", "is_snapshotted", "looks_like_in_progress"]


def gate(status: object) -> str | None:
    """``on_hold``, ``in_progress``, ``leads`` or None (excluded)."""
    group = status_gate(status)
    return None if group is None else _SECTION[group]


def looks_like_in_progress(status: object) -> bool:
    """An excluded Status that is probably a mistyped In-Progress option, worth logging."""
    return bool(_LOOKS_IN_PROGRESS.search(str(status or "")))


def is_archiving(status: object) -> bool:
    return status_key(status) == "archiving"
