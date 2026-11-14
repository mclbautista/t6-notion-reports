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
from t6.privacy import Masked

from .dates import mentions
from .llm import LlmClient, LlmError
from .masking import Masker, mask_and_read_back

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
# Plain connecting words that carry no fact, so a sentence may use them without the sources having them.
PLAIN_WORDS = frozenset("""
a an the and or but nor so yet of on in at by for to from with without into onto over under after before
during until till since while about around within up down out off as than then that this these those there
here is are was were be been being am do does did has have had having will would shall should can could may
might must not no nor now still also already just only still again yet soon later next last first new old
it its it's they them their he she his her we our you your i my me us who whom whose which what when where
why how if else both each either every any all some more most less least many much few other another same
such very too per via vs per own again further once back away ahead among between against along across
""".split())
# Everyday status vocabulary a fluent sentence may use without the sources having it. It holds no names,
# titles or codes: a word outside it, the sources and PLAIN_WORDS is treated as an invented entity.
COMMON_WORDS = frozenset("""
add added approve approved ask asked awaiting back blocked book booked bring call cancel change check
checked clear client clients close closed colour color come comment complete completed confirm confirmed
continue cut date dates day days deadline deadlines deliver delivered delivery delay delayed done draft
due early edit edits editor end ended episode episodes expect expected extend extended feedback fine
finish finished fix fixed follow get give go going good grade grading hand handed happen happening
health hold held issue issues item items keep late launch lead leads lock locked look make mark
meeting meetings miss missed missing move moved moves need needed needs note notes ok okay open order
pass pending plan planned plans post pass problem problems proceed progress project projects push
pushed put question questions ready receive received release released remain remains remaining
request requested resolve resolved review reviewed risk risks run running schedule scheduled send
sent set share shared shift shifted show shown slip slipped stage start started status step steps
stop stopped submit submitted submission team tell testing time timeline today tomorrow track
tracking turn update updated upcoming wait waiting want week weeks work working yesterday
""".split())
_MONTH_WORDS = frozenset(
    "january february march april may june july august september october november december "
    "jan feb mar apr jun jul aug sep sept oct nov dec".split()
)
_WORD_TOKEN = re.compile(r"(?<!\w)[^\W\d_]+")


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


def _stem(word: str) -> str:
    """A crude stem so "waits", "waiting" and "waited" are one word; both sides are stemmed the same way."""
    word = word.lower()
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    for suffix in ("ing", "ed", "es", "ly", "s", "d"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    return word[:-1] if word.endswith("e") and len(word) > 3 else word


def _stems(text: str) -> set[str]:
    return {_stem(w) for w in _WORD_TOKEN.findall(text) if len(w) > 1}


def _unknown_word(text: str, sources: str) -> str | None:
    """The first word of ``text`` that is not in ``sources`` (any case or inflection), a month, a plain
    connecting word or a structure word. Catches an invented name however it is capitalised."""
    have = _stems(sources)
    for word in _WORD_TOKEN.findall(text):
        low = word.lower()
        if len(low) <= 1 or low in PLAIN_WORDS or low in COMMON_WORDS or low in STRUCTURE_WORDS or _is_month(low):
            continue
        if _stem(low) not in have:
            return word
    return None


def _is_month(word: str) -> bool:
    return word in _MONTH_WORDS


def _ungrounded_date(text: str, sources: str, anchor: date) -> str | None:
    """The first date ``text`` writes with a year that no source line writes. A source date without a
    year may be written with a year within a year of ``anchor``; any other year must be written as is."""
    have = mentions(sources, anchor)
    for m in mentions(text, anchor):
        if not m.year_written:
            continue
        exact = any(h.year_written and (h.first, h.last, h.precise) == (m.first, m.last, m.precise) for h in have)
        yearless = m.precise and abs(m.first.year - anchor.year) <= 1 and any(
            not h.year_written and h.precise and (h.first.month, h.first.day) == (m.first.month, m.first.day)
            for h in have
        )
        if not (exact or yearless):
            return m.first.isoformat() if m.precise else f"{m.first:%b %Y}"
    return None


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
    if (when := _ungrounded_date("\n".join(bullets), notes, meeting)) is not None:
        return f"date {when} is not in the notes"
    if (word := _unknown_word("\n".join(bullets), notes)) is not None:
        return f"{word} is not in the notes"
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


def condense_bullets(
    llm: LlmClient, label: str, notes: str, source: list[str], meeting: date,
    *, masker: Masker | None = None, page_id: str = "",
) -> Condensed:
    """Never raises: a failed call or a rejected answer is the raw bullets with ``raw=True``."""
    session = (masker or Masker()).session(page_id)

    def hide(text: str) -> str:
        return session.mask(text).text

    prompt = json.dumps({"project": hide(label), "new_blocks": [hide(text) for text in source],
                         "pm_notes": hide(notes)}, ensure_ascii=False)
    try:
        reply = llm.complete("condense-v1", SYSTEM, Masked(prompt), CondensedBullets, session)
    except LlmError:
        return Condensed(list(source), True)
    bullets = [b.strip() for b in reply.bullets if b.strip()]
    # The reply comes back with the names restored (an id reads "[id]"), so it is checked against the
    # notes and blocks as they read back.
    if check_grounding(bullets, mask_and_read_back(session, notes)[1],
                       [mask_and_read_back(session, text)[1] for text in source], meeting) is not None:
        return Condensed(list(source), True)
    return Condensed(bullets, False)


def ungrounded(text: str, sources: str, today: date | None = None) -> str | None:
    """None when every date, number, episode and word in ``text`` is in ``sources``, else the first reason.

    The same facts ``check_grounding`` looks for, without its rules about older blocks. A date written
    with a year must be written that way in ``sources`` (see ``_ungrounded_date``); a word must be a
    word of ``sources`` in any case or inflection, a month, or a plain connecting word.
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
    if (when := _ungrounded_date(text, sources, today or date.today())) is not None:
        return f"date {when} is not in the sources"
    if (word := _unknown_word(text, sources)) is not None:
        return f"{word} is not in the sources"
    return None
