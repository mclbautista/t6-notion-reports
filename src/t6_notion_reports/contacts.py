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
