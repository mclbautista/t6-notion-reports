"""Build the Sunday briefing messages from a diff (spec section 7, without owed items).

Pure: no network, no clock. ``build_briefing`` returns the messages to send, in order: one DM per
coordinator, then Cyril's digest.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .contacts import Contact
from .diff import ProjectChange, group_by_coordinator, last_updated_text
from .telegram_html import (
    Block, Line, Span, bold, line, mention_handle, quote, render_messages,
)

BANNER = "🧪 TEST"
NO_CHANGES = "No changes on your projects this week"
HEALTH_MARK = {"Critical": "🔴", "At Risk": "🟠", "On Track": "🟡"}
NOTION_PAGE = "https://www.notion.so/"


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


def digest_missing(settings: Settings) -> bool:
    return settings.cyril.telegram_id is None


def build_briefing(
    changes: list[ProjectChange],
    coordinators: dict[str, str],
    contacts: dict[str, Contact],
    settings: Settings,
    today: date,
) -> list[OutboundMessage]:
    """``coordinators`` maps contact page id → name, for everyone who owns a project this week.

    Without a Telegram id for Cyril the digest is left out (the DMs still go); see ``digest_missing``.
    """
    groups = group_by_coordinator(changes)
    banner = BANNER if settings.test_banner else ""
    stamp = f"{today:%a %b} {today.day}"
    messages: list[OutboundMessage] = []
    fallbacks: list[str] = []
    quiet: list[str] = []

    for contact_id, name in sorted(coordinators.items(), key=lambda kv: (kv[1].lower(), kv[0])):
        projects = groups.by_coordinator.get(contact_id, (name, []))[1]
        contact = contacts.get(contact_id)
        chat = contact.telegram_id if contact else None
        if not projects:
            quiet.append(name)
        blocks: list[Block] = [line(NO_CHANGES)] if not projects else _dm_blocks(projects, stamp)
        key = f"psr-brief-{today.isoformat()}-{contact_id}"
        if chat is not None:
            messages += _parts(chat, None, blocks, banner, key, name)
        else:
            fallbacks.append(name)
            who = Span(name)
            if contact and contact.handle:
                who = mention_handle(contact.handle)
            blocks = [line(who, ":"), *blocks]
            messages += _parts(settings.group_chat_id, settings.group_topic_id, blocks, banner, key, name)

    if digest_missing(settings):
        return messages
    digest = _digest_blocks(groups, stamp, quiet, fallbacks, bool(changes))
    messages += _parts(settings.cyril.telegram_id, None, digest, banner, f"psr-digest-{today.isoformat()}", settings.cyril.name)
    return messages


def _parts(to, topic, blocks, banner, key, recipient) -> list[OutboundMessage]:
    texts = render_messages(blocks, prefix=banner)
    # The first part keeps the spec's key, so a rerun is deduplicated by the gateway.
    return [
        OutboundMessage(to, topic, text, key if n == 1 else f"{key}-{n}", recipient)
        for n, text in enumerate(texts, 1)
    ]


def _dm_blocks(projects: list[ProjectChange], stamp: str) -> list[Block]:
    blocks: list[Block] = [line(bold(f"PSR briefing · {stamp}")), line()]
    for ch in projects:
        blocks += project_blocks(ch)
    return blocks


def _digest_blocks(groups, stamp: str, quiet: list[str], fallbacks: list[str], any_changes: bool) -> list[Block]:
    blocks: list[Block] = [line(bold(f"PSR digest · {stamp}")), line()]
    if not any_changes:
        blocks.append(line("No changes on any project this week"))
    for name, projects in sorted(groups.by_coordinator.values(), key=lambda v: v[0].lower()):
        blocks += [line(bold(name)), *[b for ch in projects for b in project_blocks(ch)]]
    if groups.no_coordinator:
        blocks += [line(bold("No coordinator")), *[b for ch in groups.no_coordinator for b in project_blocks(ch)]]
    if quiet:
        blocks.append(line("No changes: " + ", ".join(quiet)))
    for name in fallbacks:
        blocks.append(line(f"⚠️ {name} has no Telegram id: briefing posted in the group topic"))
    return blocks


def project_blocks(ch: ProjectChange) -> list[Block]:
    """Heading, field changes and raw new PM Notes for one project, then a blank line."""
    title = f"{ch.code} · {ch.title}" if ch.code and ch.title else ch.name
    health = f"{HEALTH_MARK[ch.health]} {ch.health}" if ch.health in HEALTH_MARK else "Health not set"
    tag = {"added": " (new)", "removed": " (removed)"}.get(ch.kind, "")
    blocks: list[Block] = [
        line(bold(title, href=NOTION_PAGE + ch.page_id.replace("-", "")), f"{tag} · {health}"),
    ]
    for fc in ch.changes:
        blocks.append(line(f"• {fc.field}: {format_value(fc.old)} → {format_value(fc.new)}"))
    if ch.notes and (ch.notes.blocks or ch.notes.text_diff):
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


def format_value(value: object) -> str:
    if value in (None, "", []):
        return "(none)"
    if isinstance(value, list):
        return ", ".join(str(r.get("name") or r["id"]) if isinstance(r, dict) else str(r) for r in value)
    if isinstance(value, dict) and "start" in value:
        start, end = value["start"], value.get("end")
        return f"{start} – {end}" if end else str(start)
    return str(value)
