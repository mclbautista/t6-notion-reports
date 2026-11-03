"""A fake Notion that returns raw API-shaped pages. No network."""
from __future__ import annotations

PROJECTS = "projects-ds"
CONTACTS = "contacts-ds"
MEETING = "meeting-ds"

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


def contact(page_id, name):
    return {"id": page_id, "properties": {"Name": title(name)}}


def people(*users):
    """users: (id, name)"""
    return {"type": "people", "people": [{"id": i, "name": n} for i, n in users]}


def meeting_page(page_id, name, day, attendees=(), sent_at=None, handle_props=None):
    return {"id": page_id, "properties": {
        "Name": title(name), "Meeting Date": date(day), "Team Attendees": people(*attendees),
        "Sent at": date(sent_at)}}


class FakeNotion:
    def __init__(self, projects=(), contacts=(), meetings=(), description=""):
        self.projects = list(projects)
        self.contacts = list(contacts)
        self.meetings = list(meetings)
        self.description = description
        self.queried: list[str] = []
        self.created: list[dict] = []  # raw create_page bodies: {"properties", "children"}
        self.appended: list[tuple[str, list[dict]]] = []
        self.trashed: list[str] = []

    def query_data_source(self, data_source_id, filter=None):
        self.queried.append(data_source_id)
        pages = {PROJECTS: self.projects, CONTACTS: self.contacts, MEETING: self.meetings}[data_source_id]
        return [p for p in pages if not p.get("in_trash")]

    def retrieve_data_source(self, data_source_id):
        return {"id": data_source_id, "description": [{"plain_text": self.description}]}

    def create_page(self, data_source_id, properties, children):
        assert data_source_id == MEETING and len(children) <= 100
        page_id = f"new-{len(self.created) + 1}"
        self.created.append({"id": page_id, "properties": properties, "children": children})
        return {"id": page_id}

    def update_page(self, page_id, body):
        self.trashed.append(page_id)
        for p in self.meetings:
            if p["id"] == page_id:
                p["in_trash"] = True
        return {"id": page_id}

    def append_children(self, block_id, children):
        assert len(children) <= 100
        self.appended.append((block_id, children))
        return {}
