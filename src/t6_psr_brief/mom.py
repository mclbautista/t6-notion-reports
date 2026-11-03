"""The PSR minutes draft: which projects appear, in which group, and the Notion page it becomes.

Spec section 8. Bullets are the raw PM Notes blocks; LLM condensing comes in a later ticket.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .diff import ProjectChange, diff_snapshots, display_names, last_updated_text
from .pm_notes import last_updated
from .snapshots import squash

# (key, emoji, title). Page order: critical, at risk, on track, archiving, incoming, then Others.
GROUPS = (
    ("critical", "🔴", "Critical / off track"),
    ("at_risk", "🟠", "At risk / watchlist"),
    ("on_track", "🟡", "On track"),
    ("archiving", "🔵", "Archiving"),
    ("incoming", "🟢", "Incoming"),
)
OTHERS_TITLE = "⚫️ Others"
UNCHANGED_TITLE = "Unchanged (Notion only, not sent)"
HEALTH_NOT_SET = "Health not set"

ARCHIVING_STAGES = {"archiving", "for drives turnover", "waiting for payment"}
INCOMING_STATUSES = {"leads", "lead", "negotiations", "proposal made"}
CHANGE_FIELDS = {"Health", "Stage", "Status"}
_HEALTH_GROUP = {"critical": "critical", "at risk": "at_risk", "on track": "on_track"}


def norm(value: object) -> str:
    """Compare form for Status and Stage: no emoji, whitespace squashed, lower case."""
    return squash(re.sub(r"[^\w\s-]", "", str(value or ""))).lower()


def group_of(state: dict[str, object]) -> str:
    """First match wins (spec 8.2). Empty or unknown Health lands in on_track."""
    if norm(state.get("Status")) == "archiving" or norm(state.get("Stage")) in ARCHIVING_STAGES:
        return "archiving"
    if norm(state.get("Status")) in INCOMING_STATUSES:
        return "incoming"
    return _HEALTH_GROUP.get(norm(state.get("Health")), "on_track")


@dataclass
class Entry:
    page_id: str
    label: str  # code in capitals (or the name), "CODE (Name)" when a code is shared
    group: str
    bullets: list[str] = field(default_factory=list)
    last_updated_line: str | None = None  # set instead of bullets when nothing is new
    flag: str | None = None


@dataclass
class Draft:
    meeting_date: date
    entries: list[Entry]  # shown, in page order
    unchanged: list[str]  # Notion only
    project_ids: list[str]  # every project on the page, for the Project relation


def build_draft(
    old: dict[str, dict[str, object]], new: dict[str, dict[str, object]],
    old_date: date, new_date: date, meeting_date: date,
) -> Draft:
    """``old`` is the Friday snapshot, ``new`` the Monday one taken after the PSR."""
    names = display_names(old, new)
    changed = {c.page_id: c for c in diff_snapshots(old, new, old_date, new_date) if c.kind != "removed"}
    shown: list[Entry] = []
    unchanged: list[Entry] = []
    for page_id, state in new.items():
        change = changed.get(page_id)
        entry = _entry(page_id, names[page_id], state, change, new_date)
        group = entry.group
        is_changed = change is not None and (
            change.notes is not None or any(fc.field in CHANGE_FIELDS for fc in change.changes))
        always = norm(state.get("Status")) == "in-progress" or group == "archiving"
        if is_changed or always:
            if not is_changed:
                entry.bullets = []
            shown.append(entry)
        else:
            unchanged.append(entry)
    order = {key: i for i, (key, _, _) in enumerate(GROUPS)}
    shown.sort(key=lambda e: (order[e.group], e.label.lower(), e.page_id))
    unchanged.sort(key=lambda e: (e.label.lower(), e.page_id))
    return Draft(
        meeting_date, shown,
        [f"{e.label} - {e.last_updated_line}" for e in unchanged],
        [e.page_id for e in shown + unchanged],
    )


def _entry(page_id: str, label: str, state, change: ProjectChange | None, snap_date: date) -> Entry:
    group = group_of(state)
    code = str(state.get("Project Code") or "")
    if code and label.startswith(code):
        label = code.upper() + label[len(code):]
    entry = Entry(page_id, label, group)
    if group == "on_track" and not str(state.get("Health") or "").strip():
        entry.flag = HEALTH_NOT_SET
    updated = f"last updated {last_updated_text(last_updated(state.get('PM Notes'), snap_date))}"
    entry.last_updated_line = updated
    if change:
        entry.bullets = _bullets(change)
    return entry


def _bullets(change: ProjectChange) -> list[str]:
    out: list[str] = []
    if change.notes:
        for bc in change.notes.blocks:
            out += bc.block.bullets or [bc.block.header]
        out += [ln[2:] for ln in change.notes.text_diff or [] if ln.startswith("+ ")]
    if not out:
        out = [f"{fc.field}: {_show(fc.old)} → {_show(fc.new)}"
               for fc in change.changes if fc.field in CHANGE_FIELDS]
    return out


def _show(value: object) -> str:
    return "(none)" if value in (None, "") else str(value)


# ---- Notion blocks -------------------------------------------------------------------------

def render_blocks(draft: Draft, attendee_names: list[str], others: list[str], mentions: list[str]) -> list[dict]:
    d = draft.meeting_date
    blocks = [
        _block("heading_1", "PSR MOM"),
        _block("paragraph", f"{d:%A, %b} {d.day}, {d.year}"),
        _block("paragraph", "Attendees: " + ", ".join(attendee_names)),
    ]
    for key, emoji, title in GROUPS:
        entries = [e for e in draft.entries if e.group == key]
        if not entries:
            continue
        blocks.append(_block("heading_2", f"{emoji} {title}"))
        blocks += [_project_block(e) for e in entries]
    blocks.append(_block("heading_2", OTHERS_TITLE))
    if others:
        blocks.append(_block("bulleted_list_item", "Leaves and holidays (next 14 days)",
                             [_block("bulleted_list_item", t) for t in others]))
    else:
        blocks.append(_block("bulleted_list_item", "Leaves and holidays: none in the next 14 days"))
    blocks += [_block("bulleted_list_item", "Heavy workloads"), _block("bulleted_list_item", "Reminders")]
    if mentions:
        blocks.append(_block("paragraph", " ".join(mentions)))
    if draft.unchanged:
        blocks.append(_block("heading_2", UNCHANGED_TITLE))
        blocks += [_block("bulleted_list_item", t) for t in draft.unchanged]
    return blocks


def _project_block(e: Entry) -> dict:
    label = f"{e.label} ({e.flag})" if e.flag else e.label
    if e.bullets:
        return _block("bulleted_list_item", label, [_block("bulleted_list_item", b) for b in e.bullets], bold=True)
    return _block("bulleted_list_item", f"{label} - {e.last_updated_line}")


def _block(kind: str, text: str, children: list[dict] | None = None, bold: bool = False) -> dict:
    body: dict = {"rich_text": rich_text(text, bold)}
    if children:
        body["children"] = children
    return {"object": "block", "type": kind, kind: body}


def rich_text(text: str, bold: bool = False) -> list[dict]:
    """Notion caps one text object at 2000 characters."""
    chunks = [text[i:i + 2000] for i in range(0, len(text), 2000)] or [""]
    item = lambda c: {"type": "text", "text": {"content": c}, **({"annotations": {"bold": True}} if bold else {})}
    return [item(c) for c in chunks]
