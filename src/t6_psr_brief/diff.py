"""Diff two snapshots: added, removed and per-field changes."""
from __future__ import annotations

from dataclasses import dataclass

from .snapshots import squash


@dataclass(frozen=True)
class FieldChange:
    field: str
    old: object
    new: object


@dataclass
class ProjectChange:
    page_id: str
    name: str
    kind: str  # "added" | "removed" | "changed"
    changes: list[FieldChange]
    coordinators: list[str]


def diff_snapshots(
    old: dict[str, dict[str, object]],
    new: dict[str, dict[str, object]],
) -> list[ProjectChange]:
    results: list[ProjectChange] = []
    for page_id in sorted(set(old) | set(new)):
        o, n = old.get(page_id), new.get(page_id)
        if o is None and n is not None:
            results.append(ProjectChange(page_id, _name(n), "added", [], _coords(n)))
        elif n is None and o is not None:
            results.append(ProjectChange(page_id, _name(o), "removed", [], _coords(o)))
        elif o is not None and n is not None:
            changes = [
                FieldChange(f, o.get(f), n.get(f))
                for f in sorted(set(o) | set(n))
                if _cmp(o.get(f)) != _cmp(n.get(f))
            ]
            if changes:
                results.append(ProjectChange(page_id, _name(n), "changed", changes, _coords(n)))
    return results


def group_by_coordinator(changes: list[ProjectChange]) -> dict[str, list[ProjectChange]]:
    """A project with several coordinators appears under each; none goes to '(unassigned)'."""
    grouped: dict[str, list[ProjectChange]] = {}
    for ch in changes:
        for who in ch.coordinators or ["(unassigned)"]:
            grouped.setdefault(who, []).append(ch)
    return grouped


def _cmp(value: object) -> object:
    """Comparison form: whitespace-insensitive; relations compared by id, order-insensitive."""
    if isinstance(value, str):
        return squash(value)
    if isinstance(value, list):
        return sorted(r["id"] if isinstance(r, dict) else str(r) for r in value)
    return value


def _name(state: dict[str, object]) -> str:
    code = state.get("Project Code")
    name = state.get("Name")
    return f"{code} - {name}" if code and name else str(name or code or "(unnamed)")


def _coords(state: dict[str, object]) -> list[str]:
    value = state.get("T6 Post Coordinator") or []
    return [str(r.get("name") or r["id"]) if isinstance(r, dict) else str(r) for r in value]
