from datetime import datetime, timezone

import pytest

from fake_notion import (CONTACTS, PROJECTS, FakeNotion, contact, date, project, relation,
                         rich, select)
from t6_notion_reports.diff import diff_snapshots
from t6_notion_reports.snapshots import (TRACKED_FIELDS, connect, latest_snapshot_ids,
                                    load_snapshot, snapshot_info)
from t6_notion_reports.snapshot_job import run_snapshot

T1 = datetime(2026, 11, 9, 11, tzinfo=timezone.utc)
T2 = datetime(2026, 11, 13, 18, tzinfo=timezone.utc)
CONTACTS_LIST = [contact("c1", "Pio"), contact("c2", "Tim"), contact("c3", "Jason")]


def snap(conn, notion, kind="manual", at=T1):
    return run_snapshot(conn, notion, PROJECTS, CONTACTS, kind, at)


def test_stores_exactly_tracked_fields_and_never_reads_formulas(tmp_path):
    conn = connect(str(tmp_path / "psr.db"))
    notion = FakeNotion([project("p1")], CONTACTS_LIST)
    sid = snap(conn, notion).snapshot_id
    assert set(load_snapshot(conn, sid)["p1"]) == set(TRACKED_FIELDS)
    raw = conn.execute("SELECT value_json FROM project_state").fetchall()
    assert not any("SECRET-FORMULA-VALUE" in r[0] for r in raw)
    fields = {r[0] for r in conn.execute("SELECT DISTINCT field FROM project_state")}
    assert fields == set(TRACKED_FIELDS)


def test_field_values_parsed():
    conn = connect()
    p = project("p1", name="Bawod", code="BW", **{"Project Health": select("At Risk"),
                   "PM Notes": rich("As of Oct 26,\n  • Delay"),
                   "Playdate": date("2026-11-20"), "Submission Deadline": date("2026-11-22"),
                   "Archival Deadline": date("2026-12-01")})
    sid = snap(conn, FakeNotion([p], CONTACTS_LIST)).snapshot_id
    s = load_snapshot(conn, sid)["p1"]
    assert (s["Name"], s["Project Code"], s["Project Health"]) == ("Bawod", "BW", "At Risk")
    assert s["PM Notes"] == "As of Oct 26,\n  • Delay"
    assert s["Playdate"] == {"start": "2026-11-20", "end": None}
    assert s["Submission Deadline"] == {"start": "2026-11-22", "end": None}
    assert s["Archival Deadline"] == {"start": "2026-12-01", "end": None}
    assert "Health" not in s and "Deadline" not in s


def test_excludes_archived_and_lost_but_keeps_leads_standby_archiving():
    conn = connect()
    pages = [project("a", Status=select("📦 Archived")), project("l", Status=select("🧭  Lost")),
             project("lead", Status=select("👀  Lead")), project("arch", Status=select("📦 Archiving")),
             project("ok")]
    sid = snap(conn, FakeNotion(pages, CONTACTS_LIST)).snapshot_id
    assert set(load_snapshot(conn, sid)) == {"lead", "arch", "ok"}


def test_vanished_or_archived_page_is_recorded_as_removed():
    conn = connect()
    first = [project("p1"), project("p2"), project("p3")]
    snap(conn, FakeNotion(first, CONTACTS_LIST))
    second = [project("p1"), project("p2", Status=select("📦 Archived"))]  # p3 vanished
    res = snap(conn, FakeNotion(second, CONTACTS_LIST), "friday", T2)
    assert res.removed == 2
    assert sorted(snapshot_info(conn, res.snapshot_id).removed_page_ids) == ["p2", "p3"]
    old, new = (load_snapshot(conn, i) for i in reversed(latest_snapshot_ids(conn)))
    kinds = {c.page_id: c.kind for c in diff_snapshots(old, new)}
    assert kinds == {"p2": "removed", "p3": "removed"}


def test_removed_is_recorded_once_and_return_is_added():
    conn = connect()
    snap(conn, FakeNotion([project("p1")], CONTACTS_LIST))
    s2 = snap(conn, FakeNotion([], CONTACTS_LIST), at=T2).snapshot_id
    s3 = snap(conn, FakeNotion([], CONTACTS_LIST), at=T2).snapshot_id
    assert snapshot_info(conn, s3).removed_page_ids == []
    assert snapshot_info(conn, s2).removed_page_ids == ["p1"]
    s4 = snap(conn, FakeNotion([project("p1")], CONTACTS_LIST), at=T2).snapshot_id
    assert [c.kind for c in diff_snapshots(load_snapshot(conn, s3), load_snapshot(conn, s4))] == ["added"]


def test_relations_keep_ids_and_names_and_status_keeps_raw_value():
    conn = connect()
    p = project("p1", Status=select("🚨  In-Progress "), Editors=relation("c2", "c1"),
                **{"T6 Post Coordinator": relation("c1")})
    sid = snap(conn, FakeNotion([p], CONTACTS_LIST)).snapshot_id
    s = load_snapshot(conn, sid)["p1"]
    assert s["T6 Post Coordinator"] == [{"id": "c1", "name": "Pio"}]
    assert s["Editors"] == [{"id": "c1", "name": "Pio"}, {"id": "c2", "name": "Tim"}]
    assert s["Online Editor"] == []
    assert s["Status"] == "🚨  In-Progress "


def test_unknown_contact_keeps_id_with_no_name():
    conn = connect()
    sid = snap(conn, FakeNotion([project("p1", Colorist=relation("zzz"))], CONTACTS_LIST)).snapshot_id
    assert load_snapshot(conn, sid)["p1"]["Colorist"] == [{"id": "zzz", "name": None}]


def test_unchanged_projects_are_skipped_via_content_hash():
    conn = connect()
    pages = [project("p1"), project("p2")]
    snap(conn, FakeNotion(pages, CONTACTS_LIST))
    rows_before = conn.execute("SELECT COUNT(*) FROM project_state").fetchone()[0]
    pages2 = [project("p1"), project("p2", Stage=select("Online"))]
    res = snap(conn, FakeNotion(pages2, CONTACTS_LIST), "friday", T2)
    assert (res.changed, res.unchanged) == (1, 1)
    assert conn.execute("SELECT COUNT(*) FROM project_state").fetchone()[0] == rows_before + len(TRACKED_FIELDS)
    state = load_snapshot(conn, res.snapshot_id)  # unchanged project still fully readable
    assert state["p1"]["Stage"] == "Offline" and state["p2"]["Stage"] == "Online"


def test_whitespace_only_change_counts_as_unchanged():
    conn = connect()
    snap(conn, FakeNotion([project("p1")], CONTACTS_LIST))
    res = snap(conn, FakeNotion([project("p1", Status=select("In-Progress  "))], CONTACTS_LIST), at=T2)
    assert res.unchanged == 1


def test_kind_timestamp_and_baseline_flag():
    conn = connect()
    a = snap(conn, FakeNotion([project("p1")], CONTACTS_LIST), "monday", T1)
    b = snap(conn, FakeNotion([project("p1")], CONTACTS_LIST), "friday", T2)
    ia, ib = snapshot_info(conn, a.snapshot_id), snapshot_info(conn, b.snapshot_id)
    assert (ia.kind, ia.taken_at, ia.is_baseline) == ("monday", T1.isoformat(), True)
    assert (ib.kind, ib.taken_at, ib.is_baseline) == ("friday", T2.isoformat(), False)


def test_rejects_unknown_kind():
    with pytest.raises(ValueError):
        snap(connect(), FakeNotion([], CONTACTS_LIST), "tuesday")


def test_reads_only_projects_and_contacts():
    notion = FakeNotion([project("p1")], CONTACTS_LIST)
    snap(connect(), notion)
    assert sorted(notion.queried) == [CONTACTS, PROJECTS]


def test_pm_notes_whitespace_only_edit_is_unchanged():
    conn = connect()
    snap(conn, FakeNotion([project("p1", **{"PM Notes": rich("As of Oct 26,\n • Delay")})], CONTACTS_LIST))
    res = snap(conn, FakeNotion([project("p1", **{"PM Notes": rich("As of Oct 26,\n  •  Delay")})],
                                CONTACTS_LIST), at=T2)
    assert res.unchanged == 1


def test_plain_archived_status_without_emoji_is_excluded():
    conn = connect()
    sid = snap(conn, FakeNotion([project("a", Status=select("Archived")), project("b")], CONTACTS_LIST)).snapshot_id
    assert set(load_snapshot(conn, sid)) == {"b"}
