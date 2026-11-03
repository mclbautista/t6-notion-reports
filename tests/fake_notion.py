"""A fake Notion that returns raw API-shaped pages. No network."""
from __future__ import annotations

PROJECTS = "projects-ds"
CONTACTS = "contacts-ds"

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


class FakeNotion:
    def __init__(self, projects=(), contacts=()):
        self.projects = list(projects)
        self.contacts = list(contacts)
        self.queried: list[str] = []

    def query_data_source(self, data_source_id):
        self.queried.append(data_source_id)
        return list({PROJECTS: self.projects, CONTACTS: self.contacts}[data_source_id])
