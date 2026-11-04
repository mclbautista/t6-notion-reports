"""Redact identities from free text before a consolidation call (spec #40 story 46, #60).

PM Notes and Minutes lines are free text, so they can name team members, quote a Telegram id or a
Notion link, or mention another project. Before the payload leaves the machine each such value is
replaced by a numbered placeholder ("[person 1]", "[project 1]", "[id 1]"). The mapping stays local:
the reply is validated against the redacted lines the LLM saw, and the placeholders are put back
afterwards so the brief reads normally.

What counts as a person: every Contacts name, its first name (three letters or more, and not a month
name; matched only when capitalised, since "Will" or "Joy" are words too), each alias, and each
Telegram handle. What counts as a project, other than the one the payload
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
        """One placeholder per distinct value and kind, numbered in order: "[person 1]", "[id 2]"."""
        key = (kind, squash(original).casefold())
        if key not in self._by_value:
            count = sum(1 for k in self._by_value if k[0] == kind) + 1
            self._by_value[key] = f"[{kind} {count}]"
            self.originals[self._by_value[key]] = original
        return self._by_value[key]

    def restore(self, text: str) -> str:
        """Put people and projects back. Ids stay hidden as "[id]": briefs carry no links or ids."""
        def back(m: re.Match) -> str:
            token = m.group(0)
            return "[id]" if token.startswith("[id") else self.originals.get(token, token)
        return _PLACEHOLDER.sub(back, text)


@dataclass(frozen=True)
class Redactor:
    """The people and project names to hide from free text, built once per brief run."""

    people: tuple[str, ...] = ()  # full names, aliases, handles: matched ignoring case
    projects: tuple[tuple[str, str], ...] = ()  # (project page id, a name of it), matched ignoring case
    codes: tuple[tuple[str, str], ...] = ()  # (project page id, its code), matched case-sensitively
    first_names: tuple[str, ...] = ()  # matched only as written ("Will", not "will"): they are often words too

    @classmethod
    def build(
        cls, contacts: Iterable[Contact], states: Iterable[dict[str, dict[str, object]]], gated: Iterable[str] = (),
    ) -> Redactor:
        """``states``: the snapshots, so an old name of a project is caught too; ``gated``: the page ids
        whose codes are redacted as well."""
        people: set[str] = set()
        firsts: set[str] = set()
        for c in contacts:
            name = squash(c.name or "")
            if name and name != c.page_id:
                people.add(name)
                first = name.split()[0]
                if len(first) >= 3 and first.casefold() not in _MONTH_WORDS and first != name:
                    firsts.add(first)
            people.update(a for a in (squash(x) for x in (c.aliases or "").split(",")) if len(a) >= 2)
            if c.handle:
                people.add(c.handle.lstrip("@"))
        names = {(p, v) for state in states for p, s in state.items() if len(v := squash(str(s.get("Name") or ""))) >= 4}
        gated = set(gated)
        codes = {(p, v) for state in states for p, s in state.items()
                 if p in gated and len(v := squash(str(s.get("Project Code") or ""))) >= 3}
        def longest_first(pairs: set[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
            return tuple(sorted(pairs, key=lambda pair: len(pair[1]), reverse=True))
        return cls(tuple(sorted(people, key=len, reverse=True)), longest_first(names), longest_first(codes),
                   tuple(sorted(firsts - people, key=len, reverse=True)))

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
        for values, flags in ((self.people, re.I), (self.first_names, 0)):
            for person in values:
                if person.casefold() not in own:
                    text = _word(person, flags).sub(lambda m: mapping.placeholder("person", m.group(0)), text)
        return text

    def names_in(self, text: str, own_page_id: str) -> str | None:
        """The first person or other-project name ``text`` holds, in any case: never in a redacted payload."""
        own = self._own(own_page_id)
        candidates = [(v, re.I) for v in self.people] + [(v, 0) for v in self.first_names]
        candidates += [(v, re.I) for p, v in self.projects if p != own_page_id]
        candidates += [(v, 0) for p, v in self.codes if p != own_page_id]
        for value, flags in candidates:
            if value.casefold() not in own and _word(value, flags).search(text):
                return value
        return None


def _word(value: str, flags: int = re.I) -> re.Pattern:
    return re.compile(rf"(?<![\w\[]){re.escape(value)}(?![\w\]])", flags)
