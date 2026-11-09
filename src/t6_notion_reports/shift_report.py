"""Build the end-of-shift Job Orders reports (19:00 and 05:00) as Telegram HTML (spec 4, 6.1, 6.3, 6.4).

Pure: no network, no clock, no database. ``build_end_of_shift`` takes the snapshot just taken and the
older snapshot that bounds the window, and returns the messages. The line format, department
grouping, deadline sort and footer are public helpers so the 11:30 report can reuse them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple
from datetime import datetime

from .briefing import BANNER, NOTION_PAGE
from .jo_snapshots import MANILA, JoSnapshot
from .telegram_html import Block, Line, bold, line, link, render_messages

SEP = " · "

DONE, CANCELLED = "Done", "Cancelled"
NOT_STARTED = ("Needs Intake Review", "Pending")
STARTED = ("In progress", "Needs Completion Review", DONE)

# TOC and AE always appear; these follow in this order and are left out when empty.
OTHER_DEPARTMENTS = ("OE", "COOR", "COLOR", "VFX", "SOUND", "ADMIN")
NO_DEPARTMENT = "No department"
SLOT_LABEL = {"0500": "05:00", "1130": "11:30", "1900": "19:00", "1200": "12:00", "1800": "18:00", "manual": "manual"}


class Slot(NamedTuple):
    title: str
    toc_heading: str
    ae_heading: str
    window_name: str
    opens_kind: str  # kind of the snapshot that opens the window


SLOTS = {
    "1900": Slot("Day shift end of shift", "TOC tonight", "AE tomorrow", "Day shift window", "0500"),
    "0500": Slot("Night shift end of shift", "TOC to day shift", "AE", "Night shift window", "1900"),
}

Fields = dict[str, object]


# ---- reusable pieces (the 11:30 report builds on these) ----------------------------------------

def parse_instant(value: object) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) and value else None


def is_active(fields: Fields) -> bool:
    return fields.get("Status") not in (DONE, CANCELLED)


def page_url(page_id: str) -> str:
    return NOTION_PAGE + page_id.replace("-", "")


def deadline_text(fields: Fields) -> str:
    moment = parse_instant(fields.get("Deadline"))
    return "no deadline" if moment is None else moment.astimezone(MANILA).strftime("%a %b %-d %H:%M")


def _names(refs: object, empty: str) -> str:
    names = [str(r.get("name") or r["id"]) for r in refs or [] if isinstance(r, dict)]
    return ", ".join(names) if names else empty


def project_text(fields: Fields) -> str:
    return _names(fields.get("Projects"), "no project")


def assignee_text(fields: Fields) -> str:
    return _names(fields.get("Assigned"), "unassigned")


def sort_by_deadline(items: list[tuple[str, Fields]]) -> list[tuple[str, Fields]]:
    """Earliest deadline first, no deadline last, ties by JO number then page id."""
    def key(item: tuple[str, Fields]):
        page_id, f = item
        moment = parse_instant(f.get("Deadline"))
        number = str(f.get("JO Number") or "")
        return (moment is None, moment or datetime.max.replace(tzinfo=MANILA),
                (0, int(number), "") if number.isdigit() else (1, 0, number), page_id)
    return sorted(items, key=key)


def department_of(fields: Fields) -> str | None:
    department = fields.get("Department")
    return str(department) if department else None


def group_by_department(items: list[tuple[str, Fields]]) -> dict[str | None, list[tuple[str, Fields]]]:
    groups: dict[str | None, list[tuple[str, Fields]]] = {}
    for item in items:
        groups.setdefault(department_of(item[1]), []).append(item)
    return groups


def department_order(present: set[str | None]) -> list[str | None]:
    """TOC, AE, OE, COOR, COLOR, VFX, SOUND, ADMIN, any other value, then no department."""
    known = ("TOC", "AE") + OTHER_DEPARTMENTS
    extras = sorted(d for d in present if d is not None and d not in known)
    return [d for d in known if d in present] + extras + ([None] if None in present else [])


def jo_line(page_id: str, fields: Fields) -> Line:
    """JO number, project, task, Status, Health, deadline, assignee; the JO number links to Notion."""
    number = str(fields.get("JO Number") or "").strip()
    return line(
        link(f"JO #{number}" if number else "JO", page_url(page_id)),
        SEP + project_text(fields),
        SEP + str(fields.get("Task") or "no task"),
        SEP + str(fields.get("Status") or "no status"),
        SEP + str(fields.get("Health") or "no health"),
        SEP + deadline_text(fields),
        SEP + assignee_text(fields),
    )


def last_edit_footer(page_ids: set[str], pages: dict[str, Fields]) -> Line:
    """``Last JO edit: <time>``: the newest last-edited time among the JOs counted in the report."""
    edits = [m for p in page_ids if (m := parse_instant(pages[p].get("last_edited_time")))]
    text = max(edits).astimezone(MANILA).strftime("%a %b %-d %H:%M") if edits else "none"
    return line(f"Last JO edit: {text}")


# ---- window counts (spec 4) --------------------------------------------------------------------

@dataclass(frozen=True)
class Counts:
    received: int = 0
    processed: int = 0
    accomplished: int = 0

    def __add__(self, other: Counts) -> Counts:
        return Counts(self.received + other.received, self.processed + other.processed,
                      self.accomplished + other.accomplished)

    @property
    def zero(self) -> bool:
        return not (self.received or self.processed or self.accomplished)


@dataclass(frozen=True)
class WindowCounts:
    by_department: dict[str | None, Counts]
    counted: frozenset[str]  # page ids behind any non-zero count

    @property
    def total(self) -> Counts:
        return sum(self.by_department.values(), Counts())


def window_counts(
    newer: dict[str, Fields], older: dict[str, Fields] | None, start: datetime, end: datetime,
) -> WindowCounts:
    """Received, processed and accomplished between two snapshots, by Department.

    ``older`` None (the baseline) gives received only. Received is ``Date`` in ``(start, end]``.
    Cancelled JOs, judged by their Status in the newer snapshot, are in no count.
    """
    totals: dict[str | None, Counts] = {}
    counted: set[str] = set()
    for page_id, f in newer.items():
        status = f.get("Status")
        if status == CANCELLED:
            continue
        posted = parse_instant(f.get("Date"))
        received = int(posted is not None and start < posted <= end)
        processed = accomplished = 0
        if older is not None:
            before = older.get(page_id, {}).get("Status") if page_id in older else None
            absent = page_id not in older
            processed = int(status in STARTED and (absent or before in NOT_STARTED))
            accomplished = int(status == DONE and (absent or before != DONE))
        row = Counts(received, processed, accomplished)
        if row.zero:
            continue
        key = department_of(f)
        totals[key] = totals.get(key, Counts()) + row
        counted.add(page_id)
    return WindowCounts(totals, frozenset(counted))


# ---- the report --------------------------------------------------------------------------------

@dataclass(frozen=True)
class EndOfShiftReport:
    messages: list[str]
    unfinished: int
    counts: Counts


def build_end_of_shift(
    slot: str,
    current: JoSnapshot,
    older: JoSnapshot | None,
    compared_with: str | None = None,
    banner: bool = False,
    window_start: datetime | None = None,
) -> EndOfShiftReport:
    """``older`` None means the baseline: received only, with a note. ``compared_with`` is the
    "<slot> on <date>" text when ``older`` is a fallback rather than the expected bounding snapshot.
    ``window_start`` opens the received window when there is no ``older``."""
    report_slot = SLOTS[slot]
    moment = current.taken_at.astimezone(MANILA)
    blocks: list[Block] = [line(bold(f"{report_slot.title} · {moment.strftime('%a %b %-d')}"))]

    active = [(p, f) for p, f in current.pages.items() if is_active(f)]
    groups = group_by_department(active)
    headings = {"TOC": report_slot.toc_heading, "AE": report_slot.ae_heading, None: NO_DEPARTMENT}
    shown = [d for d in department_order(set(groups) | {"TOC", "AE"})
             if d in ("TOC", "AE") or groups.get(d)]
    for department in shown:
        rows = sort_by_deadline(groups.get(department, []))
        blocks += [Line(), line(bold(headings.get(department, department)))]
        blocks += [jo_line(p, f) for p, f in rows] or [line("none")]

    start = older.taken_at if older else window_start
    if start is None:
        raise ValueError("window_start is required when there is no older snapshot")
    counts = window_counts(current.pages, older.pages if older else None, start, current.taken_at)
    blocks += [Line(), line(bold(report_slot.window_name))]
    if older is None:
        blocks.append(line("First snapshot: processed and accomplished start next time."))
    elif compared_with:
        blocks.append(line(f"Compared with {compared_with}"))
    blocks += _count_blocks(counts, older is not None)

    shown_ids = {p for p, _ in active} | set(counts.counted)
    blocks += [Line(), last_edit_footer(shown_ids, current.pages)]
    messages = render_messages(blocks, prefix=BANNER if banner else "")
    return EndOfShiftReport(messages, len(active), counts.total)


def _count_blocks(counts: WindowCounts, with_flow: bool) -> list[Block]:
    def text(c: Counts) -> str:
        parts = [f"received {c.received}"]
        if with_flow:
            parts += [f"processed {c.processed}", f"accomplished {c.accomplished}"]
        return SEP.join(parts)

    rows = [line(f"{d or NO_DEPARTMENT}: {text(c)}")
            for d in department_order(set(counts.by_department)) if not (c := counts.by_department[d]).zero]
    return rows + [line(bold("Total"), f": {text(counts.total)}")]
