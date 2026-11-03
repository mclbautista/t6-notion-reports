"""Parse PM Notes into an optional preamble plus dated "As of" blocks (spec section 5)."""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .snapshots import squash

_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_HEADER = re.compile(r"^\s*as\s*of\b[\s.,:;-]*(?P<rest>.*)$", re.IGNORECASE)
_BULLET = re.compile(r"^\s*[•\-*·–]+\s*")
_DATE = re.compile(r"^(?P<month>[A-Za-z]{3,})\.?\s*(?P<day>\d{1,2})\b[\s.,:;-]*(?P<tail>.*)$")


@dataclass
class Block:
    header: str
    header_date: date | None
    bullets: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, ...]:
        """Compare form: the text, not the header date; bullet markers and whitespace ignored."""
        bullets = tuple(squash(b) for b in self.bullets if squash(b))
        # Without a parseable date the header text is all that tells blocks apart.
        return bullets if self.header_date and bullets else (squash(self.header), *bullets)

    @property
    def text(self) -> str:
        return "\n".join([self.header, *("  • " + b for b in self.bullets)])


@dataclass
class ParsedNotes:
    preamble: str
    blocks: list[Block]


def parse_pm_notes(text: str | None, snapshot_date: date) -> ParsedNotes:
    preamble: list[str] = []
    blocks: list[Block] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        header = _HEADER.match(line)
        if header:
            blocks.append(_header_block(line, header.group("rest"), snapshot_date))
        elif not line:
            continue
        elif blocks:
            blocks[-1].bullets.append(_BULLET.sub("", line).strip())
        else:
            preamble.append(line)
    return ParsedNotes("\n".join(preamble), blocks)


def last_updated(text: str | None, snapshot_date: date) -> date | None:
    """Latest block header date, or None ("no notes yet")."""
    dates = [b.header_date for b in parse_pm_notes(text, snapshot_date).blocks if b.header_date]
    return max(dates) if dates else None


def _header_block(line: str, rest: str, snapshot_date: date) -> Block:
    words = rest.split()
    if words and words[0].rstrip(".,:;")[:3].lower() in _WEEKDAYS and not _month(words[0]):
        rest = " ".join(words[1:])
    m = _DATE.match(rest)
    month = _month(m.group("month")) if m else None
    if not (m and month):
        return Block(line, None)
    when = _infer_year(month, int(m.group("day")), snapshot_date)
    tail = m.group("tail").strip()
    block = Block(line[: len(line) - len(tail)].rstrip() if tail else line, when)
    if tail:
        block.bullets.append(_BULLET.sub("", tail).strip())
    return block


def _month(word: str) -> int | None:
    word = word.lower().rstrip(".,:;")
    hit = difflib.get_close_matches(word, [m[:3] for m in _MONTHS] + list(_MONTHS), n=1, cutoff=0.75)
    if not hit or len(word) < 3:
        return None
    return [m[:3] for m in _MONTHS].index(hit[0]) + 1 if len(hit[0]) == 3 else _MONTHS.index(hit[0]) + 1


def _infer_year(month: int, day: int, snapshot_date: date) -> date | None:
    """Closest date to, but not after, snapshot date + 7 days."""
    limit = snapshot_date + timedelta(days=7)
    best = None
    for year in (snapshot_date.year - 1, snapshot_date.year, snapshot_date.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate <= limit and (best is None or candidate > best):
            best = candidate
    return best
