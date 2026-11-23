"""A fake Notion that returns raw API-shaped pages. No network."""
from __future__ import annotations

import itertools

from fake_api import FakeApi
from t6_notion_reports.t6_api import ApiNotion

_ids = itertools.count(1)

PROJECTS = "projects-ds"
CONTACTS = "contacts-ds"
MEETING = "meeting-ds"
JOB_ORDERS = "jo-ds"

# Formula property names. The snapshot must never read them.
FORMULAS = ("Coordinator", "Label A", "Deadline Display", "Separator 1")


def title(text):
    return {"type": "title", "title": [{"plain_text": text}]}


def rich(text):
    return {"type": "rich_text", "rich_text": [{"plain_text": text}] if text else []}


def select(name):
    return {"type": "select", "select": {"name": name} if name else None}


def date(start, end=None):
    return {"type": "date", "date": {"start": start, "end": end} if start else None}


def relation(*ids):
    return {"type": "relation", "relation": [{"id": i} for i in ids]}


def formula(value):
    return {"type": "formula", "formula": {"type": "string", "string": value}}


def project(page_id, name="Show", code="QAZ", **props):
    base = {
        "Name": title(name), "Project Code": select(code), "Stage": select("Offline"),
        "Status": select("🚨 In-Progress"), "Project Health": select("On Track"), "PM Notes": rich(""),
        "Playdate": date(None), "Submission Deadline": date(None), "Archival Deadline": date(None),
        "T6 Post Coordinator": relation(), "Editors": relation(), "Online Editor": relation(),
        "Colorist": relation(), "Sound Tech": relation(), "Prod Coor/Supe": relation(),
        "Assistant Editor": relation(), "Director": relation(), "Cinematographer": relation(),
    }
    for f in FORMULAS:
        base[f] = formula("SECRET-FORMULA-VALUE")
    base.update(props)
    return {"id": page_id, "in_trash": False, "properties": base}


def status(name):
    return {"type": "status", "status": {"name": name} if name else None}


def job_order(page_id, name="FAKEX - AE - JO #48", edited="2026-11-03T10:00:00.000Z", **props):
    """A Job Orders page as the API returns it. Note the trailing space in "Blocker "."""
    base = {
        "Name": title(name), "JO Number": rich("48"), "Task": rich("Export"),
        "Date": date("2026-11-03T09:00:00.000+08:00"), "Deadline": date("2026-11-03T18:00:00.000+08:00"),
        "Status": status("Pending"), "Department": select("AE"),
        "Assigned": relation(), "Projects": relation(), "Blocker ": rich(""),
        "Health": formula("🔵 On Track"),
    }
    base.update(props)
    return {"id": page_id, "in_trash": False, "last_edited_time": edited, "properties": base}


def contact(page_id, name):
    return {"id": page_id, "properties": {"Name": title(name)}}


def people(*users):
    """users: (id, name)"""
    return {"type": "people", "people": [{"id": i, "name": n} for i, n in users]}


def meeting_page(page_id, name, day, attendees=(), sent_at=None, handle_props=None, ready=False):
    return {"id": page_id, "properties": {
        "Name": title(name), "Meeting Date": date(day), "Team Attendees": people(*attendees),
        "Sent at": date(sent_at), "Ready to send": {"type": "checkbox", "checkbox": ready}}}


def rt(text, bold=False, code=False, href=None):
    """A rich-text item as the API returns it (plain_text plus annotations)."""
    return {"type": "text", "plain_text": text, "href": href,
            "annotations": {"bold": bold, "code": code}}


def block(kind, text="", children=(), **rich_kw):
    """A block as the API returns it. ``text`` may be a string or a list of rt() items."""
    items = text if isinstance(text, list) else ([rt(text, **rich_kw)] if text else [])
    return {"object": "block", "id": f"b-{next(_ids)}", "type": kind, "has_children": bool(children),
            "_children": list(children), kind: {"rich_text": items}}


def _page(page_id, state):
    """A Projects page as the API reads it now, built from a snapshot state."""
    page = project(page_id, state.get("Name") or "", state.get("Project Code"))
    props = page["properties"]
    props.update({"Stage": select(state.get("Stage")), "Status": select(state.get("Status")),
                  "Project Health": select(state.get("Project Health")), "PM Notes": rich(state.get("PM Notes"))})
    for name in ("Playdate", "Submission Deadline", "Archival Deadline"):
        value = state.get(name)
        props[name] = date(value["start"], value.get("end")) if value else date(None)
    for name in ("T6 Post Coordinator", "Editors", "Online Editor", "Colorist", "Sound Tech", "Prod Coor/Supe",
                 "Assistant Editor", "Director", "Cinematographer"):
        props[name] = relation(*[r["id"] for r in state.get(name) or []])
    page["last_edited_time"] = state.get("Last Edited")
    return page


class FakeNotion:
    def __init__(self, projects=(), contacts=(), meetings=(), description="", job_orders=(), concerns=None):
        self.concerns = concerns  # a FakeConcerns shared across runs; None: a new, empty store
        self.job_orders = list(job_orders)
        self.projects = list(projects)
        self.contacts = list(contacts)
        self.meetings = list(meetings)
        self.description = description
        self.page_blocks: dict[str, list[dict]] = {}  # page id -> top-level blocks (block() shapes)

    @classmethod
    def live(cls, conn, contacts=(), **kw):
        """A fake whose Projects are the latest snapshot of ``conn``: the API as it reads when a brief runs."""
        from t6_notion_reports.snapshots import latest_snapshot_ids, load_snapshot
        ids = latest_snapshot_ids(conn, 1)
        state = load_snapshot(conn, ids[0]) if ids else {}
        return cls([_page(p, s) for p, s in state.items()], contacts, **kw)

    # The reads: what the jobs call on ``ApiNotion``, served by ``FakeApi`` from the fixtures. Set ``down`` to
    # true to make the API unreachable.
    @property
    def api(self):
        if not hasattr(self, "_api"):
            self._api = FakeApi(self)
        return self._api

    @property
    def client(self):
        return self.api

    def _reads(self):
        return ApiNotion(self.api)

    def read_contacts(self):
        return self._reads().read_contacts()

    def t6_person_ids(self):
        return self._reads().t6_person_ids()

    def master_brief_pages(self, log=lambda _: None):
        return self._reads().master_brief_pages(log)

    def contact_names(self):
        return self._reads().contact_names()

    def find_people(self, names):
        return self._reads().find_people(names)

    def notices(self):
        return self._reads().notices()

    def meeting_notes(self, **filters):
        return self._reads().meeting_notes(**filters)

    def project_records(self):
        return self._reads().project_records()

    def job_order_records(self):
        return self._reads().job_order_records()

    def project_rules(self):
        return self._reads().project_rules()

    def project_rule(self, page_id):
        return self._reads().project_rule(page_id)

    def project_flags(self, *args):
        return self._reads().project_flags(*args)
