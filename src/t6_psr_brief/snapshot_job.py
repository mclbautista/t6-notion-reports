"""Read Notion Projects and Contacts and store one snapshot."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Protocol

from .snapshots import RELATION_FIELDS, ProjectRecord, SnapshotResult, take_snapshot

# Never snapshotted (spec section 4). Matched on the word, ignoring emoji and whitespace.
EXCLUDED_STATUSES = {"archived", "lost"}


def _is_excluded(status: object) -> bool:
    return re.sub(r"[^\w\s]", "", str(status or "")).strip().lower() in EXCLUDED_STATUSES

# Only these Notion properties are read. Formula fields are never touched.
NOTION_NAMES = {
    "Name": "Name", "Project Code": "Project Code", "Stage": "Stage", "Status": "Status",
    "Health": "Health", "PM Notes": "PM Notes", "Deadline (Playdate/Submission)": "Deadline",
    **{f: f for f in RELATION_FIELDS},
}


class Notion(Protocol):
    def query_data_source(self, data_source_id: str) -> list[dict]: ...


def run_snapshot(
    conn: sqlite3.Connection,
    notion: Notion,
    projects_ds: str,
    contacts_ds: str,
    kind: str,
    now: datetime,
) -> SnapshotResult:
    names = {c["id"]: _text(c["properties"].get("Name")) for c in notion.query_data_source(contacts_ds)}
    records = []
    for page in notion.query_data_source(projects_ds):
        if page.get("in_trash") or page.get("archived"):
            continue
        rec = parse_project(page, names)
        if _is_excluded(rec.fields.get("Status")):
            continue
        records.append(rec)
    return take_snapshot(conn, records, now, kind)


def parse_project(page: dict, contact_names: dict[str, str | None]) -> ProjectRecord:
    props = page["properties"]
    fields: dict[str, object] = {}
    for notion_name, stored in NOTION_NAMES.items():
        prop = props.get(notion_name)
        if stored in RELATION_FIELDS:
            ids = [r["id"] for r in (prop or {}).get("relation", [])]
            fields[stored] = [{"id": i, "name": contact_names.get(i)} for i in ids]
        else:
            fields[stored] = _value(prop)
    return ProjectRecord(page["id"], fields)


def _value(prop: dict | None) -> object:
    if not prop:
        return None
    kind = prop["type"]
    if kind in ("title", "rich_text"):
        return "".join(t["plain_text"] for t in prop[kind])
    if kind in ("select", "status"):
        return (prop[kind] or {}).get("name")
    if kind == "date":
        d = prop["date"]
        return {"start": d["start"], "end": d.get("end")} if d else None
    raise ValueError(f"unsupported property type {kind!r}")


def _text(prop: dict | None) -> str | None:
    return _value(prop) if prop else None  # type: ignore[return-value]
