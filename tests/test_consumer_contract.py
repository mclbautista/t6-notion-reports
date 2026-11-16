"""The consumer contract (ADR 0002 in t6-core): every allow-list entry must be needed and dated."""

import json
from datetime import date
from pathlib import Path

from t6.consumer import apply_allowlist, check

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = ROOT / ".t6-consumer-allowlist.json"
LAST_EXPIRY = date(2027, 2, 15)


def _entries():
    return json.loads(ALLOWLIST.read_text(encoding="utf-8"))


def _remaining() -> list:
    return apply_allowlist(ROOT, check(ROOT))[0]


def test_every_finding_is_allowed():
    assert _remaining() == []


def test_entries_expire_by_the_cut_off():
    for entry in _entries():
        assert date.fromisoformat(entry["expires"]) <= LAST_EXPIRY, entry["name"]


def test_every_entry_is_needed():
    """An entry that matches no finding is stale: remove it, so the list only shrinks."""
    found = {(str(Path(f.file).relative_to(ROOT)), f.rule) for f in check(ROOT)}
    for entry in _entries():
        assert (entry["file"], entry["rule"]) in found, entry["name"]
