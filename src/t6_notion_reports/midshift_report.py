"""Build the 12:00 day-shift midshift report as Telegram HTML (spec 6.1, 6.2, 6.4).

Pure: no network, no clock, no database. Reuses the line pieces, deadline sort and footer from
``shift_report``. No window counts, so no older snapshot is needed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .briefing import BANNER
from .jo_snapshots import MANILA, JoSnapshot
from .shift_report import (
    NOT_STARTED, SEP, Fields, assignee_text, deadline_text, is_active, last_edit_footer, page_url,
    parse_instant, project_text, sort_by_deadline,
)
from .telegram_html import Block, Line, bold, line, link, render_messages

TITLE = "Day shift midshift"
CARRIED_HEADING = "Carried over from night"
MORNING_HEADING = "Came in this morning"

# Flagged JOs lead each group in this order; Health is shown as Notion computed it.
BLOCKED, DELAYED, URGENT = "🔴 Blocked", "⚠️ Delayed", "🔥 Urgent"
FLAG_ORDER = (BLOCKED, DELAYED, URGENT)


@dataclass(frozen=True)
class MidshiftReport:
    messages: list[str]
    active: int
    queued: int


Item = tuple[str, Fields]


def morning_cutoff(taken_at: datetime) -> datetime:
    """Today's 05:00 Manila slot."""
    return taken_at.astimezone(MANILA).replace(hour=5, minute=0, second=0, microsecond=0)


def _posted_before(fields: Fields, cutoff: datetime) -> bool:
    """Carried over: ``Date`` before the cutoff, or no ``Date``. A date-only value counts from 00:00 Manila."""
    posted = parse_instant(fields.get("Date"))
    if posted is None:
        return True
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=MANILA)
    return posted < cutoff


def flagged_first(items: list[Item]) -> list[Item]:
    """Blocked, then delayed, then urgent, then the rest; each part sorted by deadline."""
    parts = [[i for i in items if i[1].get("Health") == flag] for flag in FLAG_ORDER]
    flagged = {id(i) for part in parts for i in part}
    parts.append([i for i in items if id(i) not in flagged])
    return [i for part in parts for i in sort_by_deadline(part)]


def queue_line(page_id: str, fields: Fields) -> Line:
    """Project, task, department, deadline, assignee, Health; the task links to Notion."""
    return line(
        project_text(fields),
        SEP,
        link(str(fields.get("Task") or "no task"), page_url(page_id)),
        SEP + str(fields.get("Department") or "no department"),
        SEP + deadline_text(fields),
        SEP + assignee_text(fields),
        SEP + str(fields.get("Health") or "no health"),
    )


def build_midshift(current: JoSnapshot, banner: bool = False) -> MidshiftReport:
    moment = current.taken_at.astimezone(MANILA)
    active = [(p, f) for p, f in current.pages.items() if is_active(f)]
    health = [f.get("Health") for _, f in active]
    queue = [i for i in active if i[1].get("Status") in NOT_STARTED]
    cutoff = morning_cutoff(current.taken_at)
    carried = [i for i in queue if _posted_before(i[1], cutoff)]
    morning = [i for i in queue if not _posted_before(i[1], cutoff)]

    blocks: list[Block] = [
        line(bold(f"{TITLE} · {moment.strftime('%a %b %-d')}")),
        line(f"Active JOs: {len(active)}{SEP}urgent {health.count(URGENT)}"
             f"{SEP}delayed {health.count(DELAYED)}{SEP}blocked {health.count(BLOCKED)}"),
    ]
    for heading, items in ((CARRIED_HEADING, carried), (MORNING_HEADING, morning)):
        blocks += [Line(), line(bold(heading))]
        blocks += [queue_line(p, f) for p, f in flagged_first(items)] or [line("none")]
    blocks += [Line(), last_edit_footer({p for p, _ in active}, current.pages)]
    return MidshiftReport(render_messages(blocks, prefix=BANNER if banner else ""), len(active), len(queue))
