"""Build the Saturday briefing messages from a diff (spec section 7).

Pure: no network, no clock. ``build_briefing`` returns the messages to send, in order: one DM per
coordinator, then Cyril's digest.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .concerns import RESOLVED_LABEL
from .contacts import Contact
from .diff import ProjectChange, group_by_coordinator, health_rank, last_updated_text
from .telegram_html import (
    LIMIT, Block, Line, Span, bold, line, mention_handle, quote, render_messages,
)

BANNER = "🧪 TEST"
FIRST_BRIEF = "First brief: no comparison yet"
SECTION_TITLES = {"on_hold": "On-Hold", "in_progress": "In-Progress", "leads": "Leads"}
DM_SECTIONS = ("on_hold", "in_progress")
DIGEST_SECTIONS = ("on_hold", "in_progress", "leads")
NOTION_PAGE = "https://www.notion.so/"
LLM_UNAVAILABLE = "(LLM unavailable)"
AFTER_PSR = "after PSR"


@dataclass(frozen=True)
class OutboundMessage:
    to: int
    topic: int | None
    text: str
    key: str
    recipient: str  # for logs: a name, never message text


@dataclass(frozen=True)
class Settings:
    cyril: Contact
    group_chat_id: int
    group_topic_id: int | None
    test_banner: bool = False
    digest_recipients: tuple[Contact, ...] = ()


def digest_missing(settings: Settings) -> bool:
    recipients = settings.digest_recipients or ((settings.cyril,) if settings.cyril else ())
    return not any(c.telegram_id is not None for c in recipients)


def build_briefing(
    changes: list[ProjectChange],
    coordinators: dict[str, str],
    contacts: dict[str, Contact],
    settings: Settings,
    today: date,
    llm_failed: list[str] | None = None,
    active: set[str] | None = None,
    first: bool = False,
    limit: int = LIMIT,
    notes: list[str] | None = None,
) -> list[OutboundMessage]:
    """``coordinators`` maps contact page id → name, for everyone who owns a project this week.

    ``changes`` holds the changed projects plus any unchanged project with flags, Concerns or
    consolidated changes. A project outside the status gate (``group`` is None) is left out.
    ``llm_failed`` names the projects whose LLM call failed, for Cyril's digest.

    The DM has On-Hold then In-Progress sections. The digest groups by coordinator, alphabetically,
    each with On-Hold, In-Progress, then Leads. Empty sections are left out, and a section is sorted by
    Project Health, then name.

    A coordinator with nothing to show in the DM gets no message at all (no DM, no group fallback).
    Their name goes on the digest's "No changes" line only if their id is in ``active``; ``None``
    means no filtering. ``first`` marks the first brief, which has no earlier snapshot to compare with.

    ``notes`` are extra warning lines for the digest only (unmatched leave names, leaves unavailable).

    Without a Telegram id for Cyril the digest is left out (the DMs still go); see ``digest_missing``.
    """
    changes = [ch for ch in changes if ch.group in SECTION_TITLES]
    groups = group_by_coordinator(changes)
    banner = BANNER if settings.test_banner else ""
    stamp = f"{today:%a %b} {today.day}"
    notice = [line(FIRST_BRIEF)] if first else []
    messages: list[OutboundMessage] = []
    fallbacks: list[str] = []
    quiet: list[str] = []

    for contact_id, name in sorted(coordinators.items(), key=lambda kv: (kv[1].lower(), kv[0])):
        projects = [
            ch for ch in groups.by_coordinator.get(contact_id, (name, []))[1] if ch.group in DM_SECTIONS
        ]
        contact = contacts.get(contact_id)
        chat = contact.telegram_id if contact else None
        if not projects:
            if active is None or contact_id in active:
                quiet.append(name)
            continue
        head: list[Block] = [line(bold(f"PSR briefing · {stamp}")), *notice, line()]
        key = f"notion-reports-{today.isoformat()}-{contact_id}"
        units = _units([(None, projects)], DM_SECTIONS, today)
        if chat is not None:
            messages += _send(chat, None, _pack(head, units, [], banner, limit), key, name)
        else:
            fallbacks.append(name)
            who = Span(name)
            if contact and contact.handle:
                who = mention_handle(contact.handle)
            head = [line(who, ":"), *head]
            messages += _send(
                settings.group_chat_id, settings.group_topic_id, _pack(head, units, [], banner, limit), key, name,
            )

    if digest_missing(settings):
        return messages
    head = [line(bold(f"PSR digest · {stamp}")), *notice, line()]
    if not changes and not first:
        head.append(line("No changes on any project this week"))
    people = sorted(groups.by_coordinator.values(), key=lambda v: v[0].lower())
    if groups.no_coordinator:
        people.append(("No coordinator", groups.no_coordinator))
    units = _units([(line(bold(name)), projects) for name, projects in people], DIGEST_SECTIONS, today)
    tail: list[Block] = []
    if quiet:
        tail.append(line("No changes: " + ", ".join(quiet)))
    for name in fallbacks:
        tail.append(line(f"⚠️ {name} has no Telegram id: briefing posted in the group topic"))
    if llm_failed:
        tail.append(line("⚠️ LLM unavailable: " + ", ".join(llm_failed)))
    tail += [line(note) for note in notes or ()]
    texts = _pack(head, units, tail, banner, limit)
    recipients = settings.digest_recipients or ((settings.cyril,) if settings.cyril else ())
    multi = len(recipients) > 1
    for contact in recipients:
        if contact.telegram_id is not None:
            key = f"psr-digest-{today.isoformat()}-{contact.page_id}" if multi else f"psr-digest-{today.isoformat()}"
            messages += _send(contact.telegram_id, None, texts, key, contact.name)
    return messages


@dataclass(frozen=True)
class _Unit:
    """One project with the headings that introduce it, so a message can split between projects."""

    coord: Line | None  # the coordinator's name line (digest only)
    section: str
    first_in_coord: bool
    first_in_section: bool
    blocks: list[Block]

    def lead(self) -> list[Block]:
        out: list[Block] = []
        if self.coord and self.first_in_coord:
            out.append(self.coord)
        if self.first_in_section:
            out.append(line(bold(self.section)))
        return out

    def restart(self) -> list[Block]:
        """What a new part starts with: the coordinator and the section heading, "(cont.)" if repeated."""
        heading = self.section if self.first_in_section else f"{self.section} (cont.)"
        return [*([self.coord] if self.coord else []), line(bold(heading))]


def _units(people, sections: tuple[str, ...], today: date) -> list[_Unit]:
    units: list[_Unit] = []
    for coord, projects in people:
        first_in_coord = True
        for group in sections:
            members = sorted(
                (ch for ch in projects if ch.group == group), key=lambda ch: (health_rank(ch.health), ch.name.lower(), ch.page_id),
            )
            for n, ch in enumerate(members):
                units.append(_Unit(
                    coord, SECTION_TITLES[group], first_in_coord, n == 0,
                    project_blocks(ch, today),
                ))
                first_in_coord = False
    return units


def _pack(head: list[Block], units: list[_Unit], tail: list[Block], banner: str, limit: int) -> list[str]:
    """Render the head, the projects and the tail into messages, splitting only between projects."""
    def fits(blocks: list[Block]) -> bool:
        return len(render_messages(blocks, prefix=banner, limit=limit)) <= 1

    parts: list[list[Block]] = []
    current: list[Block] = list(head)
    placed = False
    for unit in units:
        if placed and not fits(current + unit.lead() + unit.blocks):
            parts.append(current)
            current = unit.restart() + unit.blocks
        else:
            current = current + unit.lead() + unit.blocks
        placed = True
    parts.append(current + tail)
    return [text for blocks in parts for text in render_messages(blocks, prefix=banner, limit=limit)]


def _send(to, topic, texts: list[str], key: str, recipient: str) -> list[OutboundMessage]:
    # The first part keeps the spec's key, so a rerun is deduplicated by the gateway.
    return [
        OutboundMessage(to, topic, text, key if n == 1 else f"{key}-{n}", recipient)
        for n, text in enumerate(texts, 1)
    ]


def project_blocks(ch: ProjectChange, today: date | None = None) -> list[Block]:
    """Heading, flags, Concerns and the week's changes for one project, then a blank line.

    The heading has the name, Stage, the date properties that are set, and the weeks since the project's last update: the "As of" date
    the LLM judged real, else Notion's last-edited time. It shows neither Status (the section says it)
    nor Project Health (the sort order says it), and has no link. The changes are the LLM's consolidated
    list; when its call failed they are the raw field changes and new PM Notes, marked "(LLM unavailable)".
    """
    title = f"{ch.code} · {ch.title}" if ch.code and ch.title else ch.name
    tag = {"added": " (new)", "removed": " (removed)"}.get(ch.kind, "")
    extras = [x for x in (ch.stage, *ch.dates, updated_text(ch.llm_update or ch.edited, today)) if x]
    blocks: list[Block] = [line(bold(title), "".join([tag, *(f" · {x}" for x in extras)]))]
    if ch.llm_failed:
        blocks.append(line(LLM_UNAVAILABLE))
    blocks += [line(text) for text in ch.flags]
    blocks += [line(f"📅 {text}") for text in ch.note_dates]
    if ch.concerns:
        blocks.append(line(bold("Concerns")))
        blocks += [line(f"• {text}") for text in ch.concerns]
    if ch.resolved:
        blocks.append(line(f"{RESOLVED_LABEL}: " + "; ".join(ch.resolved)))
    blocks += [line(f"• {text}") for text in ch.summary]
    for fc in () if ch.consolidated else ch.changes:
        blocks.append(line(f"• {fc.field}: {format_value(fc.old)} → {format_value(fc.new)}"))
    if not ch.consolidated and ch.notes and (ch.notes.blocks or ch.notes.text_diff):
        blocks.append(line(f"📝 PM Notes · last updated {last_updated_text(ch.last_updated)}"))
        lines: list[Line] = []
        for bc in ch.notes.blocks:
            if lines:
                lines.append(line())
            lines += [line(text) for text in bc.block.text.splitlines()]
        lines += [line(text) for text in ch.notes.text_diff or []]
        blocks.append(quote(*lines))
    blocks.append(line())
    return blocks


def updated_text(edited: date | None, today: date | None) -> str:
    """"updated this week" under 7 days, else "updated 3 wks ago"; nothing when unknown."""
    if edited is None or today is None:
        return ""
    weeks = max(0, (today - edited).days) // 7
    if weeks == 0:
        return "updated this week"
    return f"updated {weeks} wk{'s' if weeks > 1 else ''} ago"


def format_value(value: object) -> str:
    if value in (None, "", []):
        return "(none)"
    if isinstance(value, list):
        return ", ".join(str(r.get("name") or r["id"]) if isinstance(r, dict) else str(r) for r in value)
    if isinstance(value, dict) and "start" in value:
        start, end = value["start"], value.get("end")
        return f"{start} – {end}" if end else str(start)
    return str(value)
