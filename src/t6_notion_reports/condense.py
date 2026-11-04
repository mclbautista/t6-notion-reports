"""Condense a project's raw PM Notes blocks into MOM-style bullets (spec section 8.3).

The LLM proposes; code disposes. Every date, number, episode reference and capitalised word (person
names, titles, codes) in the output must appear in that project's PM Notes, and at most one still-
upcoming date may come from an older block. Anything else, or a failed call, gives back the raw bullets.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel

from .llm import LlmClient, LlmError

SYSTEM = """You turn one project's raw PM Notes into short bullets for the weekly PSR minutes (MOM style).
You get "new_blocks" (what was added this week: the bullets to condense) and "pm_notes" (the full log, older blocks first).
You may: drop asides, merge and reorder bullets, keep the Taglish as written, and restructure
(for example "Episodes status: Ep 3 (...), Ep 4 (...)").
You may pull at most ONE still-upcoming key date from an older block of pm_notes (for example "QCinema submission on Nov 6").
You may NOT add facts: every date, number, episode, and person or company name in your bullets must already appear in pm_notes.
Do not guess, infer, or explain. Return {"bullets": [...]}."""

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MON = r"(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
_DATE_MD = re.compile(rf"\b{_MON}\s*{_DAY}\b", re.I)
_DATE_DM = re.compile(rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MON}(?![a-z])", re.I)
_DATE_NUM = re.compile(r"\b(?P<a>\d{1,2})/(?P<b>\d{1,2})(?:/\d{2,4})?\b")
_EPISODE = re.compile(r"\bep(?:isode)?s?\.?\s*#?\s*(\d+(?:\s*(?:-|–|to|&|and|,|/)\s*#?\d+)*)", re.I)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_CAPITAL = re.compile(r"\b[A-ZÀ-Ý][\w'’-]*")
_WORD = re.compile(r"[\w'’-]+")
# Words the model may use to structure a bullet without adding a fact.
STRUCTURE_WORDS = {"ep", "eps", "episode", "episodes", "status"}


class CondensedBullets(BaseModel):
    bullets: list[str]


@dataclass(frozen=True)
class Condensed:
    bullets: list[str]
    raw: bool  # True: the LLM output was rejected (or the call failed) and the raw bullets are used


@dataclass
class Facts:
    dates: set[tuple[int, int]]
    numbers: set[str]
    episodes: set[int]
    names: set[str]  # lower case capitalised words


def extract_facts(text: str) -> Facts:
    dates: set[tuple[int, int]] = set()
    for rx in (_DATE_MD, _DATE_DM):
        for m in rx.finditer(text):
            dates.add((_MONTHS.index(m.group("mon").lower()[:3]) + 1, int(m.group("day"))))
    for m in _DATE_NUM.finditer(text):
        a, b = int(m.group("a")), int(m.group("b"))
        dates.add((a, b) if a <= 12 else (b, a))  # month first unless that cannot be a month
        if b <= 12:
            dates.add((b, a))
    episodes = {int(n) for m in _EPISODE.finditer(text) for n in re.findall(r"\d+", m.group(1))}
    numbers = {n.replace(",", "") for n in _NUMBER.findall(text)}
    names = {w.lower() for w in _CAPITAL.findall(text)}
    return Facts(dates, numbers, episodes, names)


def _word_set(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text)}


def check_grounding(bullets: list[str], notes: str, source: list[str], meeting: date) -> str | None:
    """None when every fact in ``bullets`` is in ``notes``, else the first reason it is not.

    Reasons never quote more than the offending token. ``source`` is the raw new bullets; a date found
    only outside it comes from an older block.
    """
    if not any(b.strip() for b in bullets):
        return "no bullets"
    out = extract_facts("\n".join(bullets))
    have = extract_facts(notes)
    words = _word_set(notes)
    fresh = extract_facts("\n".join(source))
    for d in sorted(out.dates - have.dates):
        return f"date {d[0]}/{d[1]} is not in the notes"
    for n in sorted(out.numbers - have.numbers):
        return f"number {n} is not in the notes"
    for e in sorted(out.episodes - have.episodes):
        return f"episode {e} is not in the notes"
    for name in sorted(out.names - STRUCTURE_WORDS):
        if name not in words:
            return f"{name.title()} is not in the notes"
    older = out.dates - fresh.dates - {(meeting.month, meeting.day)}  # the meeting's own date is not an older one
    if len(older) > 1:
        return "more than one date from an older block"
    for d in older:
        if not upcoming(d, meeting):
            return f"older date {d[0]}/{d[1]} is not upcoming"
    return None


def upcoming(md: tuple[int, int], meeting: date, horizon_days: int = 183) -> bool:
    """True when the month/day falls on or after ``meeting``, within ``horizon_days`` (about six months)."""
    for year in (meeting.year, meeting.year + 1):
        try:
            when = date(year, md[0], md[1])
        except ValueError:
            return False
        if when >= meeting:
            return (when - meeting).days <= horizon_days
    return False


def condense_bullets(llm: LlmClient, label: str, notes: str, source: list[str], meeting: date) -> Condensed:
    """Never raises: a failed call or a rejected answer is the raw bullets with ``raw=True``."""
    prompt = json.dumps({"project": label, "new_blocks": source, "pm_notes": notes}, ensure_ascii=False)
    try:
        reply = llm.complete(SYSTEM, prompt, CondensedBullets)
    except LlmError:
        return Condensed(list(source), True)
    bullets = [b.strip() for b in reply.bullets if b.strip()]
    if check_grounding(bullets, notes, source, meeting) is not None:
        return Condensed(list(source), True)
    return Condensed(bullets, False)


def ungrounded(text: str, sources: str) -> str | None:
    """None when every date, number, episode and capitalised word in ``text`` is in ``sources``, else the first reason.

    The same facts ``check_grounding`` looks for, without its rules about older blocks.
    """
    out, have, words = extract_facts(text), extract_facts(sources), _word_set(sources)
    for d in sorted(out.dates - have.dates):
        return f"date {d[0]}/{d[1]} is not in the sources"
    for n in sorted(out.numbers - have.numbers):
        return f"number {n} is not in the sources"
    for e in sorted(out.episodes - have.episodes):
        return f"episode {e} is not in the sources"
    for name in sorted(out.names - STRUCTURE_WORDS):
        if name not in words:
            return f"{name.title()} is not in the sources"
    return None
