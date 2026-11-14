"""Each show's next Instagantt dates for the briefing, from a CSV export tied to Projects through External Links.

Read only. A missing or unreadable export, or an External Links read that fails, adds a note and never
stops the brief. Shows with no linked Project are listed for review in Cyril's digest only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

from t6.instagantt import LinkReport, Task

UNAVAILABLE_NOTE = "⚠️ Instagantt unavailable: no timeline dates this run"
UNLINKED_NOTE = "Instagantt shows not linked to a project: "
UPCOMING = 3  # tasks listed per project


@dataclass(frozen=True)
class InstaganttFeed:
    lines: dict[str, tuple[str, ...]]  # project page id -> next upcoming tasks
    notes: tuple[str, ...] = ()
    cyril_notes: tuple[str, ...] = ()


def _key(page_id: str) -> str:
    return page_id.replace("-", "").lower()


def _day(value: date) -> str:
    return f"{value:%b} {value.day}"


def task_line(task: Task) -> str:
    dates = _day(task.start) if task.end == task.start else f"{_day(task.start)} – {_day(task.end)}"
    return f"Instagantt · {task.name} · {dates}"


def instagantt_dates(
    load: Callable[[], LinkReport], states: dict[str, dict[str, object]], today: date, log: Callable[[str], None],
) -> InstaganttFeed:
    """The next ``UPCOMING`` tasks (ending today or later, by start) of each linked show in ``states``."""
    try:
        report = load()
    except Exception as exc:  # type only: the file or the page may hold text
        log(f"brief instagantt unavailable error={type(exc).__name__}")
        return InstaganttFeed({}, notes=(UNAVAILABLE_NOTE,))
    by_key = {_key(p): p for p in states}
    lines: dict[str, tuple[str, ...]] = {}
    for linked in report.linked:
        page = by_key.get(_key(linked.project_id))
        if page is None:  # a show outside the status gate
            continue
        upcoming = sorted((t for t in linked.timeline.tasks if t.end >= today), key=lambda t: (t.start, t.end, t.name))
        if upcoming:
            lines[page] = tuple(task_line(t) for t in upcoming[:UPCOMING])
    names = sorted({u.timeline.project for u in report.unmapped})
    cyril = (UNLINKED_NOTE + ", ".join(names),) if names else ()
    return InstaganttFeed(lines, cyril_notes=cyril)
