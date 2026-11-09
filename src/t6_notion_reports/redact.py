"""Redact identities from free text before a consolidation call (spec #40 story 46, #60).

PM Notes and Minutes lines are free text, so they can name team members, quote a Telegram id or a
Notion link, or mention another project. Before the payload leaves the machine each such value is
replaced by a numbered placeholder ("[person 1]", "[project 1]", "[id 1]"). The mapping stays local:
the reply is validated against the redacted lines the LLM saw, and the placeholders are put back
afterwards so the brief reads normally.

What counts as a person: every Contacts name, each word of it (three letters or more, and not a month
name), each alias, and each Telegram handle. What counts as a project, other than the one the payload
is about: the name and code of any project in the snapshots, including short values.
Ids: known contact/project page ids, Notion links and page ids, Telegram links
(t.me and tg://), any @handle, runs of seven or more digits (Telegram ids, phone numbers) even when a
full stop or comma follows, and every known TelegramUserID wherever it stands as a number.
Matching is by whole word, longest first.

Approved exposure boundary (#94): known full contact names and aliases are hidden in every casing,
including ordinary words (so a known full name or alias "Will" also hides the verb "will"). Unknown
names cannot be reliably recognized and may remain. The target project's identity stays available.
A partial name word or project code that is not an ordinary word is hidden in any
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
    known_ids: tuple[str, ...] = ()  # known TelegramUserIDs and contact/project page ids

    @classmethod
    def build(
        cls, contacts: Iterable[Contact], states: Iterable[dict[str, dict[str, object]]],
    ) -> Redactor:
        """``states``: the snapshots, so an old name of a project is caught too."""
        people: dict[str, bool] = {}
        full_names: set[str] = set()
        ids: set[str] = set()
        for c in contacts:
            ids.add(c.page_id)
            name = squash(c.name or "")
            if name and name != c.page_id:
                people[name] = True
                full_names.add(name)
                for word in name.split():
                    if len(word) >= 3 and word.casefold() not in _MONTH_WORDS and word != name:
                        people[word] = people.get(word, True) and not ordinary(word)
            for alias in (squash(x) for x in (c.aliases or "").split(",")):
                if len(alias) >= 2:
                    people[alias] = True
                    full_names.add(alias)
            if c.handle:
                people[c.handle.lstrip("@")] = True
            if c.telegram_id is not None:
                ids.add(str(c.telegram_id))
        # An ordinary partial name from a later contact must not weaken a known full name/alias.
        people.update((name, True) for name in full_names)
        states = list(states)
        ids.update(p for state in states for p in state)
        names = {(p, v) for state in states for p, s in state.items() if (v := squash(str(s.get("Name") or "")))}
        codes = {(p, v) for state in states for p, s in state.items()
                 if (v := squash(str(s.get("Project Code") or "")))}
        return cls(
            tuple(sorted(people.items(), key=lambda kv: len(kv[0]), reverse=True)),
            tuple(sorted(names, key=lambda pair: len(pair[1]), reverse=True)),
            tuple((p, v, not ordinary(v)) for p, v in sorted(codes, key=lambda pair: len(pair[1]), reverse=True)),
            tuple(sorted(ids, key=len, reverse=True)),
        )

    def _own(self, own_page_id: str) -> set[str]:
        return {v.casefold() for p, v in self.projects if p == own_page_id} | {
            v.casefold() for p, v, _ in self.codes if p == own_page_id}

    def _id_pattern(self, own: set[str]) -> re.Pattern | None:
        values = [v for v in self.known_ids if v.casefold() not in own]
        if not values:
            return None
        patterns = []
        for value in values:
            # Punctuation delimits ids, including brackets and commas. A digit adjacent to a
            # decimal separator and another digit belongs to a decimal, not a Telegram id.
            decimal = r"(?<!\d[.,])" if value.lstrip("-").isdigit() else ""
            end = r"(?![.,]\d)" if value.lstrip("-").isdigit() else ""
            patterns.append(r"(?<!\w)" + decimal + re.escape(value) + r"(?!\w)" + end)
        return re.compile("|".join(patterns), re.I)

    def _identity_candidates(self, own_page_id: str):
        """Shared identity boundary for outgoing redaction and returned-name checks."""
        own = self._own(own_page_id)
        candidates = [("project", v, True) for p, v in self.projects if p != own_page_id]
        candidates += [("project", v, loose) for p, v, loose in self.codes if p != own_page_id]
        candidates += [("person", v, loose) for v, loose in self.people]
        return [(kind, value, loose) for kind, value, loose in candidates if value.casefold() not in own]

    def redact(self, text: str, own_page_id: str, mapping: Mapping) -> str:
        """``text`` with every identity outside the project ``own_page_id`` replaced by a placeholder."""
        own = self._own(own_page_id)
        # One pass over the original text: issued placeholders are never scanned again.
        # This lets punctuation (including brackets) delimit identities without treating raw source
        # brackets as trusted placeholders, or redacting a placeholder number that is itself an id.
        ids = [f"(?i:{_IDS.pattern})"]
        if (known := self._id_pattern(own)) is not None:
            ids.append(f"(?i:{known.pattern})")
        patterns = {"id": ids, "project": [], "person": []}
        for kind, value, loose in self._identity_candidates(own_page_id):
            patterns[kind].append(_word(value, loose).pattern)
        matchers = [(kind, re.compile("|".join(values))) for kind, values in patterns.items() if values]
        pieces: list[str] = []
        cursor = 0
        while matches := [(kind, match) for kind, matcher in matchers if (match := matcher.search(text, cursor))]:
            # Prefer the full known value when a short page id overlaps a longer name, e.g.
            # a fixture id "other" and project name "Other Show". Both remain protected.
            kind, match = min(matches, key=lambda pair: (pair[1].start(), -len(pair[1].group(0))))
            pieces.extend((text[cursor:match.start()], mapping.placeholder(kind, match.group(0))))
            cursor = match.end()
        pieces.append(text[cursor:])
        return "".join(pieces)

    def names_in(self, text: str, own_page_id: str) -> str | None:
        """The first person or other-project name ``text`` holds, as ``redact`` would find it, or an id."""
        for _, value, loose in self._identity_candidates(own_page_id):
            if _word(value, loose).search(text):
                return value
        return None


def _word(value: str, loose: bool = True) -> re.Pattern:
    """Whole-word pattern: any case when ``loose``, else only as written, capitalised or in capitals."""
    if loose:
        return re.compile(rf"(?i:(?<!\w){re.escape(value)}(?!\w))")
    forms = "|".join(dict.fromkeys(re.escape(f) for f in (value, value.capitalize(), value.upper())))
    return re.compile(rf"(?<!\w)(?:{forms})(?!\w)")
