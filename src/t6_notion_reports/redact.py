"""Redact identities from free text before a consolidation call (spec #40 story 46, #60).

PM Notes and Minutes lines are free text, so they can name team members, quote a Telegram id or a
Notion link, or mention another project. Before the payload leaves the machine each such value is
replaced by a numbered placeholder ("[person 1]", "[project 1]", "[id 1]"). The mapping stays local:
the reply is validated against the redacted lines the LLM saw, and the placeholders are put back
afterwards so the brief reads normally.

What counts as a person: every Contacts name, its first name (three letters or more, and not a month
name), each alias, and each Telegram handle. What counts as a project, other than the one the payload
is about: the name of any project in the snapshots (four letters or more, ignoring case), and the code
of a project that passed the status gate (three characters or more, matching case, since codes such
as "T6" or "IF" are ordinary words too). Ids: Notion links and page ids, Telegram links, any @handle,
and runs of seven or more digits (Telegram ids, phone numbers). Matching is by whole word, longest first.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from .contacts import Contact
from .snapshots import squash

_MONTH_WORDS = {"jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
                "january", "february", "march", "april", "june", "july", "august", "september",
                "october", "november", "december"}
_IDS = re.compile(
    r"https?://\S*(?:notion\.(?:so|site|com)|t\.me)\S*"
    r"|\b(?:notion\.(?:so|site|com)|t\.me)/\S*"
    r"|\b[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}\b"
    r"|(?<![\w@])@[A-Za-z0-9_]{3,}"
    r"|(?<![\d.,])\d{7,}(?![\d.,])",
    re.I,
)
_PLACEHOLDER = re.compile(r"\[(?:person|project|id) \d+\]")


@dataclass
class Mapping:
    """Placeholder -> original text, for one payload."""

    originals: dict[str, str] = field(default_factory=dict)
    _by_value: dict[tuple[str, str], str] = field(default_factory=dict)

    def placeholder(self, kind: str, original: str) -> str:
        key = (kind, squash(original).casefold())
        if key not in self._by_value:
            count = sum(1 for k in self._by_value if k[0] == kind) + 1
            self._by_value[key] = f"[{kind} {count}]"
            self.originals[self._by_value[key]] = original
        return self._by_value[key]

    def restore(self, text: str) -> str:
        return _PLACEHOLDER.sub(lambda m: self.originals.get(m.group(0), m.group(0)), text)


@dataclass(frozen=True)
class Redactor:
    people: tuple[str, ...] = ()
    projects: tuple[tuple[str, str], ...] = ()  # (project page id, a name of it), matched ignoring case
    codes: tuple[tuple[str, str], ...] = ()  # (project page id, its code), matched case-sensitively

    @classmethod
    def build(
        cls, contacts: Iterable[Contact], states: dict[str, dict[str, object]], gated: Iterable[str] = (),
    ) -> Redactor:
        """``states``: every project in the snapshots; ``gated``: the page ids whose codes are redacted too."""
        people: set[str] = set()
        for c in contacts:
            name = squash(c.name or "")
            if name and name != c.page_id:
                people.add(name)
                first = name.split()[0]
                if len(first) >= 3 and first.casefold() not in _MONTH_WORDS and first != name:
                    people.add(first)
            people.update(a for a in (squash(x) for x in (c.aliases or "").split(",")) if len(a) >= 2)
            if c.handle:
                people.add(c.handle.lstrip("@"))
        names = {(p, v) for p, s in states.items() if len(v := squash(str(s.get("Name") or ""))) >= 4}
        gated = set(gated)
        codes = {(p, v) for p, s in states.items()
                 if p in gated and len(v := squash(str(s.get("Project Code") or ""))) >= 3}
        by_length = lambda pair: len(pair[1])  # noqa: E731
        return cls(tuple(sorted(people, key=len, reverse=True)), tuple(sorted(names, key=by_length, reverse=True)),
                   tuple(sorted(codes, key=by_length, reverse=True)))

    def _own(self, own_page_id: str) -> set[str]:
        return {v.casefold() for p, v in (*self.projects, *self.codes) if p == own_page_id}

    def redact(self, text: str, own_page_id: str, mapping: Mapping) -> str:
        """``text`` with every identity outside the project ``own_page_id`` replaced by a placeholder."""
        own = self._own(own_page_id)
        text = _IDS.sub(lambda m: mapping.placeholder("id", m.group(0)), text)
        for values, flags in ((self.projects, re.I), (self.codes, 0)):
            for page_id, value in values:
                if page_id != own_page_id and value.casefold() not in own:
                    text = _word(value, flags).sub(lambda m: mapping.placeholder("project", m.group(0)), text)
        for person in self.people:
            if person.casefold() not in own:
                text = _word(person).sub(lambda m: mapping.placeholder("person", m.group(0)), text)
        return text

    def names_in(self, text: str, own_page_id: str) -> str | None:
        """The first person or other-project name ``text`` holds, in any case: never in a redacted payload."""
        own = self._own(own_page_id)
        candidates = [(v, re.I) for v in self.people]
        candidates += [(v, re.I) for p, v in self.projects if p != own_page_id]
        candidates += [(v, 0) for p, v in self.codes if p != own_page_id]
        for value, flags in candidates:
            if value.casefold() not in own and _word(value, flags).search(text):
                return value
        return None


def _word(value: str, flags: int = re.I) -> re.Pattern:
    return re.compile(rf"(?<![\w\[]){re.escape(value)}(?![\w\]])", flags)
