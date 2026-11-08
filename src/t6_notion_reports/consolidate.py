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

from .condense import ungrounded
from .dates import has_future_date, is_period, mentions
from .diff import FieldChange
from .flags import ARCHIVAL, PLAYDATE, SUBMISSION, relevant_dates, stored_day
from .llm import LlmClient, LlmError
from .pm_notes import parse_pm_notes
from .redact import Mapping, Redactor, unresolved
from .schedule import MANILA
from .snapshots import squash

DEFAULT_PM_NOTES_WEEKS = 52
CONCERN_KINDS = ("open_item", "health_vs_evidence", "stale_milestone", "source_conflict")
SOURCES = {"pm_notes", "minutes", "snapshot"}
DATE_KEYS = {PLAYDATE: "playdate", SUBMISSION: "submission_deadline", ARCHIVAL: "archival_deadline"}
_PROPERTY_BY_KEY = {key: name for name, key in DATE_KEYS.items()}

SYSTEM = """You help a post-production coordinator prepare for the weekly PSR meeting on one project. You are given the project, its dated PM Notes lines (each with an id), what changed since the last brief and since the last PSR, and the project's lines from the latest PSR Minutes. Today's date is given.
Use only what is in the payload. Never invent a fact, a date or a name. Every date, number, episode and capitalised word you write in a "text" must already appear in the payload, so start a sentence with a word from the payload.

1. last_update: pick the PM Notes "As of" header that marks the project's real last update. Headers have typos ("Seep", "Oct 26.,"); read them sensibly. Cite the header's line_id and give its date as as_of_date. If no header makes sense, return null.
2. changes: short phrases (a few words, Taglish is fine) for what changed. Merge the snapshot changes, the PM Notes and the Minutes into one list. Each change needs a source. Set after_psr true only for changes newer than the Monday snapshot (window since_psr).
3. concerns: things the coordinator should act on or double-check. Write each as one fluent sentence in the language of the notes. Kinds:
   - open_item: a line such as "Pending", "TBC", "Waiting", "Confirm" or "Follow up" that is still open.
   - health_vs_evidence: project_health contradicts the evidence (ON TRACK or INCOMING but the notes show a slipped milestone or a blocker; CRITICAL or AT RISK with nothing new for two weeks).
   - stale_milestone: a dated milestone in the notes is already past and no later line mentions it.
   - source_conflict: two sources disagree and you cannot tell which is newer.
   Every concern needs a source (pm_notes or minutes), a line_id and an exact quote.
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


class Concern(BaseModel):
    kind: str = ""
    text: str = ""
    source: str = ""
    line_id: int | None = None
    quote: str | None = None


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

    ``lines`` hold the text as sent (redacted), so quotes are checked against what the LLM saw;
    ``mapping`` puts the redacted values back into accepted items.
    """

    data: dict
    lines: dict[int, SourceLine]
    facts: str  # every value in the payload as text, for the grounding check
    mapping: Mapping = field(default_factory=Mapping)
    redactor: Redactor = field(default_factory=Redactor)
    page_id: str = ""

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
    redactor: Redactor = Redactor(),
    page_id: str = "",
) -> Payload:
    """The payload for one project. Nothing about other projects, people or Notion ids goes in:
    the PM Notes and Minutes lines pass through ``redactor`` first (see redact.py).

    ``minutes_meta`` holds the minutes page's date, last-edited time and Sent at; it is sent only with
    lines about this project.
    """
    build = _Build()
    mapping = Mapping()

    def hide(text: str) -> str:
        return redactor.redact(text, page_id, mapping)

    notes = _notes_lines(state.get("PM Notes"), today, snapshot_day, notes_weeks, build, hide)
    dates = {name: stored_day(state.get(name)) for name in DATE_KEYS}
    relevant = relevant_dates(state.get("Status"))
    data: dict = {
        "today": today.isoformat(),
        "project": {
            "name": squash(str(state.get("Name") or "")),
            "stage": squash(str(state.get("Stage") or "")) or None,
            "status": squash(str(state.get("Status") or "")) or None,
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
            "lines": [{"line_id": build.add(SourceLine("minutes", t)), "text": t} for t in map(hide, minutes_lines)],
        }
    for w in windows:  # numbered like the other lines, so a snapshot change cites one exactly
        changes = []
        for c in w.changes:
            old, new = (hide(v) if isinstance(v, str) else v for v in (_plain(c.old), _plain(c.new)))
            text = f"{c.field}: {_show(old)} → {_show(new)}"
            changes.append({"line_id": build.add(SourceLine("snapshot", text)), "field": c.field,
                            "old": old, "new": new, "text": text})
        data["snapshot_changes"].append({
            "window": w.name,
            "from": w.start.astimezone(MANILA).isoformat(timespec="seconds"),
            "to": w.end.astimezone(MANILA).isoformat(timespec="seconds"),
            "changes": changes,
        })
    return Payload(data, build.lines, _facts(data), mapping, redactor, page_id)


UNDATED_BLOCKS_KEPT = 3  # PM Notes blocks whose header has no readable date count as old past the last three


def _notes_lines(raw: object, today: date, snapshot_day: date, weeks: int, build: _Build, hide) -> list[dict]:
    """Numbered PM Notes lines from the last ``weeks`` weeks, plus older blocks that hold a future date.

    Which blocks go in is judged on the text as written; each line is redacted by ``hide`` as it is sent.
    """
    parsed = parse_pm_notes(str(raw) if raw else "", snapshot_day)
    out: list[dict] = []
    for text in map(hide, parsed.preamble.splitlines()):
        out.append({"line_id": build.add(SourceLine("pm_notes", text)), "block": None, "text": text})
    cutoff = today - timedelta(weeks=weeks)
    latest = parsed.blocks[-UNDATED_BLOCKS_KEPT:]
    for block in parsed.blocks:
        # A block with a readable header date is old when it is before the cutoff; one without is old
        # unless it is among the last UNDATED_BLOCKS_KEPT. Either way an old block stays for a future date.
        old = block.header_date < cutoff if block.header_date else not any(block is b for b in latest)
        if old and not has_future_date("\n".join(block.bullets), block.header_date or snapshot_day, today):
            continue
        header = hide(block.header)
        out.append({"line_id": build.add(SourceLine("pm_notes", header, True, block.header_date)),
                    "block": header, "text": header})
        for bullet in block.bullets:
            text = hide(f"• {bullet}")
            out.append({"line_id": build.add(SourceLine("pm_notes", text)), "block": header, "text": text})
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
        reply = llm.complete(SYSTEM, payload.json(), Consolidation)
    except LlmError:
        return Outcome(failed=True)
    return validate(reply, payload, today)


def validate(reply: Consolidation, payload: Payload, today: date) -> Outcome:
    """Keep the items that check out; drop each failing item on its own."""
    items, dropped = restore_consolidation(reply, payload.mapping)
    health = payload.data["project"]["project_health"]
    changes: dict[str, ChangeLine] = {}
    for item, restored in items["changes"]:
        if not _item_ok(payload, item.text, item.source, item.line_id, item.quote, snapshot_ok=True):
            continue
        text = squash(restored.text)
        key = text.casefold()
        kept = changes.get(key)
        changes[key] = ChangeLine(kept.text if kept else text, item.after_psr or bool(kept and kept.after_psr))
    concerns: list[str] = []
    for item, restored in items["concerns"]:
        if item.kind not in CONCERN_KINDS or (item.kind == "health_vs_evidence" and not health):
            continue
        if _item_ok(payload, item.text, item.source, item.line_id, item.quote, snapshot_ok=False):
            concerns.append(squash(restored.text))
    dates = _note_dates(items["dates_from_notes"], payload)
    return Outcome(
        _last_update(reply.last_update, payload, today), tuple(changes.values()), tuple(dict.fromkeys(concerns)),
        dates, failed=dropped and not (changes or concerns or dates),
    )


def restore_consolidation(reply: Consolidation, mapping: Mapping) -> tuple[dict[str, list[tuple[Any, Any]]], bool]:
    """Restore all text fields once, retaining redacted counterparts for grounding and citations.

    Pydantic has validated the reply's shape. Every string on an item goes through this boundary,
    including future free-text fields. One unresolved field drops its whole item.
    """
    result: dict[str, list[tuple[Any, Any]]] = {}
    dropped = False
    for name in _ITEM_TYPES:
        result[name] = []
        for item in getattr(reply, name):
            canonical = {key: mapping.canonicalize(value) if isinstance(value, str) else value
                         for key, value in item.model_dump().items()}
            restored = {key: mapping.restore(value) if isinstance(value, str) else value
                        for key, value in canonical.items()}
            if any(unresolved(value) for value in restored.values() if isinstance(value, str)):
                dropped = True
                continue
            result[name].append((type(item)(**canonical), type(item)(**restored)))
    return result, dropped


def _note_dates(items: list[tuple[DateFromNotes, DateFromNotes]], payload: Payload) -> tuple[NoteDate, ...]:
    """The dates that check out: a blank, relevant property, an exact quote, and a value the quoted line holds."""
    blank = set(payload.data["blank_date_properties"])
    found: dict[str, NoteDate] = {}
    for item, restored in items:
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
            found[name] = NoteDate(name, fuzzy=squash(restored.fuzzy_text or ""))
    return tuple(found.values())


def _item_ok(payload: Payload, text: str, source: str, line_id: int | None, quote: str | None, snapshot_ok: bool) -> bool:
    """Every item cites a real line of its source and quotes it exactly; a snapshot change cites a snapshot line."""
    if not squash(text) or source not in SOURCES or (source == "snapshot" and not snapshot_ok):
        return False
    line = payload.lines.get(line_id) if line_id is not None else None
    if line is None or line.origin != source or not quote or _bare(quote) != _bare(line.text):
        return False
    # A known name in any case is ungrounded too: the payload never holds one (redact.py).
    return ungrounded(text, payload.facts, date.fromisoformat(payload.data["today"])) is None and payload.redactor.names_in(text, payload.page_id) is None


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
