from datetime import date

from t6_notion_reports.pm_notes import last_updated, parse_pm_notes

SNAP = date(2026, 10, 26)

REAL = """Prelim thoughts
As of Oct 26.,
  • Delay in Mixing
  • VBM will haggle new IMF submission (currently Nov 5)

as of Mon Dec 1:
- Color on Dec 3
- Online TBC

AS OF Seep 28
  • Lock picture
"""


def test_splits_preamble_and_blocks_tolerating_typos_and_bullets():
    notes = parse_pm_notes(REAL, SNAP)
    assert notes.preamble == "Prelim thoughts"
    assert [b.header_date for b in notes.blocks] == [
        date(2026, 10, 26), date(2025, 12, 1), date(2026, 9, 28)]
    assert notes.blocks[0].bullets == ["Delay in Mixing", "VBM will haggle new IMF submission (currently Nov 5)"]
    assert notes.blocks[1].bullets == ["Color on Dec 3", "Online TBC"]
    assert notes.blocks[2].bullets == ["Lock picture"]


def test_note_without_headers_is_all_preamble():
    notes = parse_pm_notes("Just a loose line\nand another", SNAP)
    assert notes.blocks == []
    assert notes.preamble == "Just a loose line\nand another"


def test_empty_and_none():
    assert parse_pm_notes("", SNAP).blocks == []
    assert parse_pm_notes(None, SNAP).preamble == ""


def test_year_is_closest_not_after_snapshot_plus_seven_days():
    jan = parse_pm_notes("As of Jan 3\n- x", date(2026, 12, 28)).blocks[0]
    assert jan.header_date == date(2027, 1, 3)  # within +7 days, so next year
    dec = parse_pm_notes("As of Dec 30\n- x", date(2027, 1, 4)).blocks[0]
    assert dec.header_date == date(2026, 12, 30)
    future = parse_pm_notes("As of Nov 5\n- x", date(2026, 10, 26)).blocks[0]
    assert future.header_date == date(2025, 11, 5)  # 10 days ahead: not allowed


def test_ria_oct28_in_oct26_snapshot_is_accepted_within_window():
    assert parse_pm_notes("As of Oct 28\n- x", SNAP).blocks[0].header_date == date(2026, 10, 28)


def test_unparseable_date_still_a_block():
    block = parse_pm_notes("As of sometime\n- x", SNAP).blocks[0]
    assert block.header_date is None and block.bullets == ["x"]


def test_text_after_date_on_header_line_becomes_a_bullet():
    block = parse_pm_notes("As of Oct 26: delay in mixing\n- two", SNAP).blocks[0]
    assert block.bullets == ["delay in mixing", "two"]


def test_last_updated_is_latest_header():
    assert last_updated(REAL, SNAP) == date(2026, 10, 26)
    assert last_updated("loose line", SNAP) is None
    assert last_updated("", SNAP) is None


def test_a_year_the_header_writes_is_kept_and_iso_headers_are_read():
    notes = parse_pm_notes("As of Nov 1, 2024\n- old\nAs of 2025-03-04\n- iso\nAs of Oct 20 2026: tail", SNAP)
    assert [b.header_date for b in notes.blocks] == [date(2024, 11, 1), date(2025, 3, 4), date(2026, 10, 20)]
    assert notes.blocks[2].bullets == ["tail"]
