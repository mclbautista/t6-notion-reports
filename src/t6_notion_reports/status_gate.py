"""The status gate: which briefing section a project belongs to, or none.

Matching is exact on the whitespace-normalised Status option name. A project outside the gate is
dropped before anything else runs, so it never reaches the LLM or a message.
"""
from __future__ import annotations

import re

from .snapshots import squash

IN_PROGRESS = "in_progress"
ON_HOLD = "on_hold"
LEADS = "leads"

GROUPS = {
    "🚨 In-Progress": IN_PROGRESS,
    "🏎. In-Progress TVC": IN_PROGRESS,
    "Airing": IN_PROGRESS,
    "🏋️ Standby": ON_HOLD,
    "📦 Archiving": ON_HOLD,
    "🌱 Leads": LEADS,
}

_LOOKS_IN_PROGRESS = re.compile(r"in[\s\-_]*progress", re.IGNORECASE)


def gate(status: object) -> str | None:
    """``on_hold``, ``in_progress``, ``leads`` or None (excluded)."""
    return GROUPS.get(squash(str(status or "")))


def looks_like_in_progress(status: object) -> bool:
    """An excluded Status that is probably a mistyped In-Progress option, worth logging."""
    return bool(_LOOKS_IN_PROGRESS.search(str(status or "")))


def is_archiving(status: object) -> bool:
    return squash(str(status or "")) == "📦 Archiving"
