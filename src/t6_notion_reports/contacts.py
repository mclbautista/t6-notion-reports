"""Contacts: who gets which message (spec sections 3.2 and 10)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Contact:
    page_id: str
    name: str
    handle: str | None = None  # without the leading @
    telegram_id: int | None = None
    aliases: str = ""  # raw comma-separated Contacts ``Aliases``; matched by the t6 API


def contact_by_id(contacts: dict[str, Contact], contact_id: str) -> Contact | None:
    """The Contact a configured page id names; a hyphen-less or upper-case id in the config still names it."""
    norm = lambda cid: cid.replace("-", "").lower()  # noqa: E731
    return next((c for cid, c in contacts.items() if norm(cid) == norm(contact_id)), None)
