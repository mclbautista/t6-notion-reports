"""Resolve a free-text name to one Contacts page.

Copied from the resolver in t6-jo-tg-ntn (jo_watchdog/core/resolve.py: ``normalize_name``,
``split_aliases``, ``PeopleIndex``, ``fuzzy_ranking`` and ``_fuzzy``), minus the Telegram id and
handle steps, which a name taken from free text does not have. Keep the two in step by hand: there is
no shared package (out of scope in spec #40).

Precision beats recall: an ambiguous or weak match is unresolved, never a guess.

Steps, per tier:
1. Normalise: casefold and drop honorifics (ate, kuya, sir by default).
2. Contacts ``Aliases`` exactly. A collision is unresolved.
3. Guarded rapidfuzz ``WRatio`` against Contacts names: accepted only at ``fuzzy_min_score`` or more
   with a lead of ``fuzzy_min_lead`` or more over the runner-up.

Tier one is Contacts with a TelegramUserID. Tier two, the rest of Contacts, is searched only when tier
one has no match, so a team member wins over a namesake. A tier whose alias collides is a match that
cannot be resolved, so it does not fall through either.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from rapidfuzz import fuzz

from .contacts import Contact

DEFAULT_HONORIFICS = ("ate", "kuya", "sir")


@dataclass(frozen=True)
class MatchSettings:
    fuzzy_min_score: float = 90
    fuzzy_min_lead: float = 8
    honorifics: tuple[str, ...] = DEFAULT_HONORIFICS


def normalize_name(text: str, honorifics: Iterable[str] = DEFAULT_HONORIFICS) -> str:
    """Casefold, trim, collapse whitespace, and drop honorific words."""
    drop = {h.casefold() for h in honorifics}
    return " ".join(w for w in text.casefold().split() if w.strip(".") not in drop)


def split_aliases(aliases: str, honorifics: Iterable[str] = DEFAULT_HONORIFICS) -> list[str]:
    """Contacts ``Aliases`` is comma-separated; each entry is normalised like a mention."""
    return [n for part in aliases.split(",") if (n := normalize_name(part, honorifics))]


def match_person(name: str, contacts: Iterable[Contact], settings: MatchSettings = MatchSettings()) -> Contact | None:
    """The one Contacts page this name means, or None when it is unmatched or ambiguous."""
    norm = normalize_name(name, settings.honorifics)
    if not norm:
        return None
    contacts = list(contacts)
    onboarded = [c for c in contacts if c.telegram_id is not None]
    others = [c for c in contacts if c.telegram_id is None]
    for tier in (onboarded, others):
        found, matched = _match_tier(norm, tier, settings)
        if matched:
            return found
    return None


def _match_tier(norm: str, tier: Sequence[Contact], settings: MatchSettings) -> tuple[Contact | None, bool]:
    """(the contact or None, whether the tier had a match at all). An ambiguous alias is a match."""
    by_alias = [c for c in tier if norm in split_aliases(c.aliases, settings.honorifics)]
    if by_alias:
        return (by_alias[0] if len(by_alias) == 1 else None), True
    best: dict[str, tuple[float, Contact]] = {}
    for c in tier:
        title = normalize_name(c.name, settings.honorifics)
        if not title:
            continue
        score = fuzz.WRatio(norm, title)
        if c.page_id not in best or score > best[c.page_id][0]:
            best[c.page_id] = (score, c)
    ranked = sorted(best.values(), key=lambda sc: sc[0], reverse=True)
    if not ranked:
        return None, False
    top, contact = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
    if top >= settings.fuzzy_min_score and top - runner_up >= settings.fuzzy_min_lead:
        return contact, True
    return None, False
