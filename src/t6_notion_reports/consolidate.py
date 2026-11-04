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

from pydantic import BaseModel

from .condense import extract_facts, ungrounded, upcoming
from .diff import FieldChange
from .flags import ARCHIVAL, PLAYDATE, SUBMISSION, relevant_dates, stored_day
from .llm import LlmClient, LlmError
from .pm_notes import parse_pm_notes
from .schedule import MANILA
from .snapshots import squash

DEFAULT_PM_NOTES_WEEKS = 52
CONCERN_KINDS = ("open_item", "health_vs_evidence", "stale_milestone", "source_conflict")
SOURCES = {"pm_notes", "minutes"}
DATE_KEYS = {PLAYDATE: "playdate", SUBMISSION: "submission_deadline", ARCHIVAL: "archival_deadline"}

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

When sources disagree, prefer the newest by its timestamp (minutes.last_edited, the end of a snapshot window, the PM Notes "As of" date). Raise source_conflict only when you cannot tell which source is newer.
For a change, source is pm_notes, minutes or snapshot; for pm_notes and minutes give the line_id and an exact quote, for snapshot leave both null.
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


class Consolidation(BaseModel):
    """The reply. Kinds and sources are plain text here so one odd item cannot void the whole reply."""

    last_update: LastUpdate | None = None
    changes: list[Change] = []
    concerns: list[Concern] = []


@dataclass(frozen=True)
class SourceLine:
    origin: str  # pm_notes | minutes
    text: str
    header: bool = False  # an "As of" header line


@dataclass(frozen=True)
class Payload:
    """What leaves the machine for one project, and the lines the reply may cite."""

    data: dict
    lines: dict[int, SourceLine]
    facts: str  # every value in the payload as text, for the grounding check

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
class Outcome:
    """What one project shows from the LLM. ``failed``: the call or its JSON failed, so code-only output."""

    last_update: date | None = None
    changes: tuple[ChangeLine, ...] = ()
    concerns: tuple[str, ...] = ()
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
) -> Payload:
    """The payload for one project. Nothing about other projects, people or Notion ids goes in.

    ``minutes_meta`` holds the minutes page's date, last-edited time and Sent at; it is sent only with
    lines about this project.
    """
    build = _Build()
    notes = _notes_lines(state.get("PM Notes"), today, snapshot_day, notes_weeks, build)
    dates = {name: stored_day(state.get(name)) for name in DATE_KEYS}
    relevant = relevant_dates(state.get("Status"))
    data: dict = {
        "today": today.isoformat(),
        "project": {
            "name": squash(str(state.get("Name") or "")),
            "stage": squash(str(state.get("Stage") or "")) or None,
            "status": squash(str(state.get("Status") or "")) or None,
            "project_health": squash(str(state.get("Project Health") or "")) or None,
        },
        "dates": {key: (dates[name].isoformat() if dates[name] else None) for name, key in DATE_KEYS.items()},
        "blank_date_properties": [DATE_KEYS[n] for n in relevant if dates[n] is None],
        "pm_notes": notes,
        "snapshot_changes": [
            {
                "window": w.name,
                "from": w.start.astimezone(MANILA).isoformat(timespec="seconds"),
                "to": w.end.astimezone(MANILA).isoformat(timespec="seconds"),
                "changes": [{"field": c.field, "old": _plain(c.old), "new": _plain(c.new)} for c in w.changes],
            }
            for w in windows
        ],
        "minutes": None,
    }
    if minutes_lines and minutes_meta:
        data["minutes"] = {
            "page_date": minutes_meta.get("page_date"),
            "last_edited": minutes_meta.get("last_edited"),
            "sent_at": minutes_meta.get("sent_at"),
            "lines": [{"line_id": build.add(SourceLine("minutes", t)), "text": t} for t in minutes_lines],
        }
    return Payload(data, build.lines, _facts(data))


def _notes_lines(raw: object, today: date, snapshot_day: date, weeks: int, build: _Build) -> list[dict]:
    """Numbered PM Notes lines from the last ``weeks`` weeks, plus older blocks that hold a future date."""
    parsed = parse_pm_notes(str(raw) if raw else "", snapshot_day)
    out: list[dict] = []
    for text in parsed.preamble.splitlines():
        out.append({"line_id": build.add(SourceLine("pm_notes", text)), "block": None, "text": text})
    cutoff = today - timedelta(weeks=weeks)
    for block in parsed.blocks:
        if block.header_date and block.header_date < cutoff and not _has_future_date(block.text, today):
            continue
        out.append({"line_id": build.add(SourceLine("pm_notes", block.header, header=True)),
                    "block": block.header, "text": block.header})
        for bullet in block.bullets:
            text = f"• {bullet}"
            out.append({"line_id": build.add(SourceLine("pm_notes", text)), "block": block.header, "text": text})
    return out


def _has_future_date(text: str, today: date) -> bool:
    return any(upcoming(md, today) for md in extract_facts(text).dates)


def _plain(value: object) -> object:
    if isinstance(value, dict) and "start" in value:
        start, end = value["start"], value.get("end")
        return f"{start} – {end}" if end else start
    if isinstance(value, list):
        return [str(r.get("name") or r["id"]) if isinstance(r, dict) else str(r) for r in value]
    return value


def _facts(data: dict) -> str:
    """Every value in the payload (not its keys) as text, plus "Dec 1" for each ISO date in it."""
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
        values.append(f"{when:%b} {when.day}")
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
    health = payload.data["project"]["project_health"]
    changes: dict[str, ChangeLine] = {}
    for item in reply.changes:
        if not _item_ok(payload, item.text, item.source, item.line_id, item.quote, snapshot_ok=True):
            continue
        key = squash(item.text).casefold()
        kept = changes.get(key)
        changes[key] = ChangeLine(kept.text if kept else squash(item.text), item.after_psr or bool(kept and kept.after_psr))
    concerns: list[str] = []
    for item in reply.concerns:
        if item.kind not in CONCERN_KINDS or (item.kind == "health_vs_evidence" and not health):
            continue
        if _item_ok(payload, item.text, item.source, item.line_id, item.quote, snapshot_ok=False):
            concerns.append(squash(item.text))
    return Outcome(_last_update(reply.last_update, payload, today), tuple(changes.values()), tuple(dict.fromkeys(concerns)))


def _item_ok(payload: Payload, text: str, source: str, line_id: int | None, quote: str | None, snapshot_ok: bool) -> bool:
    if not squash(text):
        return False
    if source == "snapshot" and snapshot_ok:
        pass
    elif source in SOURCES:
        line = payload.lines.get(line_id) if line_id is not None else None
        if line is None or line.origin != source or not quote or _bare(quote) != _bare(line.text):
            return False
    else:
        return False
    return ungrounded(text, payload.facts) is None


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
    return when if when <= today else None


def _bare(text: str) -> str:
    return squash(text).lstrip("•-*·– ").strip()
