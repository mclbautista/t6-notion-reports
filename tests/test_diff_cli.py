from datetime import datetime, timezone

from t6_psr_brief.cli import main
from t6_psr_brief.snapshots import ProjectRecord, connect, default_diff_pair, take_snapshot


def rec(stage, notes=""):
    return ProjectRecord("p1", {"Name": "Ria", "Project Code": "RIA", "Stage": stage, "PM Notes": notes,
                                "T6 Post Coordinator": [{"id": "c1", "name": "Pio"}]})


def at(d):
    return datetime(2026, 10, d, 3, tzinfo=timezone.utc)


def test_default_pair_is_latest_friday_and_monday_before_it():
    conn = connect()
    assert default_diff_pair(conn) is None
    m1 = take_snapshot(conn, [rec("Offline")], at(19), "monday").snapshot_id
    assert default_diff_pair(conn) is None
    f1 = take_snapshot(conn, [rec("Offline")], at(23), "friday").snapshot_id
    m2 = take_snapshot(conn, [rec("Online")], at(26), "monday").snapshot_id
    assert default_diff_pair(conn) == (m1, f1)
    f2 = take_snapshot(conn, [rec("Online")], at(30), "friday").snapshot_id
    assert default_diff_pair(conn) == (m2, f2)


def test_diff_command_prints_summary(tmp_path, capsys):
    db = str(tmp_path / "psr.db")
    conn = connect(db)
    take_snapshot(conn, [rec("Offline")], at(26), "monday")
    take_snapshot(conn, [rec("Online", "As of Oct 28\n- Online booked")], at(30), "friday")
    conn.close()
    assert main(["diff", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "== Pio ==" in out and "Stage: Offline → Online" in out and "Online booked" in out


def test_diff_with_explicit_ids_and_baseline_message(tmp_path, capsys):
    db = str(tmp_path / "psr.db")
    conn = connect(db)
    a = take_snapshot(conn, [rec("Offline")], at(26), "monday").snapshot_id
    b = take_snapshot(conn, [rec("Online")], at(30), "manual").snapshot_id
    conn.close()
    assert main(["diff", "--db", db]) == 0
    assert "first snapshot, no changes yet" in capsys.readouterr().out
    assert main(["diff", "--db", db, "--old", str(a), "--new", str(b)]) == 0
    assert "Stage: Offline → Online" in capsys.readouterr().out
    assert main(["diff", "--db", db, "--old", "1"]) == 2
