"""Build the turnover Job Orders reports (11:30, 19:00 and 05:00) as Telegram HTML (#73, replaces #20 section 6).

Pure: no network, no clock, no database. ``build_end_of_shift`` takes the snapshot just taken and the
older snapshot that bounds the window, and returns the messages. Task text goes through a
``TaskSummarizer``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple
from datetime import datetime

from .briefing import BANNER, NOTION_PAGE
from .jo_snapshots import MANILA, JoSnapshot
from .task_summary import FirstWordsSummarizer, TaskSummarizer, for_page, task_label
from .telegram_html import Block, Line, bold, line, link, render_messages

SEP = " · "

DONE, CANCELLED = "Done", "Cancelled"
NOT_STARTED = ("Needs Intake Review", "Pending")
STARTED = ("In progress", "Needs Completion Review", DONE)

# Section order after TOC and AE (COOR and ADMIN share one section); any other Department value follows
# them under its own name, then "No Department". Counts in the tally are per department, in this order.
OTHER_DEPARTMENTS = ("OE", "COOR", "COLOR", "VFX", "SOUND", "ADMIN")
NO_DEPARTMENT = "No department"
SLOT_LABEL = {"0500": "05:00", "1130": "11:30", "1900": "19:00", "1200": "12:00", "1800": "18:00", "manual": "manual"}

SECTIONS = (  # (heading, departments in it), after TOC and AE
    ("⚫️ For OE", ("OE",)),
    ("🟡 For COLOR", ("COLOR",)),
    ("🟠 For SOUND", ("SOUND",)),
    ("🟤 For VFX", ("VFX",)),
    ("🟣 For COOR & ADMIN", ("COOR", "ADMIN")),
)
NO_DEPARTMENT_HEADING = "🔴 No Department"
FLAGGED_HEALTH = ("🔴 Blocked", "⚠️ Delayed", "🔥 Urgent")
UNASSIGNED = "Unassigned"


class Slot(NamedTuple):
    title: str
    toc_heading: str
    opens_kind: str  # kind of the snapshot that opens the window


SLOTS = {
    "1130": Slot("MID SHIFT DAY REPORT", "🔵 For TOC (Day Shift)", "0500"),
    "1900": Slot("END OF SHIFT REPORT (DAY SHIFT)", "🔵 For TOC (Night Shift)", "1130"),
    "0500": Slot("END OF SHIFT REPORT (NIGHT SHIFT)", "🔵 For TOC (Day Shift)", "1900"),
}
AE_HEADING = "🟢 For AE"

Fields = dict[str, object]


# ---- reusable pieces ----------------------------------------

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


def jo_id_text(fields: Fields) -> str:
    """``<project code> #<JO Number>``: several projects joined with "/", none gives "no project", no number "#?"."""
    names = [str(r.get("name") or r["id"]) for r in fields.get("Projects") or [] if isinstance(r, dict)]
    number = str(fields.get("JO Number") or "").strip()
    return f"{'/'.join(names) or 'no project'} #{number or '?'}"


def assignee_names(fields: Fields) -> list[str]:
    return [str(r.get("name") or r["id"]) for r in fields.get("Assigned") or [] if isinstance(r, dict)]


def turnover_line(page_id: str, fields: Fields, summarizer: TaskSummarizer, owner: str | None) -> Line:
    """``[health ]<JO ID> - <task> - <Status> - <deadline>[ - also: <names>]``; the JO ID links to Notion."""
    health = fields.get("Health")
    others = [n for n in assignee_names(fields) if n != owner]
    parts: list = [f"{health} " if health in FLAGGED_HEALTH else "",
                   link(jo_id_text(fields), page_url(page_id)),
                   f" - {task_label(fields.get('Task'), for_page(summarizer, page_id))}",
                   f" - {fields.get('Status') or 'no status'}",
                   f" - {deadline_text(fields)}"]
    if others and owner is not None:
        parts.append(f" - also: {', '.join(others)}")
    return line(*[p for p in parts if p != ""])


def assignee_blocks(items: list[tuple[str, Fields]], summarizer: TaskSummarizer) -> list[Block]:
    """One block per assignee (alphabetical, Unassigned last); a JO with several assignees is in each."""
    by_person: dict[str | None, list[tuple[str, Fields]]] = {}
    for item in items:
        for name in assignee_names(item[1]) or [None]:
            by_person.setdefault(name, []).append(item)
    people = sorted((n for n in by_person if n is not None), key=lambda n: (n.casefold(), n))
    blocks: list[Block] = []
    for person in people + ([None] if None in by_person else []):
        blocks.append(line(person or UNASSIGNED))
        blocks += [turnover_line(p, f, summarizer, person) for p, f in sort_by_deadline(by_person[person])]
    return blocks


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
    summarizer: TaskSummarizer | None = None,
) -> EndOfShiftReport:
    """``older`` None means the baseline: received only, with a note. ``compared_with`` is the
    "<slot> on <date>" text when ``older`` is a fallback rather than the expected bounding snapshot.
    ``window_start`` opens the received window when there is no ``older``. ``summarizer`` shortens the
    Task text (default: the first three words)."""
    summarizer = summarizer or FirstWordsSummarizer()
    report_slot = SLOTS[slot]
    moment = current.taken_at.astimezone(MANILA)
    blocks: list[Block] = [
        line(bold(report_slot.title)),
        line(f"{moment.strftime('%a %b %-d')} / {moment.strftime('%A')} ({moment.strftime('%H:%M')})"),
    ]

    active = [(p, f) for p, f in current.pages.items() if is_active(f)]
    groups = group_by_department(active)

    def section(heading: str, items: list[tuple[str, Fields]], always: bool = False) -> None:
        if not items and not always:
            return
        blocks.extend([Line(), line(bold(heading))])
        blocks.extend(assignee_blocks(items, summarizer) or [line("none")])

    section(report_slot.toc_heading, groups.get("TOC", []), always=True)
    section(AE_HEADING, groups.get("AE", []), always=True)
    listed = {"TOC", "AE"}
    for heading, departments in SECTIONS:
        section(heading, [i for d in departments for i in groups.get(d, [])])
        listed.update(departments)
    for department in sorted(d for d in groups if d is not None and d not in listed):
        section(department, groups[department])
    section(NO_DEPARTMENT_HEADING, groups.get(None, []))

    start = older.taken_at if older else window_start
    if start is None:
        raise ValueError("window_start is required when there is no older snapshot")
    counts = window_counts(current.pages, older.pages if older else None, start, current.taken_at)
    blocks += [Line(), line(bold(f"✅ Since {SLOT_LABEL[report_slot.opens_kind]} report"))]
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
    return (rows or [line("none")]) + [line(bold("Total"), f": {text(counts.total)}")]
