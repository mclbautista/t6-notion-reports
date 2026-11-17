"""The status gate: which briefing section a project belongs to, or none.

The API decides (``status_group`` of ``project list``, see ``rules.ProjectRules``). A project outside the
gate is dropped before anything else runs, so it never reaches the LLM or a message. The section names
are the ones the layout code uses; the API's ``incoming`` is the ``leads`` section.
"""
from __future__ import annotations

import re

from .rules import ProjectRules

IN_PROGRESS = "in_progress"
ON_HOLD = "on_hold"
LEADS = "leads"

_SECTION = {"in_progress": IN_PROGRESS, "on_hold": ON_HOLD, "incoming": LEADS}

_LOOKS_IN_PROGRESS = re.compile(r"in[\s\-_]*progress", re.IGNORECASE)


def gate(rules: ProjectRules | None) -> str | None:
    """``on_hold``, ``in_progress``, ``leads`` or None (excluded)."""
    return _SECTION.get(rules.status_group) if rules else None


def looks_like_in_progress(status: object) -> bool:
    """An excluded Status that is probably a mistyped In-Progress option, worth logging."""
    return bool(_LOOKS_IN_PROGRESS.search(str(status or "")))


def is_archiving(rules: ProjectRules | None) -> bool:
    """Owner default: the word ``archiving`` is compared here, not asked of the API."""
    return rules is not None and rules.status_key == "archiving"
