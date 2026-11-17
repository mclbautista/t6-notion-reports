"""The consumer contract (ADR 0002 in t6-core): the checker passes, and every allow-list entry is dated and names a file."""

import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = ROOT / ".t6-consumer-allowlist.json"
LAST_EXPIRY = date(2027, 2, 15)


def _entries():
    return json.loads(ALLOWLIST.read_text(encoding="utf-8"))


@pytest.mark.skipif(shutil.which("t6") is None, reason="the t6 command is not on PATH")
def test_every_finding_is_allowed():
    done = subprocess.run(["t6", "admin", "consumer-check", str(ROOT)], capture_output=True, text=True)
    assert done.returncode == 0, json.loads(done.stdout)["findings"]


def test_entries_expire_by_the_cut_off():
    for entry in _entries():
        assert date.fromisoformat(entry["expires"]) <= LAST_EXPIRY, entry["name"]


def test_every_entry_names_a_file_that_exists():
    """An entry for a file that is gone is stale: remove it, so the list only shrinks."""
    for entry in _entries():
        assert (ROOT / entry["file"]).exists(), entry["name"]
