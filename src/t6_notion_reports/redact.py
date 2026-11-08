"""Redact identities from free text before a consolidation call (spec #40 story 46, #60).

PM Notes and Minutes lines are free text, so they can name team members, quote a Telegram id or a
Notion link, or mention another project. Before the payload leaves the machine each such value is
replaced by a numbered placeholder ("[person 1]", "[project 1]", "[id 1]"). The mapping stays local:
the reply is validated against the redacted lines the LLM saw, and the placeholders are put back
afterwards so the brief reads normally.

What counts as a person: every Contacts name, each word of it (three letters or more, and not a month
name), each alias, and each Telegram handle. What counts as a project, other than the one the payload
is about: the name of any project in the snapshots (four letters or more, ignoring case), and the code
of any project in them (two characters or more). Ids: Notion links and page ids, Telegram links
(t.me and tg://), any @handle, runs of seven or more digits (Telegram ids, phone numbers) even when a
full stop or comma follows, and every known TelegramUserID wherever it stands as a number.
Matching is by whole word, longest first.

Exposure boundary (#62): a name word or a project code that is not an ordinary word is hidden in any
case ("dario", "DARIO", "ab"). One that is an ordinary word ("Will", "Joy", a code such as "TWO") is hidden
only as written capitalised or in capitals, since hiding "will" or "two" everywhere would blank the notes.
The ordinary words are the lists in ``condense`` plus ``EXTRA_ORDINARY``.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from .condense import _MONTH_WORDS, COMMON_WORDS, PLAIN_WORDS
from .contacts import Contact
from .snapshots import squash

# Ordinary words that are also given names or codes, beyond the grammar and status lists in condense.
EXTRA_ORDINARY = frozenset("""
one two three four five six seven eight nine ten zero joy grace hope faith mark rose art bill dawn june
april may will wade dean earl frank grant hunter lane max miles pat peace rich sky summer victor
""".split())


def ordinary(word: str) -> bool:
    low = word.casefold()
    return low in PLAIN_WORDS or low in COMMON_WORDS or low in EXTRA_ORDINARY or low in _MONTH_WORDS

_IDS = re.compile(
    r"https?://\S*(?:notion\.(?:so|site|com)|t\.me)\S*"
    r"|\b(?:notion\.(?:so|site|com)|t\.me)/\S*"
    r"|\btg://\S*"
    r"|\b[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}\b"
    r"|(?<![\w@])@[A-Za-z0-9_]{3,}"
    r"|(?<!\d)(?<!\d[.,])\d{7,}(?!\d)(?![.,]\d)",
    re.I,
)
_PLACEHOLDER = re.compile(
    r"(?<!\w)(?:\[\s*)?(person|project|id)\s*(\d+)\b(?:\s*\])?", re.I,
)


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
            kind, number = m.group(1).lower(), m.group(2)
            token = f"[{kind} {number}]"
            if token not in self.originals:
                return m.group(0)
            return "[id]" if kind == "id" else self.originals[token]
        return _PLACEHOLDER.sub(back, text)

    def canonicalize(self, text: str) -> str:
        """Use issued token spelling for grounding and quote checks before restoring names."""
        def canonical(m: re.Match) -> str:
            token = f"[{m.group(1).lower()} {m.group(2)}]"
            return token if token in self.originals else m.group(0)
        return _PLACEHOLDER.sub(canonical, text)


def unresolved(text: str) -> bool:
    """Numbered placeholders must never survive the local restore boundary; ``[id]`` may."""
    return _PLACEHOLDER.search(text) is not None


@dataclass(frozen=True)
class Redactor:
    """The people and project names to hide from free text, built once per brief run."""

    people: tuple[tuple[str, bool], ...] = ()  # (name, loose): loose matches in any case, else as written capitalised
    projects: tuple[tuple[str, str], ...] = ()  # (project page id, a name of it), matched ignoring case
    codes: tuple[tuple[str, str, bool], ...] = ()  # (project page id, its code, loose)
    known_ids: tuple[str, ...] = ()  # every known TelegramUserID, hidden wherever it stands as a number

    @classmethod
    def build(
        cls, contacts: Iterable[Contact], states: Iterable[dict[str, dict[str, object]]],
    ) -> Redactor:
        """``states``: the snapshots, so an old name of a project is caught too."""
        people: dict[str, bool] = {}
        ids: set[str] = set()
        for c in contacts:
            name = squash(c.name or "")
            if name and name != c.page_id:
                people[name] = True
                for word in name.split():
                    if len(word) >= 3 and word.casefold() not in _MONTH_WORDS and word != name:
                        people[word] = people.get(word, True) and not ordinary(word)
            for alias in (squash(x) for x in (c.aliases or "").split(",")):
                if len(alias) >= 2:
                    people[alias] = True
            if c.handle:
                people[c.handle.lstrip("@")] = True
            if c.telegram_id is not None:
                ids.add(str(c.telegram_id))
        states = list(states)
        names = {(p, v) for state in states for p, s in state.items() if len(v := squash(str(s.get("Name") or ""))) >= 4}
        codes = {(p, v) for state in states for p, s in state.items()
                 if len(v := squash(str(s.get("Project Code") or ""))) >= 2}
        return cls(
            tuple(sorted(people.items(), key=lambda kv: len(kv[0]), reverse=True)),
            tuple(sorted(names, key=lambda pair: len(pair[1]), reverse=True)),
            tuple((p, v, not ordinary(v)) for p, v in sorted(codes, key=lambda pair: len(pair[1]), reverse=True)),
            tuple(sorted(ids, key=len, reverse=True)),
        )

    def _own(self, own_page_id: str) -> set[str]:
        return {v.casefold() for p, v in self.projects if p == own_page_id} | {
            v.casefold() for p, v, _ in self.codes if p == own_page_id}

    def _id_pattern(self) -> re.Pattern | None:
        if not self.known_ids:
            return None
        return re.compile(r"(?<![\w\[.,])(?:" + "|".join(map(re.escape, self.known_ids)) + r")(?![\w\]]|[.,]\d)")

    def redact(self, text: str, own_page_id: str, mapping: Mapping) -> str:
        """``text`` with every identity outside the project ``own_page_id`` replaced by a placeholder."""
        own = self._own(own_page_id)
        text = _IDS.sub(lambda m: mapping.placeholder("id", m.group(0)), text)
        if (known := self._id_pattern()) is not None:
            text = known.sub(lambda m: mapping.placeholder("id", m.group(0)), text)
        for page_id, value in self.projects:
            if page_id != own_page_id and value.casefold() not in own:
                text = _word(value).sub(lambda m: mapping.placeholder("project", m.group(0)), text)
        for page_id, value, loose in self.codes:
            if page_id != own_page_id and value.casefold() not in own:
                text = _word(value, loose).sub(lambda m: mapping.placeholder("project", m.group(0)), text)
        for person, loose in self.people:
            if person.casefold() not in own:
                text = _word(person, loose).sub(lambda m: mapping.placeholder("person", m.group(0)), text)
        return text

    def names_in(self, text: str, own_page_id: str) -> str | None:
        """The first person or other-project name ``text`` holds, as ``redact`` would find it, or an id."""
        own = self._own(own_page_id)
        candidates = [(v, loose) for v, loose in self.people]
        candidates += [(v, True) for p, v in self.projects if p != own_page_id]
        candidates += [(v, loose) for p, v, loose in self.codes if p != own_page_id]
        for value, loose in candidates:
            if value.casefold() not in own and _word(value, loose).search(text):
                return value
        return None


def _word(value: str, loose: bool = True) -> re.Pattern:
    """Whole-word pattern: any case when ``loose``, else only as written, capitalised or in capitals."""
    if loose:
        return re.compile(rf"(?<![\w\[]){re.escape(value)}(?![\w\]])", re.I)
    forms = "|".join(dict.fromkeys(re.escape(f) for f in (value, value.capitalize(), value.upper())))
    return re.compile(rf"(?<![\w\[])(?:{forms})(?![\w\]])")
