"""Read Notion Job Orders (with Contacts and Projects names) and store one snapshot."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Protocol

from .jo_snapshots import JoRecord, JoSnapshotResult, manila_instant, take_snapshot


class Notion(Protocol):
    def query_data_source(self, data_source_id: str) -> list[dict]: ...


def run_jo_snapshot(
    conn: sqlite3.Connection,
    notion: Notion,
    jo_ds: str,
    contacts_ds: str,
    projects_ds: str,
    kind: str,
    now: datetime,
) -> JoSnapshotResult:
    contacts = {c["id"]: _value(c["properties"].get("Name")) for c in notion.query_data_source(contacts_ds)}
    projects = {p["id"]: _project_label(p) for p in notion.query_data_source(projects_ds)}
    records = [
        parse_job_order(page, contacts, projects)
        for page in notion.query_data_source(jo_ds)
        if not (page.get("in_trash") or page.get("archived"))
    ]
    return take_snapshot(conn, records, now, kind)


def _project_label(page: dict) -> str | None:
    """Code if there is one, else name."""
    props = page["properties"]
    return _value(props.get("Project Code")) or _value(props.get("Name"))


def parse_job_order(page: dict, contact_names: dict, project_labels: dict) -> JoRecord:
    props = page["properties"]
    fields: dict[str, object] = {
        name: _value(props.get(name))
        for name in ("Name", "JO Number", "Task", "Status", "Department", "Blocker ", "Health")
    }
    fields["Date"] = _date(props.get("Date"), as_deadline=False)
    fields["Deadline"] = _date(props.get("Deadline"), as_deadline=True)
    fields["Assigned"] = _relation(props.get("Assigned"), contact_names)
    fields["Projects"] = _relation(props.get("Projects"), project_labels)
    fields["last_edited_time"] = page.get("last_edited_time")
    return JoRecord(page["id"], fields)


def _relation(prop: dict | None, names: dict) -> list[dict]:
    return [{"id": r["id"], "name": names.get(r["id"])} for r in (prop or {}).get("relation", [])]


def _date(prop: dict | None, as_deadline: bool) -> str | None:
    d = (prop or {}).get("date")
    return manila_instant(d["start"], as_deadline, d.get("time_zone")) if d else None


def _value(prop: dict | None) -> object:
    if not prop:
        return None
    kind = prop["type"]
    if kind in ("title", "rich_text"):
        return "".join(t["plain_text"] for t in prop[kind])
    if kind in ("select", "status"):
        return (prop[kind] or {}).get("name")
    if kind == "formula":
        f = prop["formula"]
        return f.get(f["type"])
    raise ValueError(f"unsupported property type {kind!r}")
