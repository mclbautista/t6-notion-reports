"""Contacts: who gets which message (spec sections 3.2 and 10)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Contact:
    page_id: str
    name: str
    handle: str | None = None  # without the leading @
    telegram_id: int | None = None


class Notion(Protocol):
    def query_data_source(self, data_source_id: str) -> list[dict]: ...


def load_contacts(notion: Notion, contacts_ds: str) -> dict[str, Contact]:
    out: dict[str, Contact] = {}
    for page in notion.query_data_source(contacts_ds):
        props = page["properties"]
        handle = (_plain(props.get("TelegramHandle")) or "").lstrip("@").strip() or None
        out[page["id"]] = Contact(
            page["id"], _plain(props.get("Name")) or page["id"], handle,
            _int(_plain(props.get("TelegramUserID"))),
        )
    return out


def _plain(prop: dict | None) -> str | None:
    """Text of a title, rich text, number or select property; None when empty or another type."""
    if not prop:
        return None
    kind = prop.get("type")
    if kind in ("title", "rich_text"):
        return "".join(t.get("plain_text", "") for t in prop[kind]).strip() or None
    if kind == "number":
        number = prop.get("number")
        return None if number is None else str(int(number))
    if kind == "select":
        return (prop.get("select") or {}).get("name")
    return None


def _int(text: str | None) -> int | None:
    try:
        return int(text) if text else None
    except ValueError:
        return None
