"""Hide identities in free text before an LLM call, with t6-core's pseudonymizer (spec #40 story 46, #60).

PM Notes and Minutes lines are free text, so they can name team members, quote a Telegram id or a
Notion link, or mention another project. Each such value is replaced by a numbered placeholder
("[person 1]", "[project 1]", "[id 1]") before the text leaves the machine. The ``strict`` preset is
the rule this app has always used: every Contacts name, each word of it (three letters or more, and
not a month name), each alias and each Telegram handle; the name of any project in the snapshots
(four letters or more and not an ordinary word) and its code (two characters or more); known page
ids and Telegram ids; any Notion or Telegram link, @handle and run of seven or more digits. An
ordinary word that is also a name or code ("Will", "TWO") is hidden only when capitalised. Restored
ids read "[id]", so briefs carry no links or ids.

A ``Masker`` holds the names for one run. ``session(page_id)`` starts the placeholder map for one
payload and leaves the project ``page_id`` itself readable, since a payload is about that project.
The LLM call restores the names (``llm.py``), so nothing here restores.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from t6.privacy import Person, Project, Registry, Session, Unresolved

from .contacts import Contact
from .snapshots import squash


@dataclass(frozen=True)
class Masker:
    """The people and project names to hide from free text, built once per run."""

    registry: Registry = Registry()
    own: tuple[tuple[str, tuple[str, ...]], ...] = ()  # project page id -> its names and codes, left readable

    @classmethod
    def build(cls, contacts: Iterable[Contact], states: Iterable[dict[str, dict[str, object]]]) -> Masker:
        """``states``: the snapshots, so an old name of a project is hidden too."""
        people: list[Person] = []
        ids: dict[str, None] = {}
        for c in contacts:
            ids[c.page_id] = None
            name = squash(c.name or "")
            aliases = tuple(a for a in (squash(x) for x in (c.aliases or "").split(",")) if a)
            if c.telegram_id is not None:
                ids[str(c.telegram_id)] = None
            if name and name != c.page_id:
                people.append(Person(name, aliases, (c.handle,) if c.handle else ()))
        states = list(states)
        projects: dict[tuple[str, str | None], None] = {}
        own: dict[str, dict[str, None]] = {}
        for state in states:
            for page_id, fields in state.items():
                ids[page_id] = None
                name = squash(str(fields.get("Name") or ""))
                code = squash(str(fields.get("Project Code") or "")) or None
                if name or code:
                    projects[(name, code)] = None
                    own.setdefault(page_id, {}).update({v: None for v in (name, code) if v})
        return cls(
            Registry(tuple(people), tuple(Project(n, c) for n, c in projects), tuple(ids)),
            tuple((page_id, tuple(values)) for page_id, values in own.items()),
        )

    def session(self, page_id: str = "") -> Session:
        """A new placeholder map. The project ``page_id`` is the payload's subject, so its names stay readable."""
        allow = dict(self.own).get(page_id, ())
        return Session(self.registry, allow=allow, options="strict")


def mask_and_read_back(session: Session, text: str) -> tuple[str, str]:
    """``text`` masked as sent, and as it reads once the reply's placeholders are restored.

    A restored reply shows ids and links as "[id]" and a name in the casing first seen, so a quote of
    this text comes back in the second form: that is the form to compare quotes and facts against.
    Text that already looks like a placeholder ("project 2") cannot be restored; then both are the
    masked text, which such a quote can never match.
    """
    masked = session.mask(text).text
    try:
        return masked, session.restore(masked)
    except Unresolved:
        return masked, masked
