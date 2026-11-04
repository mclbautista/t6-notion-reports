"""Diff two snapshots: added, removed and per-field changes, PM Notes compared by block."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime

from .pm_notes import Block, last_updated, parse_pm_notes
from .schedule import MANILA
from .snapshots import LAST_EDITED, squash
from .status_gate import gate

# Project Health order inside a section; a blank one sorts last.
HEALTH_ORDER = {"critical": 0, "at risk": 1, "on track": 2, "incoming": 3}
HEALTH_BLANK = 9


@dataclass(frozen=True)
class FieldChange:
    field: str
    old: object
    new: object


@dataclass(frozen=True)
class BlockChange:
    kind: str  # "added" (new block) | "changed" (same header date, different text)
    block: Block


@dataclass
class NotesChange:
    blocks: list[BlockChange] = field(default_factory=list)
    # Only when the new note has no "As of" headers: lines added in a plain text diff.
    text_diff: list[str] | None = None


@dataclass(frozen=True)
class Person:
    id: str
    name: str


@dataclass
class ProjectChange:
    page_id: str
    name: str  # display name: code, or name, or "CODE (Name)" when a code is shared
    kind: str  # "added" | "removed" | "changed" | "unchanged" (briefing only, for flags, Concerns and the LLM)
    changes: list[FieldChange]
    coordinators: list[Person]
    health: str | None = None
    notes: NotesChange | None = None
    last_updated: date | None = None
    code: str | None = None
    title: str | None = None
    stage: str | None = None
    group: str | None = None  # status gate: on_hold | in_progress | leads
    edited: date | None = None  # Notion last-edited day (Manila)
    flags: tuple[str, ...] = ()  # deterministic flags, overdue first (flags.py)
    note_dates: tuple[str, ...] = ()  # blank dates the PM Notes state, with a "not in property" hint when precise
    concerns: tuple[str, ...] = ()  # open Concerns, each with its age (concerns.py)
    resolved: tuple[str, ...] = ()  # Concerns that no longer appear, shown once
    consolidated: bool = False  # the LLM's changes (``summary``) replace the raw ``changes`` and ``notes``
    summary: tuple[str, ...] = ()  # consolidated change lines, validated
    llm_failed: bool = False  # the LLM call failed: the raw changes are shown, marked "(LLM unavailable)"
    llm_update: date | None = None  # the "As of" date the LLM judged to be the last real update


@dataclass
class CoordinatorGroups:
    by_coordinator: dict[str, tuple[str, list[ProjectChange]]]  # id -> (name, changes)
    no_coordinator: list[ProjectChange]  # Cyril's digest only


def diff_snapshots(
    old: dict[str, dict[str, object]],
    new: dict[str, dict[str, object]],
    old_date: date | None = None,
    new_date: date | None = None,
) -> list[ProjectChange]:
    new_date = new_date or date.today()
    old_date = old_date or new_date
    names = display_names(old, new)
    results: list[ProjectChange] = []
    for page_id in set(old) | set(new):
        o, n = old.get(page_id), new.get(page_id)
        if o is None and n is not None:
            notes = _diff_notes("", n.get("PM Notes"), old_date, new_date)
            results.append(_project(page_id, names, "added", [], n, notes, new_date))
        elif n is None and o is not None:
            results.append(_project(page_id, names, "removed", [], o, None, old_date))
        elif o is not None and n is not None:
            changes = [
                FieldChange(f, o.get(f), n.get(f))
                for f in sorted(set(o) | set(n))
                if f not in ("PM Notes", LAST_EDITED) and _cmp(o.get(f)) != _cmp(n.get(f))
            ]
            notes = _diff_notes(o.get("PM Notes"), n.get("PM Notes"), old_date, new_date)
            if changes or notes:
                results.append(_project(page_id, names, "changed", changes, n, notes, new_date))
    return sorted(results, key=_sort_key)


def group_by_coordinator(changes: list[ProjectChange]) -> CoordinatorGroups:
    """A project with several coordinators appears under each; none goes only to the digest."""
    by_coordinator: dict[str, tuple[str, list[ProjectChange]]] = {}
    no_coordinator: list[ProjectChange] = []
    for ch in changes:
        if not ch.coordinators:
            no_coordinator.append(ch)
        for person in ch.coordinators:
            by_coordinator.setdefault(person.id, (person.name, []))[1].append(ch)
    return CoordinatorGroups(by_coordinator, no_coordinator)


def last_updated_text(when: date | None) -> str:
    return f"{when:%b} {when.day}" if when else "no notes yet"


def format_diff(changes: list[ProjectChange]) -> str:
    """Readable summary: one section per coordinator, then projects with none (digest only)."""
    if not changes:
        return "No changes."
    groups = group_by_coordinator(changes)
    out: list[str] = []
    for name, items in sorted(groups.by_coordinator.values(), key=lambda v: v[0].lower()):
        out += [f"== {name} ==", *_format_items(items)]
    if groups.no_coordinator:
        out += ["== No coordinator (digest only) ==", *_format_items(groups.no_coordinator)]
    return "\n".join(out)


def _format_items(items: list[ProjectChange]) -> list[str]:
    lines: list[str] = []
    for ch in items:
        lines.append(f"[{ch.kind}] {ch.name} · {ch.health or 'no Health'} · notes: {last_updated_text(ch.last_updated)}")
        for fc in ch.changes:
            lines.append(f"    {fc.field}: {_show(fc.old)} → {_show(fc.new)}")
        if ch.notes:
            for bc in ch.notes.blocks:
                lines.append(f"    PM Notes ({bc.kind}):")
                lines += [f"      {ln}" for ln in bc.block.text.splitlines()]
            for ln in ch.notes.text_diff or []:
                lines.append(f"    PM Notes {ln}")
    return lines


def _show(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(r.get("name") or r["id"]) if isinstance(r, dict) else str(r) for r in value) or "(none)"
    return "(none)" if value in (None, "") else str(value)


def _project(page_id, names, kind, changes, state, notes, snap_date) -> ProjectChange:
    return ProjectChange(
        page_id, names[page_id], kind, changes, _coords(state),
        health=state.get("Project Health") or None, notes=notes,
        last_updated=last_updated(state.get("PM Notes"), snap_date),
        code=state.get("Project Code") or None, title=state.get("Name") or None,
        stage=squash(str(state["Stage"])) if state.get("Stage") else None,
        group=gate(state.get("Status")), edited=_edited_day(state.get(LAST_EDITED)),
    )


def _edited_day(value: object) -> date | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(MANILA).date()
    except ValueError:
        return None


def unchanged_project(page_id: str, names: dict[str, str], state: dict[str, object], snap_date: date) -> ProjectChange:
    """A project with no changes, for the briefing when it has flags, Concerns or consolidated changes."""
    return _project(page_id, names, "unchanged", [], state, None, snap_date)


def sort_projects(items: list[ProjectChange]) -> list[ProjectChange]:
    return sorted(items, key=_sort_key)


def _sort_key(ch: ProjectChange):
    return (health_rank(ch.health), ch.name.lower(), ch.page_id)


def health_rank(health: str | None) -> int:
    """CRITICAL, AT RISK, ON TRACK, INCOMING, then any other value, then blank."""
    key = squash(health or "").casefold()
    if not key:
        return HEALTH_BLANK
    return HEALTH_ORDER.get(key, HEALTH_BLANK - 1)


def _diff_notes(old_text, new_text, old_date: date, new_date: date) -> NotesChange | None:
    old_text, new_text = old_text or "", new_text or ""
    old = parse_pm_notes(old_text, old_date)
    new = parse_pm_notes(new_text, new_date)
    if not new.blocks:
        if squash(old_text) == squash(new_text):
            return None
        before = [squash(ln) for ln in old_text.splitlines() if squash(ln)]
        after = [squash(ln) for ln in new_text.splitlines() if squash(ln)]
        lines = [f"+ {ln}" for ln in after if ln not in before] + [f"- {ln}" for ln in before if ln not in after]
        return NotesChange(text_diff=lines) if lines else None
    old_keys = {b.key for b in old.blocks}
    old_dates = {b.header_date for b in old.blocks if b.header_date}
    blocks = [
        BlockChange("changed" if b.header_date in old_dates else "added", b)
        for b in new.blocks if b.key not in old_keys
    ]
    return NotesChange(blocks) if blocks else None


def display_names(old, new) -> dict[str, str]:
    states: dict[str, dict[str, object]] = {**old, **new}
    codes = Counter(squash(str(s["Project Code"])) for s in states.values() if s.get("Project Code"))
    out = {}
    for page_id, s in states.items():
        code, name = s.get("Project Code"), s.get("Name")
        if code and name and codes[squash(str(code))] > 1:
            out[page_id] = f"{code} ({name})"
        else:
            out[page_id] = str(code or name or "(unnamed)")
    return out


def _cmp(value: object) -> object:
    """Comparison form: whitespace-insensitive; relations compared by id, order-insensitive."""
    if isinstance(value, str):
        return squash(value)
    if isinstance(value, list):
        return sorted(r["id"] if isinstance(r, dict) else str(r) for r in value)
    return value


def _coords(state: dict[str, object]) -> list[Person]:
    value = state.get("T6 Post Coordinator") or []
    return [
        Person(str(r["id"]), str(r.get("name") or r["id"])) if isinstance(r, dict) else Person(str(r), str(r))
        for r in value
    ]
