"""A fake Notion that returns raw API-shaped pages. No network."""
from __future__ import annotations

import itertools

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


def project(page_id, name="Show", code="PAS", **props):
    base = {
        "Name": title(name), "Project Code": select(code), "Stage": select("Offline"),
        "Status": select("In-Progress"), "Health": select("On Track"), "PM Notes": rich(""),
        "Deadline (Playdate/Submission)": date(None),
        "T6 Post Coordinator": relation(), "Editors": relation(), "Online Editor": relation(),
        "Colorist": relation(), "Sound Tech": relation(), "Post Supe": relation(),
    }
    for f in FORMULAS:
        base[f] = formula("SECRET-FORMULA-VALUE")
    base.update(props)
    return {"id": page_id, "in_trash": False, "properties": base}


def status(name):
    return {"type": "status", "status": {"name": name} if name else None}


def job_order(page_id, name="TWBTY - AE - JO #48", edited="2026-11-03T10:00:00.000Z", **props):
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


class FakeNotion:
    def __init__(self, projects=(), contacts=(), meetings=(), description="", job_orders=()):
        self.job_orders = list(job_orders)
        self.projects = list(projects)
        self.contacts = list(contacts)
        self.meetings = list(meetings)
        self.description = description
        self.queried: list[str] = []
        self.created: list[dict] = []  # raw create_page bodies: {"properties", "children"}
        self.appended: list[tuple[str, list[dict]]] = []
        self.trashed: list[str] = []
        self.sent_marked: list[tuple[str, dict]] = []  # (page id, properties) from property updates
        self.page_blocks: dict[str, list[dict]] = {}  # page id -> top-level blocks (block() shapes)

    def query_data_source(self, data_source_id, filter=None):
        self.queried.append(data_source_id)
        pages = {PROJECTS: self.projects, CONTACTS: self.contacts, MEETING: self.meetings,
                 JOB_ORDERS: self.job_orders}[data_source_id]
        return [p for p in pages if not p.get("in_trash")]

    def retrieve_data_source(self, data_source_id):
        return {"id": data_source_id, "description": [{"plain_text": self.description}]}

    def create_page(self, data_source_id, properties, children):
        assert data_source_id == MEETING and len(children) <= 100
        page_id = f"new-{len(self.created) + 1}"
        self.created.append({"id": page_id, "properties": properties, "children": children})
        return {"id": page_id}

    def update_page(self, page_id, body):
        if "properties" in body:
            self.sent_marked.append((page_id, body["properties"]))
            for p in self.meetings:
                if p["id"] == page_id:
                    p["properties"].update(body["properties"])
            return {"id": page_id}
        self.trashed.append(page_id)
        for p in self.meetings:
            if p["id"] == page_id:
                p["in_trash"] = True
        return {"id": page_id}

    def list_block_children(self, block_id):
        """Children of a page (from ``page_blocks``) or of a block built with block(children=...)."""
        if block_id in self.page_blocks:
            return list(self.page_blocks[block_id])
        for blocks in self.page_blocks.values():
            found = _find(blocks, block_id)
            if found is not None:
                return list(found["_children"])
        raise KeyError(block_id)

    def append_children(self, block_id, children):
        assert len(children) <= 100
        self.appended.append((block_id, children))
        return {}


def _find(blocks, block_id):
    for b in blocks:
        if b["id"] == block_id:
            return b
        inner = _find(b["_children"], block_id)
        if inner is not None:
            return inner
    return None
