from datetime import datetime, timezone

from t6_notion_reports.diff import diff_snapshots, group_by_coordinator
from t6_notion_reports.snapshots import (
    ProjectRecord, connect, latest_snapshot_ids, load_snapshot, take_snapshot as _take,
)


def take_snapshot(conn, records, at):
    return _take(conn, records, at).snapshot_id


def rec(page_id, **fields):
    base = {"Name": "Show", "Project Code": "PAS", "Stage": "Offline",
            "Status": "In-Progress", "PM Notes": "", "Deadline": None,
            "T6 Post Coordinator": [{"id": "c1", "name": "Pio"}], "Editors": []}
    base.update(fields)
    return ProjectRecord(page_id, base)


def two_snapshots(first, second):
    conn = connect()
    a = take_snapshot(conn, first, datetime(2026, 11, 2, tzinfo=timezone.utc))
    b = take_snapshot(conn, second, datetime(2026, 11, 9, tzinfo=timezone.utc))
    return conn, a, b


def test_roundtrip_and_order():
    conn, a, b = two_snapshots([rec("p1")], [rec("p1")])
    assert latest_snapshot_ids(conn) == [b, a]
    assert load_snapshot(conn, a)["p1"]["Stage"] == "Offline"


def test_no_change_gives_empty_diff():
    conn, a, b = two_snapshots([rec("p1")], [rec("p1")])
    assert diff_snapshots(load_snapshot(conn, a), load_snapshot(conn, b)) == []


def test_field_change_added_removed():
    conn, a, b = two_snapshots(
        [rec("p1"), rec("p2", **{"Project Code": "BW"})],
        [rec("p1", Stage="Online", **{"PM Notes": "  new note "}), rec("p3", **{"Project Code": "KB"})],
    )
    changes = {c.page_id: c for c in diff_snapshots(load_snapshot(conn, a), load_snapshot(conn, b))}
    assert changes["p2"].kind == "removed"
    assert changes["p3"].kind == "added"
    fields = {f.field: (f.old, f.new) for f in changes["p1"].changes}
    assert fields == {"Stage": ("Offline", "Online")}
    assert changes["p1"].notes.text_diff == ["+ new note"]


def test_whitespace_only_change_is_not_a_change():
    conn, a, b = two_snapshots([rec("p1", Status="In-Progress")], [rec("p1", Status=" In-Progress  ")])
    assert diff_snapshots(load_snapshot(conn, a), load_snapshot(conn, b)) == []


def test_relation_order_is_ignored():
    conn, a, b = two_snapshots(
        [rec("p1", Editors=[{"id": "t", "name": "Tim"}, {"id": "j", "name": "Jason"}])],
        [rec("p1", Editors=[{"id": "j", "name": "Jason"}, {"id": "t", "name": "Tim"}])])
    assert diff_snapshots(load_snapshot(conn, a), load_snapshot(conn, b)) == []


def test_group_by_coordinator_and_unassigned():
    conn, a, b = two_snapshots(
        [rec("p1"), rec("p2", **{"T6 Post Coordinator": []})],
        [rec("p1", Stage="Online"), rec("p2", Stage="Online", **{"T6 Post Coordinator": []})],
    )
    grouped = group_by_coordinator(diff_snapshots(load_snapshot(conn, a), load_snapshot(conn, b)))
    assert set(grouped.by_coordinator) == {"c1"}
    assert [c.page_id for c in grouped.no_coordinator] == ["p2"]
