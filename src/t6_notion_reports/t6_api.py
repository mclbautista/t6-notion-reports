"""Every read of Notion data goes through the t6 API (ADR 0002 in t6-core), with ``t6.client.Client``.

``ApiNotion`` is what the jobs read from: Contacts, the Projects and Job Orders snapshot records, the
Projects database notices and Meeting Notes. It builds each record in the shape the snapshots store, so
the stored data, the diffs and the reports are the same as when Notion was read directly. Relations are
stored as Contacts page ids (a T6 Person ID from the API is mapped back through ``person list``).

The Minutes writes are not here: ``minutes_api`` makes them with ``ApiNotion.client``.

An API that cannot be reached, or refuses, is an ``ApiUnavailable``, an ``OSError`` like the transport
errors Notion gave, so each job treats it as the outage it handles.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping

from t6.client import APIError, Client, Unavailable
from t6.verbs import (
    Done, JoListInput, MeetingNotesListInput, PersonFindInput, PersonListInput, ProjectFlagsInput, ProjectFlagsItem, ProjectGetInput,
    ProjectListInput, Refused, StudioNoticesInput,
)

from .contacts import Contact
from .holidays import Run
from .jo_snapshots import JoRecord, manila_instant
from .rules import JoRules, ProjectRules
from .snapshots import LAST_EDITED, RELATION_FIELDS, ProjectRecord
from .telegram_html import Span

# The full-list reads are slow: ``jo list`` reads every page body and ``meeting-notes list`` every
# note body. The client's own 10 s limit is too short for them.
READ_TIMEOUT = 120

# ``person find`` takes at most this many names in one call.
FIND_LIMIT = 50

# Project field -> the ``project list`` field that holds its related Contacts.
_PROJECT_RELATIONS = {
    "T6 Post Coordinator": "post_coordinator_ids", "Editors": "editor_ids", "Online Editor": "online_editor_ids",
    "Colorist": "colorist_ids", "Sound Tech": "sound_tech_ids", "Assistant Editor": "assistant_editor_ids",
    "Director": "director_ids", "Cinematographer": "cinematographer_ids", "Prod Coor/Supe": "prod_coor_supe_ids",
}
assert set(_PROJECT_RELATIONS) == set(RELATION_FIELDS)

# The Project attribute names the rule results use -> the Notion property names the snapshots use.
PROPERTY_OF = {
    "playdate": "Playdate", "submission_deadline": "Submission Deadline", "archival_deadline": "Archival Deadline",
    **{key: name for name, key in _PROJECT_RELATIONS.items()},
}
ATTRIBUTE_OF = {name: attr for attr, name in PROPERTY_OF.items()}


class ApiUnavailable(OSError):
    """The t6 API did not answer or refused. The message names the kind of failure, never the data."""


@dataclass(frozen=True)
class Notices:
    """The Projects database description: its plain text and its runs (text and a bold flag each)."""

    description: str
    runs: tuple[Run, ...]


@dataclass(frozen=True)
class Person:
    """The Contact ``person find`` matched. Snapshot relations hold ``page_id``, or ``t6_id`` where the API gave one."""

    page_id: str
    t6_id: str | None
    name: str


@dataclass(frozen=True)
class Block:
    """One block of a Meeting Notes body. ``kind`` is the block type ("bulleted_list_item", "toggle", ...)."""

    kind: str
    spans: tuple[Span, ...] = ()
    children: tuple["Block", ...] = ()
    checked: bool = False

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)


@dataclass(frozen=True)
class Note:
    """A Meeting Notes page. Times are as the Minutes code stores and shows them."""

    page_id: str
    title: str
    meeting_day: date | None
    sent_at: str | None
    ready_to_send: bool
    last_edited: str | None  # "2026-11-03T10:00:00.000Z"
    attendees: tuple[tuple[str, str], ...]  # (Notion user id, name)
    body: tuple[Block, ...]


def client_from_env(env: Mapping[str, str] | None = None, timeout: float = READ_TIMEOUT) -> Client | None:
    """A client for ``T6_API_URL`` and ``T6_API_SECRET``; None when the URL is unset. The reads use the
    long default, ``llm run`` its own limit (``llm_api.LLM_TIMEOUT``)."""
    env = os.environ if env is None else env
    if not env.get("T6_API_URL", "").strip():
        return None
    return Client(env["T6_API_URL"].strip(), env.get("T6_API_SECRET", ""), timeout=timeout)


class ApiNotion:
    def __init__(self, client):
        self.client = client

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
        return self._read(self.client.person_list, PersonListInput())["people"]

    def read_contacts(self) -> dict[str, Contact]:
        """Contacts by Notion page id."""
        out: dict[str, Contact] = {}
        for p in self._people():
            handle = (p.get("telegram_handle") or "").lstrip("@").strip() or None
            out[p["page_id"]] = Contact(
                p["page_id"], p.get("name") or p["page_id"], handle,
                p.get("telegram_user_id"), ", ".join(p.get("aliases") or ()),
            )
        return out

    def find_people(self, names: list[str]) -> list[Person | None]:
        """One ``person find`` call for the names (``mentions``, Telegram-onboarded Contacts first); the Person each
        names, in order, or None when the API has no match or marks it ``needs_review``. A tie is None, never a guess."""
        found: list[Person | None] = []
        for i in range(0, len(names), FIND_LIMIT):
            inputs = PersonFindInput(mentions=names[i:i + FIND_LIMIT], prefer_telegram=True)
            for r in self._read(self.client.person_find, inputs)["results"]:
                m = r.get("match")
                found.append(Person(m["page_id"], m.get("t6_id"), m["name"]) if m and not r.get("needs_review") else None)
        return found

    def t6_person_ids(self) -> dict[str, str]:
        """Contacts page id -> T6 Person ID, for the Contacts the API has bound to a Person."""
        return {p["page_id"]: p["t6_id"] for p in self._people() if p.get("t6_id")}

    def contact_names(self) -> dict[str, str | None]:
        return {p["page_id"]: p.get("name") for p in self._people()}

    # ---- the Projects database notices ----
    def notices(self) -> Notices:
        notices = self._read(self.client.studio_notices, StudioNoticesInput())
        text = notices.get("description") or ""
        runs = tuple(Run(r["text"], bool(r.get("bold"))) for r in notices.get("runs") or ())
        return Notices(text, runs or ((Run(text, False),) if text else ()))

    # ---- Meeting Notes ----
    def meeting_notes(self, **filters) -> list[Note]:
        """Filters are ``meeting-notes list`` inputs: ``title``, ``title_prefix``, ``ready_to_send``, ``include_body``."""
        notes = self._read(self.client.meeting_notes_list, MeetingNotesListInput(**filters))["meeting_notes"]
        return [_note(n) for n in notes]

    # ---- Projects and Job Orders snapshot records ----
    def project_records(self) -> list[ProjectRecord]:
        people = self._people()
        to_page = {p["t6_id"]: p["page_id"] for p in people if p.get("t6_id")}
        names = {p["page_id"]: p.get("name") for p in people}

        def related(ids):
            pages = [to_page.get(i, i) for i in ids or ()]
            return [{"id": i, "name": names.get(i)} for i in pages]

        records = []
        for p in self._read(self.client.project_list, ProjectListInput())["projects"]:
            fields: dict[str, object] = {
                "Name": p["name"], "Project Code": p.get("notion_project_code"), "Stage": p.get("stage"),
                "Status": p.get("status"), "Project Health": p.get("project_health"),
                "PM Notes": p.get("pm_notes") or "",
                "Playdate": _date_field(p.get("playdate"), p.get("playdate_end")),
                "Submission Deadline": _date_field(p.get("submission_deadline"), p.get("submission_deadline_end")),
                "Archival Deadline": _date_field(p.get("archival_deadline"), p.get("archival_deadline_end")),
                LAST_EDITED: _notion_time(p.get("last_edited")),
            }
            for name, key in _PROJECT_RELATIONS.items():
                fields[name] = related(p.get(key))
            records.append(ProjectRecord(p["page_id"], fields, _project_rules(p)))
        return records

    def project_rules(self) -> dict[str, ProjectRules]:
        """The rule results of every Project, by page id: one ``project list`` call."""
        return {p["page_id"]: _project_rules(p) for p in self._read(self.client.project_list, ProjectListInput())["projects"]}

    def project_rule(self, page_id: str) -> ProjectRules | None:
        """The rule results of one Project, or None when the API does not know the page."""
        try:
            outcome = self.client.project_get(ProjectGetInput(project=page_id))
        except APIError as exc:
            raise ApiUnavailable(f"t6 API error status={exc.status}") from None
        if isinstance(outcome, Unavailable):
            raise ApiUnavailable("t6 API unreachable")
        if isinstance(outcome, Refused):
            return None
        if not isinstance(outcome, Done):
            raise ApiUnavailable(f"t6 API answered {outcome.outcome}")
        return _project_rules(outcome.result["project"])

    def project_flags(
        self, stated: Mapping[str, Mapping[str, date | None]], today: date, lead_warning_days: int,
    ) -> dict[str, dict[str, Any] | None]:
        """``project flags`` for several Projects in one call, judged on ``today``. ``stated`` maps a page id to the
        dates the PM Notes give for its blank date properties (property name to day, None: a fuzzy value). Returns the
        result of each page id, None for one the API does not know."""
        inputs = ProjectFlagsInput(
            projects=[
                ProjectFlagsItem(project=page_id, stated={ATTRIBUTE_OF[name]: day for name, day in dates.items()})
                for page_id, dates in stated.items()
            ],
            as_of=today, lead_warning_days=lead_warning_days,
        )
        entries = self._read(self.client.project_flags, inputs)["projects"]
        return {e["project"]: (e if e["found"] else None) for e in entries}

    def job_order_records(self) -> list[JoRecord]:
        people = self._people()
        to_page = {p["t6_id"]: p["page_id"] for p in people if p.get("t6_id")}
        names = {p["page_id"]: p.get("name") for p in people}
        projects = self._read(self.client.project_list, ProjectListInput())["projects"]
        page_of = {p["t6_id"]: p["page_id"] for p in projects if p.get("t6_id")}
        labels = {p["page_id"]: p.get("notion_project_code") or p["name"] or None for p in projects}
        records = []
        for o in self._read(self.client.jo_list, JoListInput())["job_orders"]:
            project_pages = [page_of.get(i, i) for i in o.get("project_ids") or ()]
            fields: dict[str, object] = {
                "Name": o.get("name") or "", "JO Number": o.get("number") or "", "Task": o.get("task") or "",
                "Status": o.get("status"), "Department": o.get("department"),
                "Blocker ": o.get("blocker") or "", "Health": o.get("health"),
                "Date": _instant(o.get("date"), False), "Deadline": _instant(o.get("deadline"), True),
                "Assigned": [{"id": i, "name": names.get(i)} for i in (to_page.get(a, a) for a in o.get("assigned_ids") or ())],
                "Projects": [{"id": i, "name": labels.get(i)} for i in project_pages],
                "last_edited_time": _notion_time(o.get("last_edited")),
            }
            records.append(JoRecord(o["page_id"], fields, JoRules(bool(o["active"]), tuple(o.get("health_flags") or ()))))
        return records


def _project_rules(p: dict) -> ProjectRules:
    return ProjectRules(
        p.get("t6_id"), p["status_key"], p.get("status_group"), bool(p["snapshotted"]),
        tuple(PROPERTY_OF[a] for a in p.get("missing_roles") or ()),
        tuple(PROPERTY_OF[a] for a in p.get("relevant_dates") or ()),
        bool(p["health_missing"]), int(p["health_rank"]),
    )


def _note(n: dict) -> Note:
    day = n.get("meeting_date")
    return Note(
        n["page_id"], n.get("name") or "", date.fromisoformat(day[:10]) if day else None, n.get("sent_at") or None,
        bool(n.get("ready_to_send")), _notion_time(n.get("last_edited")),
        tuple(zip(n.get("attendee_ids") or (), n.get("attendee_names") or ())),
        tuple(_block(b) for b in n.get("body") or ()),
    )


def _block(b: dict) -> Block:
    spans = tuple(Span(s["text"], bool(s.get("bold")), bool(s.get("code")), s.get("url"))
                  for s in b.get("spans") or () if s["text"])
    return Block(b["kind"], spans, tuple(_block(c) for c in b.get("children") or ()), bool(b.get("checked")))


def _notion_time(value: str | None) -> str | None:
    """An ISO time as Notion writes it ("2026-11-03T10:00:00.000Z"), so stored values do not change."""
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _day(value: str) -> str:
    """A date as Notion writes it: a day as is, a time with milliseconds."""
    return value if len(value) == 10 else datetime.fromisoformat(value).isoformat(timespec="milliseconds")


def _date_field(start: str | None, end: str | None) -> dict | None:
    if not start:
        return None
    return {"start": _day(start), "end": _day(end) if end else None}


def _instant(value: str | None, as_deadline: bool) -> str | None:
    return manila_instant(_day(value), as_deadline) if value else None
