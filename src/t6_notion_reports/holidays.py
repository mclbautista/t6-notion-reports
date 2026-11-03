"""Pre-fill ⚫️ Others from the Projects database description ("Upcoming Holidays and Leaves")."""
from __future__ import annotations

import re
from datetime import date, timedelta

HEADING = "upcoming holidays and leaves"
_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")
_BULLET = re.compile(r"^\s*(?:[•\-*·–]+|\d+[.)])\s*")
# "Nov 12", "Nov 12-14", "Nov 30 - Dec 2", "November 30 to Dec 2"
_RANGE = re.compile(
    r"\b(?P<m1>[A-Za-z]{3,9})\.?\s+(?P<d1>\d{1,2})(?!\d)"
    r"(?:\s*(?:-|–|—|to|until)\s*(?:(?P<m2>[A-Za-z]{3,9})\.?\s+)?(?P<d2>\d{1,2})(?!\d))?",
    re.IGNORECASE,
)


def upcoming_entries(description: str, today: date, days: int = 14) -> list[str]:
    """Lines of the "Upcoming Holidays and Leaves" list that overlap [today, today + days).

    The list is free text, one entry per line. Lines under the heading are used (the whole text
    when there is no heading); a line is kept as written when a month-and-day, or a range, in it
    overlaps the window. Lines without a readable date are skipped.
    """
    lines = [ln.strip() for ln in description.splitlines()]
    for i, ln in enumerate(lines):
        if HEADING in ln.lower():
            lines = lines[i + 1:]
            break
    end = today + timedelta(days=days)
    kept: list[str] = []
    for ln in lines:
        text = _BULLET.sub("", ln).strip()
        span = _span(text, today) if text else None
        if span and span[0] < end and span[1] >= today:
            kept.append(text)
    return kept


def _month(word: str | None) -> int | None:
    word = (word or "").lower()
    if len(word) < 3:
        return None
    for number, name in enumerate(_MONTHS, 1):
        if name.startswith(word):
            return number
    return None


def _span(text: str, today: date) -> tuple[date, date] | None:
    for m in _RANGE.finditer(text):
        m1 = _month(m.group("m1"))
        if not m1:
            continue
        d1 = int(m.group("d1"))
        m2 = _month(m.group("m2")) if m.group("m2") else m1
        d2 = int(m.group("d2")) if m.group("d2") else d1
        if not m2:
            m2, d2 = m1, d1
        start = _nearest(m1, d1, today)
        if start is None:
            continue
        end = _on_or_after(m2, d2, start)
        return (start, end or start)
    return None


def _nearest(month: int, day: int, today: date) -> date | None:
    best = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if best is None or abs(candidate - today) < abs(best - today):
            best = candidate
    return best


def _on_or_after(month: int, day: int, start: date) -> date | None:
    for year in (start.year, start.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= start:
            return candidate
    return None
