"""Dates written in free text (PM Notes lines), with or without a year.

``mentions`` finds every date a line writes: ISO (2026-12-01), numeric (12/1/2026, 12/1), month and
day ("Dec 1", "1 Dec", "Dec 1, 2028"), month and year ("July 2027") and quarters ("Q2 2027"), halves ("H1 2027"), seasons ("summer 2027") and year parts ("early 2027"). A
written year is kept as written. A date without one takes the year that puts it nearest ``anchor``:
the date of the "As of" block it sits in, so "Dec 2" in a block from August 2024 is Dec 2, 2024.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MON = r"(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_YEAR = r"(?P<year>(?:19|20)\d\d)"
_PATTERNS = (
    ("iso", re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})\b")),
    ("num", re.compile(r"\b(?P<a>\d{1,2})/(?P<b>\d{1,2})(?:/(?P<year>\d{4}|\d{2}))?\b(?!/)")),
    ("md", re.compile(rf"\b{_MON}\s*(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s*{_YEAR}\b)?", re.I)),
    ("dm", re.compile(rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MON}(?![a-z])(?:,?\s*{_YEAR}\b)?", re.I)),
    ("my", re.compile(rf"\b{_MON}\s*,?\s*{_YEAR}\b", re.I)),
    ("h", re.compile(
        r"\bH(?P<h>[12])\s*,?\s*(?P<year>(?:19|20)\d\d)\b|\b(?P<year2>(?:19|20)\d\d)\s*H(?P<h2>[12])\b"
        r"|\b(?P<h3>[12])H\s*(?P<year3>(?:19|20)\d\d)\b", re.I)),
    ("season", re.compile(
        r"\b(?P<season>spring|summer|fall|autumn|winter)\s*(?:of\s+)?(?P<year>(?:19|20)\d\d)\b", re.I)),
    ("rel", re.compile(
        r"\b(?P<rel>early|mid|late|end\s+of|start\s+of|beginning\s+of|middle\s+of)[\s-]*(?:the\s+)?"
        r"(?:year\s+)?(?P<year>(?:19|20)\d\d)\b", re.I)),
    ("q", re.compile(r"\bQ(?P<q>[1-4])\s*,?\s*(?P<year>(?:19|20)\d\d)\b|\b(?P<year2>(?:19|20)\d\d)\s*Q(?P<q2>[1-4])\b", re.I)),
)


@dataclass(frozen=True)
class Mention:
    """One date a line writes, as the span of days it covers."""

    first: date
    last: date
    precise: bool  # a single day
    year_written: bool


def mentions(text: str, anchor: date) -> list[Mention]:
    """Every date in ``text``, in order; a stretch of text is read as one date at most."""
    taken: list[tuple[int, int]] = []
    found: list[tuple[int, Mention]] = []
    for kind, rx in _PATTERNS:
        for m in rx.finditer(text):
            if any(m.start() < end and start < m.end() for start, end in taken):
                continue
            hit = _read(kind, m, anchor)
            if hit is not None:
                taken.append((m.start(), m.end()))
                found.append((m.start(), hit))
    return [hit for _, hit in sorted(found, key=lambda x: x[0])]


def days(text: str, anchor: date) -> set[date]:
    """The single days ``text`` writes."""
    return {m.first for m in mentions(text, anchor) if m.precise}


def has_future_date(text: str, anchor: date, today: date) -> bool:
    """True when a date in ``text`` ends today or later. No horizon: a far date counts."""
    return any(m.last >= today for m in mentions(text, anchor))


# Month names written out or abbreviated, but not "may" (a verb as often as a month) unless capitalised.
_PERIOD = re.compile(
    r"(?i:\b(?:january|february|march|april|june|july|august|september|october|november|december"
    r"|jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec|[qh][1-4]|(?:19|20)\d\d"
    r"|spring|summer|fall|autumn|winter|weeks?|months?|quarters?|years?)\b\.?)|\bMay\b"
)


# Words that say there is no date (yet): a period after one of them is not a stated date.
_NEGATING = re.compile(
    r"(?i)\b(?:no|not|none|never|without|tbc|tbd|tba|pending|unknown|undecided|unconfirmed|undetermined|unsure|maybe|perhaps|awaiting|waiting|n/a|to\s+be\s+(?:confirmed|determined|decided))\b"
)


def is_period(text: str) -> bool:
    """True when ``text`` states a time: a month, a quarter or half (Q2, H1), a year, a season, or a
    week, month, quarter or year. "TBC", "pending" or a bare property name is not one, and neither
    is a period that a negation or placeholder introduces ("no date this year", "TBC next month");
    one that a placeholder follows ("Q2 2027 TBC") is still stated."""
    found = _PERIOD.search(text)
    return bool(found) and not _NEGATING.search(text[: found.start()])


def _read(kind: str, m: re.Match, anchor: date) -> Mention | None:
    try:
        if kind == "iso":
            return _day(date(int(m["year"]), int(m["month"]), int(m["day"])))
        if kind == "num":
            a, b = int(m["a"]), int(m["b"])
            month, day = (a, b) if a <= 12 else (b, a)
            return _yearly(month, day, m["year"], anchor)
        if kind in ("md", "dm"):
            return _yearly(_MONTHS.index(m["mon"].lower()[:3]) + 1, int(m["day"]), m["year"], anchor)
        if kind == "my":
            month, year = _MONTHS.index(m["mon"].lower()[:3]) + 1, int(m["year"])
            first = date(year, month, 1)
            return Mention(first, _next_month(first) - timedelta(days=1), False, True)
        if kind == "h":
            half = int(m["h"] or m["h2"] or m["h3"])
            year = int(m["year"] or m["year2"] or m["year3"])
            return _span(year, 1 + 6 * (half - 1), 6 + 6 * (half - 1))
        if kind == "season":
            first, last, shift = _SEASONS[m["season"].lower()]
            return _span(int(m["year"]), first, last, shift)
        if kind == "rel":
            first, last = _PARTS[re.sub(r"\s+", " ", m["rel"].lower())]
            return _span(int(m["year"]), first, last)
        if kind == "q":
            q, year = int(m["q"] or m["q2"]), int(m["year"] or m["year2"])
            first = date(year, 3 * q - 2, 1)
            return Mention(first, _next_month(date(year, 3 * q, 1)) - timedelta(days=1), False, True)
    except ValueError:
        return None
    return None


_SEASONS = {"spring": (3, 5, 0), "summer": (6, 8, 0), "fall": (9, 11, 0), "autumn": (9, 11, 0), "winter": (12, 2, 1)}
_PARTS = {"early": (1, 4), "start of": (1, 4), "beginning of": (1, 4), "mid": (5, 8), "middle of": (5, 8),
          "late": (9, 12), "end of": (9, 12)}


def _span(year: int, first_month: int, last_month: int, end_shift: int = 0) -> Mention:
    """A stretch of whole months of ``year``; ``end_shift`` years later for one that wraps (winter)."""
    end = _next_month(date(year + end_shift, last_month, 1)) - timedelta(days=1)
    return Mention(date(year, first_month, 1), end, False, True)


def _yearly(month: int, day: int, year: str | None, anchor: date) -> Mention | None:
    if year:
        full = int(year) + (2000 if len(year) == 2 else 0)
        return _day(date(full, month, day), True)
    best = None
    for y in (anchor.year - 1, anchor.year, anchor.year + 1):
        try:
            candidate = date(y, month, day)
        except ValueError:
            continue
        if best is None or abs(candidate - anchor) < abs(best - anchor):
            best = candidate
    return None if best is None else _day(best, False)


def _day(when: date, year_written: bool = True) -> Mention:
    return Mention(when, when, True, year_written)


def _next_month(first: date) -> date:
    return date(first.year + first.month // 12, first.month % 12 + 1, 1)
