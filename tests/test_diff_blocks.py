from datetime import date

from t6_notion_reports.diff import diff_snapshots, format_diff, group_by_coordinator

D1, D2 = date(2026, 10, 26), date(2026, 10, 30)  # Monday baseline, Friday


def proj(code="RIA", name="Ria", notes="", health=None, coords=(("c1", "Pio"),), **extra):
    return {"Name": name, "Project Code": code, "Stage": "Offline", "Status": "In-Progress",
            "Project Health": health, "PM Notes": notes, "Playdate": None,
            "T6 Post Coordinator": [{"id": i, "name": n} for i, n in coords], "Editors": [], **extra}


def run(old, new):
    return diff_snapshots(old, new, D1, D2)


def test_new_block_is_reported_and_old_blocks_are_not():
    old = {"p": proj(notes="As of Oct 19\n- Locked")}
    new = {"p": proj(notes="As of Oct 19\n- Locked\nAs of Oct 28\n- Online booked")}
    (ch,) = run(old, new)
    assert [(b.kind, b.block.header) for b in ch.notes.blocks] == [("added", "As of Oct 28")]
    assert ch.changes == []


def test_ria_oct28_block_in_a_monday_oct26_snapshot_is_new():
    (ch,) = diff_snapshots({"p": proj(notes="")}, {"p": proj(notes="As of Oct 28\n- x")}, D1, D1)
    assert ch.notes.blocks[0].block.header_date == date(2026, 10, 28)


def test_edited_old_block_is_a_change():
    old = {"p": proj(notes="As of Oct 19\n- Locked")}
    new = {"p": proj(notes="As of Oct 19\n- Locked\n- Unlocked again")}
    (ch,) = run(old, new)
    assert [(b.kind, b.block.bullets) for b in ch.notes.blocks] == [
        ("changed", ["Locked", "Unlocked again"])]


def test_matched_by_text_not_header_date():
    old = {"p": proj(notes="As of Oct 19\n- Locked")}
    assert run(old, {"p": proj(notes="As of Oct 20\n- Locked")}) == []
    assert run(old, {"p": proj(notes="as of Oct 19:\n  • Locked  ")}) == []


def test_note_without_headers_falls_back_to_text_diff():
    old = {"p": proj(notes="Waiting on VBM")}
    new = {"p": proj(notes="Waiting on VBM\nQC done")}
    (ch,) = run(old, new)
    assert ch.notes.blocks == [] and ch.notes.text_diff == ["+ QC done"]
    assert run(old, {"p": proj(notes="Waiting   on VBM")}) == []


def test_last_updated_from_latest_header_or_none():
    chs = {c.name: c for c in run({}, {"p": proj(notes="As of Oct 19\n- a\nAs of Oct 28\n- b"),
                                     "q": proj(code="X", notes="")})}
    assert chs["RIA"].last_updated == date(2026, 10, 28)
    assert chs["X"].last_updated is None


def test_other_fields_old_to_new_and_ignores_whitespace_and_relation_order():
    old = {"p": proj(Stage="Offline", Editors=[{"id": "a", "name": "A"}, {"id": "b", "name": "B"}])}
    new = {"p": proj(Stage="Online", Editors=[{"id": "b", "name": "B"}, {"id": "a", "name": "A"}])}
    (ch,) = run(old, new)
    assert [(f.field, f.old, f.new) for f in ch.changes] == [("Stage", "Offline", "Online")]


def test_added_project_lists_its_blocks_and_removed_has_none():
    (add,) = run({}, {"p": proj(notes="As of Oct 28\n- x")})
    assert add.kind == "added" and len(add.notes.blocks) == 1
    (rem,) = run({"p": proj()}, {})
    assert rem.kind == "removed" and rem.notes is None


def test_sort_critical_at_risk_on_track_none_then_code():
    states = {
        "1": proj(code="ZZ", health=None), "2": proj(code="BB", health="On Track"),
        "3": proj(code="CC", health="Critical"), "4": proj(code="AA", health=None),
        "5": proj(code="DD", health="At Risk"), "6": proj(code="AB", health="Critical"),
    }
    changed = {k: {**v, "Stage": "Online"} for k, v in states.items()}
    assert [c.name for c in run(states, changed)] == ["AB", "CC", "DD", "BB", "AA", "ZZ"]


def test_display_name_uses_code_then_name_and_disambiguates_shared_codes():
    old = {"1": proj(code="BW", name="Bawod"), "2": proj(code="BW", name="Body Works"),
           "3": proj(code=None, name="Untitled"), "4": proj(code="PAS", name="Pas")}
    new = {k: {**v, "Stage": "Online"} for k, v in old.items()}
    assert sorted(c.name for c in run(old, new)) == ["BW (Bawod)", "BW (Body Works)", "PAS", "Untitled"]
    # Only one of the pair changed: the name is still disambiguated.
    only = {**old, "1": {**old["1"], "Stage": "Online"}}
    assert [c.name for c in run(old, only)] == ["BW (Bawod)"]


def test_grouping_multi_coordinator_and_none():
    old = {"1": proj(code="A", coords=(("c1", "Pio"), ("c2", "Ann"))),
           "2": proj(code="B", coords=()), "3": proj(code="C", coords=(("c2", "Ann"),))}
    new = {k: {**v, "Stage": "Online"} for k, v in old.items()}
    g = group_by_coordinator(run(old, new))
    assert [c.name for c in g.by_coordinator["c1"][1]] == ["A"]
    assert [c.name for c in g.by_coordinator["c2"][1]] == ["A", "C"]
    assert [c.name for c in g.no_coordinator] == ["B"]
    assert all(c.name != "B" for _, items in g.by_coordinator.values() for c in items)


def test_format_diff_is_readable():
    old = {"p": proj(health="At Risk", notes="As of Oct 19\n- Locked"), "q": proj(code="NC", coords=())}
    new = {"p": proj(health="At Risk", Stage="Online", notes="As of Oct 19\n- Locked\nAs of Oct 28\n- Online booked"),
           "q": proj(code="NC", coords=(), Stage="Online")}
    out = format_diff(run(old, new))
    assert "== Pio ==" in out and "[changed] RIA · At Risk" in out
    assert "Stage: Offline → Online" in out and "Online booked" in out
    assert out.index("== No coordinator (digest only) ==") > out.index("== Pio ==")
    assert format_diff([]) == "No changes."


def test_plain_text_diff_reports_removed_lines_and_ignores_reorder():
    old = {"p": proj(notes="Waiting on VBM\nQC done")}
    (ch,) = run(old, {"p": proj(notes="Waiting on VBM")})
    assert ch.notes.text_diff == ["- QC done"]
    assert run(old, {"p": proj(notes="QC done\nWaiting on VBM")}) == []


def test_header_tail_is_not_duplicated_and_mid_block_as_of_bullet_stays_a_bullet():
    from t6_notion_reports.pm_notes import parse_pm_notes
    b = parse_pm_notes("As of Oct 26: delay\n- two", D1).blocks[0]
    assert b.text.count("delay") == 1
    notes = parse_pm_notes("As of Oct 26\n- as of Nov 5 VBM files\n- x", D1)
    assert len(notes.blocks) == 1 and notes.blocks[0].bullets[0].startswith("as of Nov 5")


def test_block_with_unparsed_date_is_diffed_by_header_text():
    old = {"p": proj(notes="As of sometime\n- x")}
    (ch,) = run(old, {"p": proj(notes="As of sometime\n- x\nAs of later")})
    assert [b.block.header for b in ch.notes.blocks] == ["As of later"]
