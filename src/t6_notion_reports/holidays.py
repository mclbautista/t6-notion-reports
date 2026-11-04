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


def leave_lines(description: str | list[dict]) -> list[str] | None:
    """The lines of the "Upcoming Holidays and Leaves" section, bullet marks dropped; None without that heading.

    ``description`` is the Projects database description as text, or as its rich text items, which
    also tell bold lines apart. Unlike ``upcoming_entries`` this keeps lines without a readable date:
    the leave parser reads them. The section ends at the next heading, so text under a later heading
    never reaches the leave call. A heading is a bold line, a line starting with "#", a line ending
    with ":" that holds no digit, or a dateless line after a blank line. Bold lines straight under the
    heading (a note to the team) are skipped rather than ending the section.
    """
    lines = _rich_lines(description)
    start = next((i for i, (text, _) in enumerate(lines) if HEADING in text.lower()), None)
    if start is None:
        return None
    out: list[str] = []
    blank = False
    for text, bold in lines[start + 1:]:
        text = text.strip()
        if not text:
            blank = True
            continue
        heading = bold or text.startswith("#") or (text.endswith(":") and not re.search(r"\d", text))
        if out and (heading or (blank and not re.search(r"\d", text))):
            break
        blank = False
        if heading:
            continue
        if (clean := _BULLET.sub("", text).strip()):
            out.append(clean)
    return out


def _rich_lines(description: str | list[dict]) -> list[tuple[str, bool]]:
    """(text, every visible character is bold) for each line of the description."""
    if isinstance(description, str):
        return [(ln, False) for ln in description.splitlines()]
    lines: list[tuple[str, bool]] = [("", True)]
    for item in description:
        bold = bool((item.get("annotations") or {}).get("bold"))
        for n, piece in enumerate(str(item.get("plain_text") or "").split("\n")):
            if n:
                lines.append(("", True))
            text, all_bold = lines[-1]
            lines[-1] = (text + piece, all_bold and (bold or not piece.strip()))
    return [(text, all_bold and bool(text.strip())) for text, all_bold in lines]


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
