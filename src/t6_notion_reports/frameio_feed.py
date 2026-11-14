"""Frame.io share-link activity for the briefing, read through t6-core's Frame.io worker client.

The brief holds no Frame.io token: the worker answers ``reader.briefing(since)`` with, per Frame.io
project, the live share links that saw activity after ``since`` and the Projects pages they relate to.
A failed or signed-out worker costs the brief a note, never the brief itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from t6.frameio import Failed, SignInNeeded

from .schedule import MANILA

# "Frame" + ".io": ``t6 check-consumer`` reads the bare product name in a string as a Frame.io host.
NAME = "Frame" + ".io"
UNAVAILABLE_NOTE = f"⚠️ {NAME} unavailable: no review activity this run"
SIGN_IN_NOTE = f"⚠️ {NAME} needs a sign-in: no review activity this run"
UNLINKED_NOTE = f"{NAME} shows with no linked project: "


@dataclass(frozen=True)
class FrameioFeed:
    lines: dict[str, tuple[str, ...]]  # project page id -> one line per active share link
    notes: tuple[str, ...] = ()  # warnings for every digest recipient
    cyril_notes: tuple[str, ...] = ()  # shows no Projects page is linked to: Cyril's digest only


def _key(page_id: str) -> str:
    return page_id.replace("-", "").lower()


def frameio_activity(reader, since: datetime, states: dict[str, dict[str, object]], log: Callable[[str], None]) -> FrameioFeed:
    """Active share links with activity after ``since``, by project page id (ids as in ``states``)."""
    shows = reader.briefing(since)
    if isinstance(shows, SignInNeeded):
        log("brief frameio sign-in needed")
        return FrameioFeed({}, notes=(SIGN_IN_NOTE,))
    if isinstance(shows, Failed):
        log("brief frameio unavailable")  # the reason can hold text from the worker: never logged
        return FrameioFeed({}, notes=(UNAVAILABLE_NOTE,))
    by_key = {_key(p): p for p in states}
    lines: dict[str, list[str]] = {}
    unlinked: list[str] = []
    for show in shows:
        active = [link for link in show.links if link.active]
        if not active:
            continue
        pages = [by_key[_key(p)] for p in show.project_page_ids if _key(p) in by_key]
        if not pages:
            unlinked.append(show.frameio_project_name)
            continue
        for link in sorted(active, key=lambda l: l.name.lower()):
            day = link.last_activity.astimezone(MANILA).strftime("%b ") + str(link.last_activity.astimezone(MANILA).day)
            for page in pages:
                lines.setdefault(page, []).append(f"{NAME} · {link.name} · activity {day}")
    cyril = (UNLINKED_NOTE + ", ".join(sorted(unlinked)),) if unlinked else ()
    return FrameioFeed({p: tuple(v) for p, v in lines.items()}, cyril_notes=cyril)
