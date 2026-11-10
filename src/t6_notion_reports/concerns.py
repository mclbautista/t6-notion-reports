"""The Concern store (spec #40, ticket #46): Concerns remembered across briefs.

A Concern is keyed by project id and normalised text. The store records the brief date it was first
listed and the last brief date it appeared in. Each brief run does three things:

* a Concern in this run keeps its first date (or starts a new one if it was resolved before);
* an open Concern missing from this run is resolved, and is shown once, in the "Resolved last week"
  line, for the rest of that day's reruns;
* a Concern resolved on an earlier day is deleted.

Each Concern also records its source: ``flags`` (code, recomputed every run from stored properties),
``flags_notes`` (code, but resting on a date the project's LLM call read from the PM Notes), ``llm``
(the project's consolidation call) or ``leave`` (the leave parse). When a source could not be read this run (the
project's call failed, the leaves were unavailable), its open Concerns are neither re-listed from
stale text nor resolved: they stay open with their age, because nothing says they went away. The
exception is a ``flags_notes`` Concern whose dates have since all become stored properties: the code
can judge it again, so it is dropped from the kept ones and recomputed like any ``flags`` Concern.

Running the same brief again on the same day changes no stored date.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Callable

from .snapshots import squash

DEFAULT_CHRONIC_WEEKS = 4
CHRONIC_MARK = "🔁 chronic"
RESOLVED_LABEL = "Resolved last week"


@dataclass(frozen=True)
class AgedConcerns:
    """What one project shows: its open Concerns with their age, and the ones resolved this run."""

    open: tuple[str, ...] = ()
    resolved: tuple[str, ...] = ()


def concern_key(text: str) -> str:
    return squash(text).casefold()


FLAGS, LLM, LEAVE = "flags", "llm", "leave"
FLAGS_NOTES = "flags_notes"  # a code-made Concern that rests on a date the LLM read from the PM Notes


def ensure_source_column(conn: sqlite3.Connection) -> None:
    """Add the ``source`` column to a store made before it existed; its rows count as LLM Concerns."""
    if "source" not in {r[1] for r in conn.execute("PRAGMA table_info(concerns)")}:
        conn.execute("ALTER TABLE concerns ADD COLUMN source TEXT NOT NULL DEFAULT 'llm'")


def age_text(first_on: date, today: date, chronic_weeks: int) -> str:
    """"since Oct 26 (2 wks)", plus a marker when open for more than ``chronic_weeks`` weeks."""
    weeks = max(0, (today - first_on).days) // 7
    text = f"since {first_on:%b} {first_on.day} ({weeks} wk{'s' if weeks != 1 else ''})"
    if (today - first_on).days > chronic_weeks * 7:
        text += f" {CHRONIC_MARK}"
    return text


def update_concerns(
    conn: sqlite3.Connection,
    current: dict[str, tuple[tuple[str, str], ...]],
    today: date,
    chronic_weeks: int,
    unavailable: set[tuple[str, str]] | None = None,
    decided: Callable[[str, str], bool] = lambda page_id, text: False,
) -> dict[str, AgedConcerns]:
    """Record this run's Concerns (project id → (text, source) pairs) and return what each project shows.

    ``unavailable`` holds (project id, source) pairs whose source could not be read this run: their
    open Concerns stay open and shown, unchanged, unless ``decided(project id, text)`` says this run could
    judge that Concern anyway. Writes inside the connection's open transaction; the
    caller commits, or rolls back for a dry run.
    """
    ensure_source_column(conn)
    unavailable = unavailable or set()
    day = today.isoformat()
    rows = {
        (r[0], r[1]): r for r in conn.execute(
            "SELECT page_id, key, text, first_on, last_on, resolved_on, source FROM concerns"
        )
    }
    # Open Concerns of a source that could not be read: kept as they are, in the order they were stored.
    kept = sorted(
        (r for r in rows.values() if r[5] is None and (r[0], r[6]) in unavailable
         and not (r[6] == FLAGS_NOTES and decided(r[0], r[2]))),
        key=lambda r: (r[3], r[1]),
    )
    current = {p: list(items) for p, items in current.items()}
    for page_id, key, text, *_ , source in kept:
        current.setdefault(page_id, []).append((text, source))
    shown: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for page_id, items in current.items():
        for text, source in items:
            key = concern_key(text)
            if (page_id, key) in seen:
                continue
            seen.add((page_id, key))
            row = rows.get((page_id, key))
            if row is None:
                first = day
                conn.execute(
                    "INSERT INTO concerns (page_id, key, text, first_on, last_on, resolved_on, source)"
                    " VALUES (?, ?, ?, ?, ?, NULL, ?)", (page_id, key, text, day, day, source),
                )
            else:
                _, _, _, first, _, resolved, _ = row
                if resolved is not None and resolved < day:
                    first = day  # gone for a brief, back now: ages from today
                conn.execute(
                    "UPDATE concerns SET text = ?, first_on = ?, last_on = ?, resolved_on = NULL, source = ?"
                    " WHERE page_id = ? AND key = ?",
                    (text, first, day, source, page_id, key),
                )
            shown.setdefault(page_id, []).append(
                f"{text} · {age_text(date.fromisoformat(first), today, chronic_weeks)}"
            )

    resolved_now: dict[str, list[str]] = {}
    for (page_id, key), (_, _, text, _, _, resolved, _) in sorted(rows.items()):
        if (page_id, key) in seen:
            continue
        if resolved is None:
            conn.execute("UPDATE concerns SET resolved_on = ? WHERE page_id = ? AND key = ?", (day, page_id, key))
            resolved = day
        if resolved == day:
            resolved_now.setdefault(page_id, []).append(text)
        elif resolved < day:
            conn.execute("DELETE FROM concerns WHERE page_id = ? AND key = ?", (page_id, key))
    return {
        p: AgedConcerns(tuple(shown.get(p, ())), tuple(resolved_now.get(p, ())))
        for p in shown.keys() | resolved_now.keys()
    }
