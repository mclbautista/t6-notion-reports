"""Reads through the t6 API (ADR 0002 in t6-core), shaped as the Notion reads the jobs already use.

``ApiNotion`` wraps the Notion client. Contacts, the Projects database notices and Meeting Notes are
read through ``t6.client.Client`` and handed to the jobs in the shape they read today, so the jobs and
their output do not change. Writes (the Minutes) and every other read go to the wrapped Notion client.
Leaving ``T6_API_URL`` unset keeps the old path: that is the rollback.

An API that cannot be reached, or refuses, is an ``ApiUnavailable``, an ``OSError`` like the transport
errors Notion gives, so each job treats it as the Notion outage it handles today.

Projects and Job Orders: ``snapshots=True`` also builds their snapshot records from ``project list`` and
``jo list``. The production wiring leaves it off, because those verbs do not carry everything the
snapshots hold (see ``docs/agents`` notes in the ticket: the date ``end``, every related Project of a
Job Order). ``compare-api`` switches it on to show the difference on live data.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any, Mapping

from t6.client import APIError, Client, Unavailable
from t6.verbs import (
    Done, JoListInput, MeetingNotesListInput, PersonListInput, ProjectListInput, StudioNoticesInput,
)

from .contacts import Contact
from .jo_snapshots import JoRecord, manila_instant
from .snapshots import LAST_EDITED, RELATION_FIELDS, ProjectRecord

# The full-list reads are slow: ``jo list`` reads every page body and ``meeting-notes list`` every
# note body. The client's own 10 s limit is too short for them.
READ_TIMEOUT = 120

# Project field -> the ``project list`` field that holds its related Contacts.
_PROJECT_RELATIONS = {
    "T6 Post Coordinator": "post_coordinator_ids", "Editors": "editor_ids", "Online Editor": "online_editor_ids",
    "Colorist": "colorist_ids", "Sound Tech": "sound_tech_ids", "Assistant Editor": "assistant_editor_ids",
    "Director": "director_ids", "Cinematographer": "cinematographer_ids",
}
assert set(_PROJECT_RELATIONS) | {"Post Supe"} == set(RELATION_FIELDS)


class ApiUnavailable(OSError):
    """The t6 API did not answer or refused. The message names the kind of failure, never the data."""


def client_from_env(env: Mapping[str, str] | None = None) -> Client | None:
    """A client for ``T6_API_URL`` and ``T6_API_SECRET``; None when the URL is unset (the old Notion path)."""
    env = os.environ if env is None else env
    if not env.get("T6_API_URL", "").strip():
        return None
    return Client(env["T6_API_URL"].strip(), env.get("T6_API_SECRET", ""), timeout=READ_TIMEOUT)


class ApiNotion:
    def __init__(self, notion, client, projects_ds: str, meeting_notes_ds: str | None, snapshots: bool = False):
        self._notion, self._client = notion, client
        self._projects_ds, self._meeting_ds = projects_ds, meeting_notes_ds
        self.snapshots = snapshots
        self._children: dict[str, list[dict]] = {}
        self._ids = 0

    # Everything the jobs read that the API does not serve, and every write, goes to Notion.
    def __getattr__(self, name: str):
        return getattr(self._notion, name)

    def _read(self, call, inputs) -> dict[str, Any]:
        try:
            outcome = call(inputs)
        except APIError as exc:
            raise ApiUnavailable(f"t6 API error status={exc.status}") from None
        if isinstance(outcome, Unavailable):
            raise ApiUnavailable("t6 API unreachable")
        if not isinstance(outcome, Done):
            raise ApiUnavailable(f"t6 API answered {outcome.outcome}")
        return outcome.result

    # ---- Contacts ----
    def _people(self) -> list[dict]:
        return self._read(self._client.person_list, PersonListInput())["people"]

    def read_contacts(self) -> dict[str, Contact]:
        """Contacts by Notion page id, as ``load_contacts`` gives them."""
        out: dict[str, Contact] = {}
        for p in self._people():
            handle = (p.get("telegram_handle") or "").lstrip("@").strip() or None
            out[p["page_id"]] = Contact(
                p["page_id"], p.get("name") or p["page_id"], handle,
                p.get("telegram_user_id"), ", ".join(p.get("aliases") or ()),
            )
        return out

    def read_contact_names(self) -> dict[str, str | None]:
        return {p["page_id"]: p.get("name") or None for p in self._people()}

    # ---- the Projects database notices ----
    def retrieve_data_source(self, data_source_id: str) -> dict:
        if data_source_id != self._projects_ds:
            return self._notion.retrieve_data_source(data_source_id)
        notices = self._read(self._client.studio_notices, StudioNoticesInput())
        runs = notices.get("runs") or []
        return {"id": data_source_id, "description": [
            {"plain_text": r["text"], "annotations": {"bold": bool(r.get("bold"))}} for r in runs]}

    # ---- Meeting Notes ----
    def query_data_source(self, data_source_id: str, filter: dict | None = None) -> list[dict]:
        if data_source_id != self._meeting_ds:
            return self._notion.query_data_source(data_source_id, filter)
        inputs = MeetingNotesListInput(**_note_filter(filter))
        notes = self._read(self._client.meeting_notes_list, inputs)["meeting_notes"]
        return [self._note_page(n) for n in notes]

    def list_block_children(self, block_id: str) -> list[dict]:
        return list(self._children[block_id])

    def _note_page(self, note: dict) -> dict:
        def date_prop(value): return {"type": "date", "date": {"start": value} if value else None}
        page_id = note["page_id"]
        self._children[page_id] = [self._block(b) for b in note.get("body") or []]
        return {"id": page_id, "last_edited_time": _notion_time(note.get("last_edited")), "properties": {
            "Name": {"type": "title", "title": [{"plain_text": note.get("name") or ""}]},
            "Meeting Date": date_prop(note.get("meeting_date")),
            "Sent at": date_prop(note.get("sent_at")),
            "Ready to send": {"type": "checkbox", "checkbox": bool(note.get("ready_to_send"))},
            "Team Attendees": {"type": "people", "people": [
                {"id": i, "name": n} for i, n in zip(note.get("attendee_ids") or (), note.get("attendee_names") or ())]},
        }}

    def _block(self, b: dict) -> dict:
        """A body block from ``meeting-notes list`` as the Notion block the Minutes code reads."""
        self._ids += 1
        block_id = f"api-block-{self._ids}"
        kids = b.get("children") or []
        self._children[block_id] = [self._block(k) for k in kids]
        content: dict[str, Any] = {"rich_text": [
            {"plain_text": s["text"], "href": s.get("url"),
             "annotations": {"bold": bool(s.get("bold")), "code": bool(s.get("code"))}}
            for s in b.get("spans") or []]}
        if b["kind"] == "to_do":
            content["checked"] = bool(b.get("checked"))
        return {"id": block_id, "type": b["kind"], "has_children": bool(kids), b["kind"]: content}

    # ---- Projects and Job Orders snapshots (compare-api only; see the module docstring) ----
    def project_records(self) -> list[ProjectRecord]:
        people = self._people()
        to_page = {p["t6_id"]: p["page_id"] for p in people if p.get("t6_id")}
        names = {p["page_id"]: p.get("name") or None for p in people}

        def related(ids):
            pages = [to_page.get(i, i) for i in ids or ()]
            return [{"id": i, "name": names.get(i)} for i in pages]

        records = []
        for p in self._read(self._client.project_list, ProjectListInput())["projects"]:
            fields: dict[str, object] = {
                "Name": p["name"], "Project Code": p.get("project_code"), "Stage": p.get("stage"),
                "Status": p.get("status"), "Project Health": p.get("project_health"),
                "PM Notes": p.get("pm_notes") or "",
                "Playdate": _date_field(p.get("playdate")),
                "Submission Deadline": _date_field(p.get("submission_deadline")),
                "Archival Deadline": _date_field(p.get("archival_deadline")),
                "Post Supe": [],  # Notion has no property by that name: the snapshot holds an empty list (#121)
                LAST_EDITED: _notion_time(p.get("last_edited")),
            }
            for name, key in _PROJECT_RELATIONS.items():
                fields[name] = related(p.get(key))
            records.append(ProjectRecord(p["page_id"], fields))
        return records

    def job_order_records(self) -> list[JoRecord]:
        people = self._people()
        to_page = {p["t6_id"]: p["page_id"] for p in people if p.get("t6_id")}
        names = {p["page_id"]: p.get("name") or None for p in people}
        projects = self._read(self._client.project_list, ProjectListInput())["projects"]
        page_of = {p["t6_id"]: p["page_id"] for p in projects if p.get("t6_id")}
        labels = {p["page_id"]: p.get("project_code") or p["name"] or None for p in projects}
        records = []
        for o in self._read(self._client.jo_list, JoListInput())["job_orders"]:
            project = o.get("project_id")
            project = page_of.get(project, project)
            fields: dict[str, object] = {
                "Name": o.get("name") or "", "JO Number": o.get("number") or "", "Task": o.get("task") or "",
                "Status": o.get("status"), "Department": o.get("department"),
                "Blocker ": o.get("blocker") or "", "Health": o.get("health"),
                "Date": _instant(o.get("date"), False), "Deadline": _instant(o.get("deadline"), True),
                "Assigned": [{"id": i, "name": names.get(i)} for i in (to_page.get(a, a) for a in o.get("assigned_ids") or ())],
                "Projects": [{"id": project, "name": labels.get(project)}] if project else [],
                "last_edited_time": _notion_time(o.get("last_edited")),
            }
            records.append(JoRecord(o["page_id"], fields))
        return records


def _note_filter(filter: dict | None) -> dict:
    """The three filters the Minutes jobs send, as ``meeting-notes list`` inputs."""
    if filter and filter.get("property") == "Name" and "equals" in (filter.get("title") or {}):
        return {"title": filter["title"]["equals"]}
    if filter and filter.get("property") == "Name" and "starts_with" in (filter.get("title") or {}):
        return {"title_prefix": filter["title"]["starts_with"]}
    if filter and "and" in filter:
        return {"ready_to_send": True}
    raise ValueError("unsupported Meeting Notes filter")


def _notion_time(value: str | None) -> str | None:
    """An ISO time as Notion writes it ("2026-11-03T10:00:00.000Z"), so stored values do not change."""
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _date_field(value: str | None) -> dict | None:
    if not value:
        return None
    start = value if len(value) == 10 else datetime.fromisoformat(value).isoformat(timespec="milliseconds")
    return {"start": start, "end": None}


def _instant(value: str | None, as_deadline: bool) -> str | None:
    return manila_instant(_date_field(value)["start"], as_deadline) if value else None
