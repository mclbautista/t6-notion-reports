"""The Saturday briefing job: diff Friday against Monday, build the messages, send them."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .briefing import AFTER_PSR, OutboundMessage, Settings, build_briefing, digest_missing
from .concerns import FLAGS, FLAGS_NOTES, LEAVE, LLM, AgedConcerns, update_concerns
from .config import BriefSettings
from .consolidate import Outcome, Window, build_payload, consolidate
from .contacts import Contact, load_contacts
from .diff import diff_snapshots, display_names, sort_projects, unchanged_project
from .flags import project_flags
from .gateway import GatewayError
from .holidays import leave_lines
from .leaves import NO_HEADING_NOTE, UNAVAILABLE_NOTE, UNMATCHED_NOTE, leave_clashes, parse_leaves, payload as leave_payload
from .llm import LlmClient
from .mom import entry_label
from .mom_lines import Minutes, load_minutes
from .people_match import MatchSettings
from .redact import Redactor
from .schedule import MANILA
from .snapshots import (
    RELATION_FIELDS, default_diff_pair, latest_snapshot_ids, load_snapshot, previous_friday, snapshot_info,
)
from .status_gate import gate, is_archiving, looks_like_in_progress


class BriefFailed(Exception):
    """At least one message could not be queued. Everything else was still attempted."""


@dataclass(frozen=True)
class BriefResult:
    baseline: bool = False
    messages: tuple[OutboundMessage, ...] = ()
    sent: int = 0
    failed: int = 0
    changed: int = 0
    llm_failed: int = 0


def run_brief(
    conn: sqlite3.Connection,
    notion,
    contacts_ds: str,
    brief: BriefSettings,
    now: datetime,
    gateway=None,
    llm: LlmClient | None = None,
    dry_print: bool = False,
    out: Callable[[str], None] = print,
    log: Callable[[str], None] = print,
    meeting_notes_ds: str | None = None,
    show_llm_input: bool = False,
    projects_ds: str | None = None,
    matching: MatchSettings = MatchSettings(),
) -> BriefResult:
    """Send the briefing now. With ``dry_print`` (or no ``gateway``), print and send nothing.

    With ``llm``, each gated project gets one consolidation call: its changes, Concerns and last update.
    Without one the raw changes are shown and nothing is marked. ``show_llm_input`` prints each payload
    in full and makes no call at all, so what would leave the machine can be read first.

    With ``llm`` and ``projects_ds``, one more call parses the Projects database's "Upcoming Holidays and
    Leaves" lines, and a team member's leave near a project date becomes a Concern on that project.
    Names nobody matched, and a failed parse, are noted in Cyril's digest only.
    """
    dry_print = dry_print or show_llm_input  # inspecting payloads never sends
    pair = default_diff_pair(conn)
    first = False
    if pair is None:
        latest = latest_snapshot_ids(conn, 1)
        if not latest:
            out("first snapshot, no changes yet (need a Friday snapshot and an earlier Monday one)")
            log("brief baseline=True sent=0 failed=0")
            return BriefResult(baseline=True)
        # Only one comparison point exists: brief without a changes section.
        first = True
        old_id = new_id = latest[0]
    else:
        old_id, new_id = pair
    raw_old, raw_new = load_snapshot(conn, old_id), load_snapshot(conn, new_id)
    old_state, new_state = apply_gate(raw_old, raw_new, log)
    old_on = snapshot_info(conn, old_id).taken_at
    new_on = snapshot_info(conn, new_id).taken_at
    changes = [] if first else diff_snapshots(
        old_state, new_state,
        datetime.fromisoformat(old_on).astimezone(MANILA).date(),
        datetime.fromisoformat(new_on).astimezone(MANILA).date(),
    )
    changed = len(changes)
    names = display_names(old_state, new_state)
    new_day = datetime.fromisoformat(new_on).astimezone(MANILA).date()
    today = now.astimezone(MANILA).date()
    outcomes: dict[str, Outcome] = {}
    contacts = load_contacts(notion, contacts_ds)  # read once: the redaction, the leaves and the messages
    if llm is not None or show_llm_input:
        windows = diff_windows(conn, old_id, new_id, first, new_state, changes)
        minutes = None
        if meeting_notes_ds:
            try:
                minutes = load_minutes(notion, meeting_notes_ds, today)
            except Exception as exc:  # type only: the page may hold text. The brief goes on without Minutes.
                log(f"brief minutes unavailable error={type(exc).__name__}")
        # Contacts people, other projects and ids are redacted from free text before it is sent.
        redactor = Redactor.build(contacts.values(), (raw_old, raw_new))
        payloads = {
            p: build_payload(
                s, today, new_day, windows.get(p, []), *minutes_for(minutes, names[p], s), brief.pm_notes_weeks,
                redactor, p,
            ) for p, s in new_state.items()
        }
        for page_id in sorted(payloads, key=lambda p: (names[p].lower(), p)):
            if show_llm_input:
                out(f"--- llm input: {names[page_id]} ---\n{payloads[page_id].json(indent=2)}\n")
            elif llm is not None:
                outcomes[page_id] = consolidate(llm, payloads[page_id], today)
    leaves = Leaves(available=False)  # no leave call: nothing says a stored leave Concern went away
    if (llm is not None or show_llm_input) and projects_ds:
        leaves = leave_concerns(
            notion, projects_ds, llm, today, new_state, contacts,
            brief.leave_clash_days, matching, show_llm_input, out, log, redactor,
        )
    # Dates the notes state count as given, so they are known before the flags are worked out.
    stated = {p: {d.property: d.day for d in o.dates} for p, o in outcomes.items()}
    flagged = {p: project_flags(s, today, brief.lead_warning_days, stated.get(p)) for p, s in new_state.items()}
    note_dates = {p: note_date_lines(o) for p, o in outcomes.items() if o.dates}
    failed_ids = sorted(p for p, o in outcomes.items() if o.failed)
    llm_failed = sorted(names[p] for p in failed_ids)
    for page_id in failed_ids:
        log(f"brief llm unavailable project={page_id}")
    conn.commit()
    current = {}
    unavailable: set[tuple[str, str]] = set()
    for p, f in flagged.items():
        o = outcomes.get(p)
        # Code-made Concerns are recomputed every run. A source that could not be read (a failed or
        # skipped project call, unavailable leaves) neither re-lists nor resolves its open Concerns.
        if o is None or o.failed:
            unavailable.add((p, LLM))
            unavailable.add((p, FLAGS_NOTES))  # the dates from the notes are unknown, not gone
        if not leaves.available:
            unavailable.add((p, LEAVE))
        current[p] = (
            *((t, FLAGS_NOTES if t in f.from_notes else FLAGS) for t in f.concerns),
            *((t, LLM) for t in (o.concerns if o and not o.failed else ())),
            *((t, LEAVE) for t in leaves.concerns.get(p, ())),
        )
    aged = update_concerns(conn, current, today, brief.concern_chronic_weeks, unavailable)
    # A dry run shows the ages and the resolved line but leaves the store as it was.
    if dry_print or gateway is None:
        conn.rollback()
    else:
        conn.commit()
    changes = with_flags(changes, flagged, aged, names, new_state, new_day, note_dates)
    changes = with_consolidation(changes, outcomes, names, new_state, new_day, first)
    # Private warnings go only to an explicitly identified Cyril, never to whoever is listed first.
    cyril_id = brief.cyril_contact_id
    cyril = (contacts.get(cyril_id) or Contact(cyril_id, "Cyril")) if cyril_id else None
    digest_contacts = tuple(
        contacts.get(cid) or Contact(cid, cid)
        for cid in brief.digest_recipients
    ) or ((cyril,) if cyril else ())
    settings = Settings(
        cyril, brief.group_chat_id, brief.group_topic_id, brief.test_banner,
        digest_recipients=digest_contacts, coordinator_recipients=brief.coordinator_recipients,
    )
    messages = build_briefing(
        changes, coordinators_in(old_state, new_state), contacts, settings, now.astimezone(MANILA).date(),
        llm_failed, active_coordinators_in(new_state), first=first, notes=list(leaves.notes),
        cyril_notes=list(leaves.cyril_notes),
    )
    no_digest = digest_missing(settings)

    if dry_print or gateway is None:
        for m in messages:
            out(f"--- to={m.to} topic={m.topic} key={m.key} ({m.recipient}) ---\n{m.text}\n")
        log(f"brief dry_print=True messages={len(messages)} changed={changed} llm_failed={len(llm_failed)}")
        if no_digest:
            raise BriefFailed("No digest recipient has a TelegramUserID: digest not built")
        return BriefResult(messages=tuple(messages), changed=changed, llm_failed=len(llm_failed))

    sent = failed = 0
    for m in messages:
        try:
            gateway.notify(m.to, m.text, m.key, m.topic)
            sent += 1
        except GatewayError as exc:
            failed += 1
            log(f"brief send failed key={m.key} error={exc}")
    log(f"brief sent={sent} failed={failed} messages={len(messages)} changed={changed} llm_failed={len(llm_failed)}")
    if no_digest:
        raise BriefFailed("No digest recipient has a TelegramUserID: DMs sent, digest not sent")
    if failed:
        raise BriefFailed(f"{failed} of {len(messages)} messages were not queued")
    return BriefResult(messages=tuple(messages), sent=sent, changed=changed, llm_failed=len(llm_failed))


@dataclass(frozen=True)
class Leaves:
    """What the leave step gives the brief. ``available``: the leaves were read, so a leave Concern
    missing this run really went away."""

    concerns: dict[str, tuple[str, ...]] = field(default_factory=dict)  # project id -> clash Concerns
    notes: tuple[str, ...] = ()  # warnings for every digest recipient
    cyril_notes: tuple[str, ...] = ()  # for Cyril's digest only: names nobody matched
    available: bool = True


def leave_concerns(
    notion, projects_ds, llm, today, states, contacts, clash_days, matching, show_input, out, log,
    redactor: Redactor = Redactor(),
) -> Leaves:
    """Leave clash Concerns by project, and the digest notes.

    No leave lines under the heading means no call and nothing to clash. A failed call, an unreadable
    description or a missing heading gives no clash Concerns this run, says leaves were unavailable,
    and leaves the stored leave Concerns open.
    """
    try:
        description = notion.retrieve_data_source(projects_ds).get("description") or []
    except Exception as exc:  # type only: never the response
        log(f"brief leaves unavailable error={type(exc).__name__}")
        return Leaves(notes=() if show_input else (UNAVAILABLE_NOTE,), available=False)
    lines = leave_lines(description)
    if lines is None:  # the heading was renamed or removed: say so rather than read unrelated text
        log("brief leaves unavailable no heading")
        return Leaves(notes=() if show_input else (NO_HEADING_NOTE,), available=False)
    if not lines:
        return Leaves()
    if show_input:
        out(f"--- llm input: leaves ---\n{json.dumps(leave_payload(lines, today, redactor), ensure_ascii=False, indent=2)}\n")
        return Leaves(available=False)
    result = parse_leaves(llm, lines, today, redactor=redactor)
    if result.failed:
        log("brief leaves unavailable")
        return Leaves(notes=(UNAVAILABLE_NOTE,), available=False)
    found = leave_clashes(result.entries, contacts.values(), states, today, clash_days, matching)
    unmatched = (UNMATCHED_NOTE + ", ".join(found.unmatched),) if found.unmatched else ()
    return Leaves(found.concerns, cyril_notes=unmatched)


def diff_windows(conn, old_id, new_id, first, new_state, since_psr) -> dict[str, list[Window]]:
    """Each gated project's property changes for the two windows: since the last brief and since the PSR.

    ``since_last_brief`` runs from the previous Friday snapshot to this one, ``since_psr`` from the
    Monday one. A window with no earlier snapshot to compare with is left out. Team relations are left
    out: the LLM never gets the names in them.
    """
    if first:
        return {}
    new_at = datetime.fromisoformat(snapshot_info(conn, new_id).taken_at)
    spans = [("since_psr", old_id, {c.page_id: c for c in since_psr})]
    last = previous_friday(conn, new_id)
    if last is not None:
        prev = {p: s for p, s in load_snapshot(conn, last).items() if p in new_state}
        prev_at = datetime.fromisoformat(snapshot_info(conn, last).taken_at)
        diffs = diff_snapshots(prev, new_state, prev_at.astimezone(MANILA).date(), new_at.astimezone(MANILA).date())
        spans.insert(0, ("since_last_brief", last, {c.page_id: c for c in diffs}))
    out: dict[str, list[Window]] = {p: [] for p in new_state}
    for name, start_id, diffs in spans:
        start = datetime.fromisoformat(snapshot_info(conn, start_id).taken_at)
        for page_id in new_state:
            change = diffs.get(page_id)
            fields = tuple(fc for fc in change.changes if fc.field not in RELATION_FIELDS) if change else ()
            out[page_id].append(Window(name, start, new_at, fields))
    return out


def minutes_for(minutes: Minutes | None, display_name: str, state: dict[str, object]):
    """The (lines, meta) arguments of ``build_payload`` for one project; empty when the minutes say nothing of it."""
    if minutes is None:
        return (), None
    lines = minutes.lines_for(entry_label(display_name, state.get("Project Code")))
    return lines, {
        "page_date": minutes.page_date.isoformat(), "last_edited": minutes.last_edited, "sent_at": minutes.sent_at,
    }


def with_consolidation(changes, outcomes, names, state, snap_day, first):
    """Show each project's consolidated changes instead of its raw ones; mark a failed call.

    A project the LLM added changes for is listed even with no snapshot change; a project with a raw
    change but nothing consolidated, no flags and no Concerns drops out. A failed project keeps its raw
    changes and is listed so the failure is seen. The first brief has no earlier snapshot, so shows no changes.
    """
    if not outcomes:
        return changes
    shown = {c.page_id for c in changes}
    extra = [
        unchanged_project(p, names, state[p], snap_day) for p, o in outcomes.items()
        if p not in shown and (o.failed or (o.changes and not first))
    ]
    kept = []
    for ch in changes + extra:
        o = outcomes.get(ch.page_id)
        if o is None:
            kept.append(ch)
            continue
        ch.llm_failed, ch.llm_update = o.failed, o.last_update
        if not o.failed:
            ch.consolidated = True
            ch.summary = () if first else tuple(
                f"{c.text} ({AFTER_PSR})" if c.after_psr else c.text for c in o.changes
            )
        if ch.kind in ("added", "removed") or ch.llm_failed or ch.summary or ch.flags or ch.note_dates or ch.concerns or ch.resolved:
            kept.append(ch)
    return sort_projects(kept)


def coordinators_in(*states: dict[str, dict[str, object]]) -> dict[str, str]:
    """Contact page id → name for everyone who coordinates a project in any of the snapshots."""
    people: dict[str, str] = {}
    for state in states:
        for project in state.values():
            for ref in project.get("T6 Post Coordinator") or []:
                if isinstance(ref, dict):
                    people.setdefault(str(ref["id"]), str(ref.get("name") or ref["id"]))
    return people



def note_date_lines(outcome: Outcome) -> tuple[str, ...]:
    """"Submission Deadline (from PM Notes): Dec 1, 2026 · not in property", or the fuzzy wording as stated."""
    return tuple(
        f"{d.property} (from PM Notes): {d.fuzzy}" if d.day is None
        else f"{d.property} (from PM Notes): {d.day:%b} {d.day.day}, {d.day.year} · not in property"
        for d in outcome.dates
    )


def with_flags(changes, flagged, aged, names, state, snap_day, note_dates=None):
    """Attach each project's flags, aged Concerns and resolved Concerns; a project with flags or Concerns is listed too.

    Flags and open Concerns count as activity, so a project with no changes but one of them still
    reaches its coordinator. A resolved Concern also lists its project, so the "Resolved last week"
    line is seen once rather than dropped unread. A removed project has no current state to flag.
    A date the notes state, precise or fuzzy, counts as activity too, so it is always shown.
    """
    note_dates = note_dates or {}
    shown = set()
    for ch in changes:
        if ch.kind != "removed" and ch.page_id in flagged:
            a = aged.get(ch.page_id, AgedConcerns())
            ch.flags, ch.concerns, ch.resolved = flagged[ch.page_id].lines, a.open, a.resolved
            ch.note_dates = note_dates.get(ch.page_id, ())
        shown.add(ch.page_id)
    extra = []
    for page_id, f in flagged.items():
        a = aged.get(page_id, AgedConcerns())
        notes = note_dates.get(page_id, ())
        if page_id not in shown and (f.lines or a.open or a.resolved or notes):
            ch = unchanged_project(page_id, names, state[page_id], snap_day)
            ch.flags, ch.concerns, ch.resolved, ch.note_dates = f.lines, a.open, a.resolved, notes
            extra.append(ch)
    return sort_projects(changes + extra) if extra else changes


def apply_gate(old, new, log: Callable[[str], None] = print):
    """Drop every project outside the status gate from both snapshots, judged by its latest state.

    Runs before the LLM or any message. An excluded Status that looks like an In-Progress option is
    logged by project name, so the Notion data can be fixed.
    """
    names = display_names(old, new)
    latest = {**old, **new}
    keep = set()
    for page_id, state in latest.items():
        if gate(state.get("Status")) is not None:
            keep.add(page_id)
        elif looks_like_in_progress(state.get("Status")):
            log(f"brief status excluded looks like In-Progress project={names[page_id]} status={state.get('Status')!r}")
    return ({p: s for p, s in old.items() if p in keep}, {p: s for p, s in new.items() if p in keep})


def active_coordinators_in(state: dict[str, dict[str, object]]) -> set[str]:
    """Contact page ids of coordinators with an In-Progress (any kind) or Archiving project."""
    return {
        str(ref["id"])
        for project in state.values()
        if gate(project.get("Status")) == "in_progress" or is_archiving(project.get("Status"))
        for ref in project.get("T6 Post Coordinator") or [] if isinstance(ref, dict)
    }
