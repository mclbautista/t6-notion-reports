"""Project consolidation (spec #40, ticket #47): one LLM call per gated project.

The call reads a small payload about that project alone and returns the week's changes, the Concerns
and the real last update. The LLM proposes; code disposes. Every item must quote an exact source line
(ignoring bullet marks and whitespace) and pass the grounding check, and an item that fails is
dropped on its own, so the rest of the same reply still renders.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from typing import Any

from pydantic import BaseModel, ValidationError, field_validator
from t6.privacy import Masked, Session

from .condense import ungrounded
from .dates import has_future_date, is_period, mentions
from .diff import FieldChange
from .flags import ARCHIVAL, PLAYDATE, SUBMISSION, relevant_dates, stored_day
from .llm import LlmClient, LlmError
from .pm_notes import parse_pm_notes
from .masking import Masker, mask_and_read_back
from .schedule import MANILA
from .snapshots import squash

DEFAULT_PM_NOTES_WEEKS = 52
CONCERN_KINDS = ("open_item", "health_vs_evidence", "stale_milestone", "source_conflict")
SOURCES = {"pm_notes", "minutes", "snapshot"}
DATE_KEYS = {PLAYDATE: "playdate", SUBMISSION: "submission_deadline", ARCHIVAL: "archival_deadline"}
_PROPERTY_BY_KEY = {key: name for name, key in DATE_KEYS.items()}

SYSTEM = """You help a post-production coordinator prepare for the weekly PSR meeting on one project. You are given the project, its dated PM Notes lines (each with an id), what changed since the last brief and since the last PSR, and the project's lines from the latest PSR Minutes. Today's date is given.
Use only what is in the payload. Never invent a fact, a date or a name. Every date, number, episode and name you write in a "text" must be supported by its cited line. Do not borrow facts from other lines or metadata; start a sentence with a word from the cited line.

1. last_update: pick the PM Notes "As of" header that marks the project's real last update. Headers have typos ("Seep", "Oct 26.,"); read them sensibly. Cite the header's line_id and give its date as as_of_date. If no header makes sense, return null.
2. changes: short phrases (a few words, Taglish is fine) for what changed. Merge the snapshot changes, the PM Notes and the Minutes into one list. Each change needs a source. Set after_psr true only for changes newer than the Monday snapshot (window since_psr).
3. concerns: things the coordinator should act on or double-check. Write each as one fluent sentence in the language of the notes. Kinds:
   - open_item: a line such as "Pending", "TBC", "Waiting", "Confirm" or "Follow up" that is still open.
   - health_vs_evidence: project_health contradicts the evidence (ON TRACK or INCOMING but the notes show a slipped milestone or a blocker; CRITICAL or AT RISK with nothing new for two weeks).
   - stale_milestone: a dated milestone in the notes is already past and no later line mentions it.
   - source_conflict: two sources disagree and you cannot tell which is newer.
   Every concern needs a source (pm_notes or minutes), a line_id and an exact quote.
   For source_conflict or health_vs_evidence comparisons that use other lines, include supporting_citations:
   [{"source": "pm_notes", "line_id": 1, "quote": "exact line"}]. Supporting sources may also be minutes or
   snapshot. Every fact must come from the primary line or these explicitly cited lines; health comparisons
   may also use project_health. Do not borrow facts from uncited lines.
4. dates_from_notes: for each property in blank_date_properties, look for its date in the PM Notes. Give iso_date (YYYY-MM-DD) when the line states a precise day, otherwise fuzzy_text copied exactly from the line (for example "Q2 2027"). Cite the line_id and an exact quote. Return nothing for a property the notes do not state, and never for a property that is not in blank_date_properties.

When sources disagree, prefer the newest by its timestamp (minutes.last_edited, the end of a snapshot window, the PM Notes "As of" date). Raise source_conflict only when you cannot tell which source is newer.
For a change, source is pm_notes, minutes or snapshot. Every change gives the line_id and an exact quote: for snapshot, the line_id of the snapshot change and its "text" as the quote.
Every quote must be copied exactly from one line, character for character, and cited by line_id.
If nothing applies, return empty lists."""


class LastUpdate(BaseModel):
    line_id: int | None = None
    as_of_date: str | None = None


class Change(BaseModel):
    text: str = ""
    source: str = ""
    line_id: int | None = None
    quote: str | None = None
    after_psr: bool = False


class Citation(BaseModel):
    source: str
    line_id: int
    quote: str


class Concern(BaseModel):
    kind: str = ""
    text: str = ""
    source: str = ""
    line_id: int | None = None
    quote: str | None = None
    supporting_citations: list[Citation] = []


class DateFromNotes(BaseModel):
    property: str = ""
    iso_date: str | None = None
    fuzzy_text: str | None = None
    line_id: int | None = None
    quote: str | None = None


class Consolidation(BaseModel):
    """The reply. Kinds and sources are plain text here, and each item is parsed on its own, so one
    malformed item (a null text, a list for a quote) is dropped without voiding the rest of the reply."""

    last_update: LastUpdate | None = None
    changes: list[Change] = []
    concerns: list[Concern] = []
    dates_from_notes: list[DateFromNotes] = []

    @field_validator("changes", "concerns", "dates_from_notes", mode="wrap")
    @classmethod
    def _each_item(cls, value: Any, handler, info):
        if not isinstance(value, list):
            return handler(value)  # the envelope itself is wrong: the reply fails
        item = _ITEM_TYPES[info.field_name]
        kept = []
        for raw in value:
            try:
                kept.append(item.model_validate(raw))
            except ValidationError:
                continue
        return kept

    @field_validator("last_update", mode="wrap")
    @classmethod
    def _lenient_last_update(cls, value: Any, handler):
        try:
            return handler(value)
        except ValidationError:
            return None  # an unreadable last update falls back to last-edited time


_ITEM_TYPES = {"changes": Change, "concerns": Concern, "dates_from_notes": DateFromNotes}


@dataclass(frozen=True)
class SourceLine:
    origin: str  # pm_notes | minutes | snapshot
    text: str
    header: bool = False  # an "As of" header line
    header_date: date | None = None  # what the header says, when code can read it


@dataclass(frozen=True)
class Payload:
    """What leaves the machine for one project, and the lines the reply may cite.

    ``data`` is what is sent, with identities masked by ``session``. The reply comes back with the names
    restored (``llm.py``), so ``lines`` and ``health`` hold the text as written: quotes and facts are
    checked against that.
    """

    data: dict
    lines: dict[int, SourceLine]
    facts: str  # every value in the payload as text, for the grounding check
    session: Session
    page_id: str = ""
    health: str | None = None  # the project's Health as written

    def json(self, indent: int | None = None) -> str:
        return json.dumps(self.data, ensure_ascii=False, indent=indent)


@dataclass(frozen=True)
class Window:
    """One diff window: the property changes of a project between two snapshots."""

    name: str  # since_last_brief | since_psr
    start: datetime
    end: datetime
    changes: tuple[FieldChange, ...]


@dataclass(frozen=True)
class ChangeLine:
    text: str
    after_psr: bool = False


@dataclass(frozen=True)
class NoteDate:
    """A blank date property whose value the PM Notes state: a precise ``day`` or a ``fuzzy`` wording."""

    property: str  # the Notion property name, e.g. "Submission Deadline"
    day: date | None = None
    fuzzy: str | None = None


@dataclass(frozen=True)
class Outcome:
    """What one project shows from the LLM. ``failed``: the call or its JSON failed, so code-only output."""

    last_update: date | None = None
    changes: tuple[ChangeLine, ...] = ()
    concerns: tuple[str, ...] = ()
    dates: tuple[NoteDate, ...] = ()
    failed: bool = False


@dataclass
class _Build:
    lines: dict[int, SourceLine] = field(default_factory=dict)
    next_id: int = 1

    def add(self, line: SourceLine) -> int:
        self.lines[self.next_id] = line
        self.next_id += 1
        return self.next_id - 1


def build_payload(
    state: dict[str, object],
    today: date,
    snapshot_day: date,
    windows: list[Window],
    minutes_lines: tuple[str, ...] = (),
    minutes_meta: dict[str, str | None] | None = None,
    notes_weeks: int = DEFAULT_PM_NOTES_WEEKS,
    masker: Masker = Masker(),
    page_id: str = "",
) -> Payload:
    """The payload for one project. Known identities and identifiers in free text are protected:
    the PM Notes and Minutes lines pass through the ``strict`` pseudonymizer first (``masking.py``).
    The target project identity remains available; unknown names may remain in source text.

    ``minutes_meta`` holds the minutes page's date, last-edited time and Sent at; it is sent only with
    lines about this project.
    """
    build = _Build()
    session = masker.session(page_id)

    def hide(text: str) -> str:
        return session.mask(text).text

    def both(text: str) -> tuple[str, str]:
        return mask_and_read_back(session, text)

    notes = _notes_lines(state.get("PM Notes"), today, snapshot_day, notes_weeks, build, both)
    dates = {name: stored_day(state.get(name)) for name in DATE_KEYS}
    relevant = relevant_dates(state.get("Status"))
    data: dict = {
        "today": today.isoformat(),
        "project": {
            "name": squash(str(state.get("Name") or "")),
            "stage": hide(squash(str(state.get("Stage") or ""))) or None,
            "status": hide(squash(str(state.get("Status") or ""))) or None,
            "project_health": hide(squash(str(state.get("Project Health") or ""))) or None,
        },
        "dates": {key: (dates[name].isoformat() if dates[name] else None) for name, key in DATE_KEYS.items()},
        "blank_date_properties": [DATE_KEYS[n] for n in relevant if dates[n] is None],
        "pm_notes": notes,
        "snapshot_changes": [],
        "minutes": None,
    }
    if minutes_lines and minutes_meta:
        data["minutes"] = {
            "page_date": minutes_meta.get("page_date"),
            "last_edited": minutes_meta.get("last_edited"),
            "sent_at": minutes_meta.get("sent_at"),
            "lines": [{"line_id": build.add(SourceLine("minutes", seen)), "text": masked}
                      for masked, seen in map(both, minutes_lines)],
        }
    for w in windows:  # numbered like the other lines, so a snapshot change cites one exactly
        changes = []
        for c in w.changes:
            sent = [both(v) if isinstance(v, str) else (v, v) for v in (_plain(c.old), _plain(c.new))]
            (old, shown_old), (new, shown_new) = sent
            line_id = build.add(SourceLine("snapshot", f"{c.field}: {_show(shown_old)} → {_show(shown_new)}"))
            changes.append({"line_id": line_id, "field": c.field, "old": old, "new": new,
                            "text": f"{c.field}: {_show(old)} → {_show(new)}"})
        data["snapshot_changes"].append({
            "window": w.name,
            "from": w.start.astimezone(MANILA).isoformat(timespec="seconds"),
            "to": w.end.astimezone(MANILA).isoformat(timespec="seconds"),
            "changes": changes,
        })
    health = squash(str(state.get("Project Health") or "")) or None
    return Payload(data, build.lines, _facts(data), session, page_id, health)


UNDATED_BLOCKS_KEPT = 3  # PM Notes blocks whose header has no readable date count as old past the last three


def _notes_lines(raw: object, today: date, snapshot_day: date, weeks: int, build: _Build, both) -> list[dict]:
    """Numbered PM Notes lines from the last ``weeks`` weeks, plus older blocks that hold a future date.

    Which blocks go in is judged on the text as written. ``both(text)`` gives each line masked as it is
    sent and as the reply's quote of it reads once restored, which is what the line is kept as.
    """
    parsed = parse_pm_notes(str(raw) if raw else "", snapshot_day)
    out: list[dict] = []
    for text in parsed.preamble.splitlines():
        masked, seen = both(text)
        out.append({"line_id": build.add(SourceLine("pm_notes", seen)), "block": None, "text": masked})
    cutoff = today - timedelta(weeks=weeks)
    latest = parsed.blocks[-UNDATED_BLOCKS_KEPT:]
    for block in parsed.blocks:
        # A block with a readable header date is old when it is before the cutoff; one without is old
        # unless it is among the last UNDATED_BLOCKS_KEPT. Either way an old block stays for a future date.
        old = block.header_date < cutoff if block.header_date else not any(block is b for b in latest)
        if old and not has_future_date("\n".join(block.bullets), block.header_date or snapshot_day, today):
            continue
        header, header_seen = both(block.header)
        out.append({"line_id": build.add(SourceLine("pm_notes", header_seen, True, block.header_date)),
                    "block": header, "text": header})
        for bullet in block.bullets:
            masked, seen = both(f"• {bullet}")
            out.append({"line_id": build.add(SourceLine("pm_notes", seen)), "block": header, "text": masked})
    return out


def _show(value: object) -> str:
    """A snapshot value as a line shows it: "(none)" when blank, a list joined with commas."""
    if value in (None, "", []):
        return "(none)"
    return ", ".join(map(str, value)) if isinstance(value, list) else str(value)


def _plain(value: object) -> object:
    if isinstance(value, dict) and "start" in value:
        start, end = value["start"], value.get("end")
        return f"{start} – {end}" if end else start
    if isinstance(value, list):
        return [str(r.get("name") or r["id"]) if isinstance(r, dict) else str(r) for r in value]
    return value


def _facts(data: dict) -> str:
    """Every value in the payload (not its keys) as text, plus "Dec 1, 2026" for each ISO date in it."""
    values: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k != "line_id":  # ids are not facts
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif node is not None and not isinstance(node, bool):
            values.append(str(node))

    walk(data)
    text = "\n".join(values)
    for iso in set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)):
        try:
            when = date.fromisoformat(iso)
        except ValueError:
            continue
        values.append(f"{when:%b} {when.day}, {when.year}")
    return "\n".join(values)


def consolidate(llm: LlmClient, payload: Payload, today: date) -> Outcome:
    """Ask about one project. Never raises: a failed call or invalid JSON is ``Outcome(failed=True)``."""
    try:
        reply = llm.complete("consolidate-v1", SYSTEM, Masked(payload.json()), Consolidation, payload.session)
    except LlmError:
        return Outcome(failed=True)
    return validate(reply, payload, today)


def validate(reply: Consolidation, payload: Payload, today: date) -> Outcome:
    """Keep the items that check out; drop each failing item on its own."""
    changes: dict[str, ChangeLine] = {}
    for item in reply.changes:
        if not _item_ok(payload, item.text, item.source, item.line_id, item.quote, snapshot_ok=True):
            continue
        text = squash(item.text)
        key = text.casefold()
        kept = changes.get(key)
        changes[key] = ChangeLine(kept.text if kept else text, item.after_psr or bool(kept and kept.after_psr))
    concerns: list[str] = []
    for item in reply.concerns:
        if item.kind not in CONCERN_KINDS or (item.kind == "health_vs_evidence" and not payload.health):
            continue
        if _concern_ok(payload, item):
            concerns.append(squash(item.text))
    dates = _note_dates(reply.dates_from_notes, payload)
    return Outcome(_last_update(reply.last_update, payload, today), tuple(changes.values()), tuple(dict.fromkeys(concerns)), dates)


def _note_dates(items: list[DateFromNotes], payload: Payload) -> tuple[NoteDate, ...]:
    """The dates that check out: a blank, relevant property, an exact quote, and a value the quoted line holds."""
    blank = set(payload.data["blank_date_properties"])
    found: dict[str, NoteDate] = {}
    for item in items:
        name = _PROPERTY_BY_KEY.get(item.property)
        if name is None or item.property not in blank or name in found:
            continue
        line = payload.lines.get(item.line_id) if item.line_id is not None else None
        if line is None or line.origin != "pm_notes" or not item.quote or _bare(item.quote) != _bare(line.text):
            continue
        if item.iso_date:
            try:
                day = date.fromisoformat(item.iso_date)
            except ValueError:
                continue
            if _day_grounded(day, line.text, date.fromisoformat(payload.data["today"])):
                found[name] = NoteDate(name, day=day)
        elif (item.fuzzy_text and is_period(item.fuzzy_text)
              and squash(item.fuzzy_text).casefold() in squash(line.text).casefold()):
            found[name] = NoteDate(name, fuzzy=squash(item.fuzzy_text))
    return tuple(found.values())


def _item_ok(payload: Payload, text: str, source: str, line_id: int | None, quote: str | None, snapshot_ok: bool) -> bool:
    """Every item cites a real line of its source and quotes it exactly; a snapshot change cites a snapshot line."""
    if not squash(text) or not _citation_ok(payload, source, line_id, quote, snapshot_ok):
        return False
    line = payload.lines[line_id]
    return ungrounded(text, _facts({"line": line.text}), date.fromisoformat(payload.data["today"])) is None


def _concern_ok(payload: Payload, item: Concern) -> bool:
    """Comparisons may combine only explicitly cited evidence, never arbitrary payload facts."""
    comparison = item.kind in {"source_conflict", "health_vs_evidence"}
    if item.supporting_citations and not comparison:
        return False
    if not _citation_ok(payload, item.source, item.line_id, item.quote, snapshot_ok=False):
        return False
    evidence = [payload.lines[item.line_id].text]
    for citation in item.supporting_citations:
        if not _citation_ok(payload, citation.source, citation.line_id, citation.quote, snapshot_ok=True):
            return False
        evidence.append(payload.lines[citation.line_id].text)
    if item.kind == "health_vs_evidence":
        evidence.append(payload.health or "")
    return bool(squash(item.text)) and ungrounded(
        item.text, _facts({"evidence": evidence}), date.fromisoformat(payload.data["today"])
    ) is None


def _citation_ok(payload: Payload, source: str, line_id: int | None, quote: str | None,
                 snapshot_ok: bool) -> bool:
    line = payload.lines.get(line_id) if line_id is not None else None
    return (source in SOURCES and (source != "snapshot" or snapshot_ok)
            and line is not None and line.origin == source and bool(quote)
            and _bare(quote) == _bare(line.text))


def _day_grounded(day: date, line: str, today: date) -> bool:
    """The line writes this day. A written year must match exactly, however far off; a day written
    without a year must be within a year of today, so the model cannot pick an arbitrary year."""
    for m in mentions(line, today):
        if not m.precise or (m.first.month, m.first.day) != (day.month, day.day):
            continue
        if m.first == day if m.year_written else abs((day - today).days) <= 366:
            return True
    return False


def _last_update(cited: LastUpdate | None, payload: Payload, today: date) -> date | None:
    """The cited header's date when the line is a real "As of" header and the date is not in the future."""
    if cited is None or cited.line_id is None or not cited.as_of_date:
        return None
    line = payload.lines.get(cited.line_id)
    if line is None or not line.header:
        return None
    try:
        when = date.fromisoformat(cited.as_of_date)
    except ValueError:
        return None
    if when > today or (line.header_date and line.header_date != when):
        return None  # the LLM may fix a typo in a header, but not move a readable one
    return when


def _bare(text: str) -> str:
    return squash(text).lstrip("•-*·– ").strip()
