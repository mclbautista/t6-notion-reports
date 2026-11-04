"""The Concern store (spec #40, ticket #46): Concerns remembered across briefs.

A Concern is keyed by project id and normalised text. The store records the brief date it was first
listed and the last brief date it appeared in. Each brief run does three things:

* a Concern in this run keeps its first date (or starts a new one if it was resolved before);
* an open Concern missing from this run is resolved, and is shown once, in the "Resolved last week"
  line, for the rest of that day's reruns;
* a Concern resolved on an earlier day is deleted.

Running the same brief again on the same day changes no stored date.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

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


def open_concern_texts(conn: sqlite3.Connection, page_id: str) -> tuple[str, ...]:
    """The Concerns still open for a project, as stored. Used to carry them over when the LLM call failed."""
    return tuple(r[0] for r in conn.execute(
        "SELECT text FROM concerns WHERE page_id = ? AND resolved_on IS NULL ORDER BY first_on, key", (page_id,)))


def age_text(first_on: date, today: date, chronic_weeks: int) -> str:
    """"since Oct 26 (2 wks)", plus a marker when open for more than ``chronic_weeks`` weeks."""
    weeks = max(0, (today - first_on).days) // 7
    text = f"since {first_on:%b} {first_on.day} ({weeks} wk{'s' if weeks != 1 else ''})"
    if (today - first_on).days > chronic_weeks * 7:
        text += f" {CHRONIC_MARK}"
    return text


def update_concerns(
    conn: sqlite3.Connection,
    current: dict[str, tuple[str, ...]],
    today: date,
    chronic_weeks: int,
) -> dict[str, AgedConcerns]:
    """Record this run's Concerns (project id → texts) and return what each project shows.

    Writes inside the connection's open transaction; the caller commits, or rolls back for a dry run.
    """
    day = today.isoformat()
    rows = {
        (r[0], r[1]): r for r in conn.execute(
            "SELECT page_id, key, text, first_on, last_on, resolved_on FROM concerns"
        )
    }
    shown: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for page_id, texts in current.items():
        for text in texts:
            key = concern_key(text)
            if (page_id, key) in seen:
                continue
            seen.add((page_id, key))
            row = rows.get((page_id, key))
            if row is None:
                first = day
                conn.execute("INSERT INTO concerns VALUES (?, ?, ?, ?, ?, NULL)", (page_id, key, text, day, day))
            else:
                _, _, _, first, _, resolved = row
                if resolved is not None and resolved < day:
                    first = day  # gone for a brief, back now: ages from today
                conn.execute(
                    "UPDATE concerns SET text = ?, first_on = ?, last_on = ?, resolved_on = NULL"
                    " WHERE page_id = ? AND key = ?",
                    (text, first, day, page_id, key),
                )
            shown.setdefault(page_id, []).append(
                f"{text} · {age_text(date.fromisoformat(first), today, chronic_weeks)}"
            )

    resolved_now: dict[str, list[str]] = {}
    for (page_id, key), (_, _, text, _, _, resolved) in sorted(rows.items()):
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
