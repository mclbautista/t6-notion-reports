from datetime import datetime, timedelta, timezone

import pytest

from fake_notion import (CONTACTS, JOB_ORDERS, PROJECTS, FakeNotion, contact, date, formula,
                         job_order, project, relation, rich, select, status)
from t6_psr_brief import cli
from t6_psr_brief.jo_snapshot_job import run_jo_snapshot
from t6_psr_brief.jo_snapshots import (TRACKED_FIELDS, connect, load_bounding, load_latest,
                                       load_snapshot)

T1 = datetime(2026, 11, 3, 5, tzinfo=timezone(timedelta(hours=8)))
T2 = T1 + timedelta(hours=7)
PEOPLE = [contact("c1", "Pio"), contact("c2", "Tim")]
PROJS = [project("pr1", name="Twenty Beauty", code="TWBTY"), project("pr2", name="No Code Show", code=None)]


def notion(*jos):
    return FakeNotion(projects=PROJS, contacts=PEOPLE, job_orders=jos)


def snap(conn, fake, kind="manual", at=T1):
    return run_jo_snapshot(conn, fake, JOB_ORDERS, CONTACTS, PROJECTS, kind, at)


def test_round_trips_every_field_with_ids_and_names():
    conn = connect()
    jo = job_order("j1", Assigned=relation("c1", "c2"), Projects=relation("pr1", "pr2"),
                   **{"Blocker ": rich("waiting  on\nrushes"), "Health": formula("🔴 Blocked"),
                      "Status": status("In progress"), "Department": select("VFX")})
    sid = snap(conn, notion(jo)).snapshot_id
    page = load_snapshot(conn, sid).pages["j1"]
    assert set(page) == set(TRACKED_FIELDS)
    assert page["Blocker "] == "waiting on rushes"
    assert page["Health"] == "🔴 Blocked"
    assert (page["Name"], page["JO Number"], page["Task"]) == ("TWBTY - AE - JO #48", "48", "Export")
    assert (page["Status"], page["Department"]) == ("In progress", "VFX")
    assert page["Assigned"] == [{"id": "c1", "name": "Pio"}, {"id": "c2", "name": "Tim"}]
    # Project code if there is one, else name.
    assert page["Projects"] == [{"id": "pr1", "name": "TWBTY"}, {"id": "pr2", "name": "No Code Show"}]
    assert page["last_edited_time"] == "2026-11-03T10:00:00.000Z"


def test_projects_tables_are_untouched():
    conn = connect()
    from t6_psr_brief.snapshots import connect as projects_connect
    # The two stores share one file; the JO store never creates or writes the Projects tables.
    snap(conn, notion(job_order("j1")))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not tables & {"snapshots", "snapshot_projects", "project_state"}
    assert not any(r[0].startswith("jo_") for r in projects_connect().execute("SELECT name FROM sqlite_master"))


def test_same_fields_point_at_earlier_rows_and_health_alone_changes():
    conn = connect()
    r1 = snap(conn, notion(job_order("j1"), job_order("j2")))
    r2 = snap(conn, notion(job_order("j1"), job_order("j2", Health=formula("🔥 Urgent"))), "1200", T2)
    assert (r2.changed, r2.unchanged) == (1, 1)
    state = dict(conn.execute("SELECT page_id, state_snapshot_id FROM jo_snapshot_pages WHERE snapshot_id = ?",
                              (r2.snapshot_id,)))
    assert state == {"j1": r1.snapshot_id, "j2": r2.snapshot_id}
    assert load_snapshot(conn, r2.snapshot_id).pages["j1"]["Health"] == "🔵 On Track"


def test_vanished_page_is_marked_removed_and_not_loaded():
    conn = connect()
    snap(conn, notion(job_order("j1"), job_order("j2")))
    res = snap(conn, notion(job_order("j1")), "1200", T2)
    assert res.removed == 1
    assert conn.execute("SELECT removed FROM jo_snapshot_pages WHERE snapshot_id=? AND page_id='j2'",
                        (res.snapshot_id,)).fetchone() == (1,)
    assert set(load_snapshot(conn, res.snapshot_id).pages) == {"j1"}


def test_first_snapshot_is_baseline_later_are_not():
    conn = connect()
    assert snap(conn, notion(job_order("j1"))).is_baseline is True
    assert snap(conn, notion(job_order("j1")), "0500", T2).is_baseline is False
    assert load_latest(conn).is_baseline is False


def test_dates_follow_spec_3_1():
    conn = connect()
    jo = job_order("j1", Date=date("2026-11-03"), Deadline=date("2026-11-03"))
    jo2 = job_order("j2", Date=date("2026-11-03T01:00:00.000Z", "2026-11-04"),
                    Deadline=date("2026-11-03T18:30:00.000+08:00"))
    jo3 = job_order("j3", Deadline=date(None), Date=date(None))
    pages = load_snapshot(conn, snap(conn, notion(jo, jo2, jo3)).snapshot_id).pages
    assert pages["j1"]["Deadline"] == "2026-11-03T23:59:00+08:00"
    assert pages["j1"]["Date"] == "2026-11-03T00:00:00+08:00"
    assert pages["j2"]["Date"] == "2026-11-03T09:00:00+08:00"  # range uses start; same instant
    assert pages["j2"]["Deadline"] == "2026-11-03T18:30:00+08:00"
    assert pages["j3"]["Deadline"] is None and pages["j3"]["Date"] is None


def test_retention_drops_old_snapshots_but_keeps_referenced_rows():
    conn = connect()
    old = T1 - timedelta(days=100)
    r1 = snap(conn, notion(job_order("j1"), job_order("j2")), at=old)
    snap(conn, notion(job_order("j1"), job_order("j2", Health=formula("⚠️ Delayed"))), "1800", old + timedelta(days=1))
    r3 = snap(conn, notion(job_order("j1"), job_order("j2", Health=formula("✅ Closed"))), "0500", T1)
    # j1 never changed: the newest snapshot still points at the 100-day-old rows.
    assert conn.execute("SELECT COUNT(*) FROM jo_snapshots WHERE snapshot_id = ?", (r1.snapshot_id,)).fetchone()[0] == 1
    assert r3.pruned == 1  # the second snapshot; the first is kept for j1's rows
    assert load_snapshot(conn, r3.snapshot_id).pages["j1"]["Health"] == "🔵 On Track"
    assert load_snapshot(conn, r3.snapshot_id).pages["j2"]["Health"] == "✅ Closed"
    # j2's older versions (referenced by no newer snapshot) are gone.
    assert conn.execute("SELECT COUNT(DISTINCT snapshot_id) FROM jo_page_state WHERE page_id='j2'").fetchone()[0] == 1
    # Recent snapshots are untouched.
    assert conn.execute("SELECT COUNT(*) FROM jo_snapshots").fetchone()[0] == 2


def test_load_latest_and_bounding():
    conn = connect()
    assert load_latest(conn) is None
    r1 = snap(conn, notion(job_order("j1")), "1800", T1)
    r2 = snap(conn, notion(job_order("j1")), "0500", T2)
    assert load_latest(conn).snapshot_id == r2.snapshot_id
    assert load_bounding(conn, T2 - timedelta(minutes=1)).snapshot_id == r1.snapshot_id
    assert load_bounding(conn, T2).snapshot_id == r2.snapshot_id
    assert load_bounding(conn, T2, kinds=("1800",)).snapshot_id == r1.snapshot_id
    assert load_bounding(conn, T1 - timedelta(hours=1)) is None


def test_trashed_pages_are_skipped():
    conn = connect()
    gone = job_order("j2")
    gone["in_trash"] = True
    assert set(load_latest_pages(conn, notion(job_order("j1"), gone))) == {"j1"}


def load_latest_pages(conn, fake):
    return load_snapshot(conn, snap(conn, fake).snapshot_id).pages


def test_cli_logs_one_line_with_counts_and_no_values(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "ntn_supersecret")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f'projects_data_source: "{PROJECTS_ID}"\ncontacts_data_source: "{CONTACTS_ID}"\n'
                   f'jo_data_source: "{JO_ID}"\ndatabase_path: "{tmp_path / "psr.db"}"\n')
    fake = notion(job_order("j1", Task=rich("SECRET-TASK-TEXT")))
    fake.query_data_source = lambda ds, filter=None: {
        PROJECTS_ID: fake.projects, CONTACTS_ID: fake.contacts, JO_ID: fake.job_orders}[ds]
    assert cli.main(["jo-snapshot", "--config", str(cfg)], notion=fake, now=T1) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["jo-snapshot id=1 kind=manual baseline=True changed=1 unchanged=0 removed=0 pruned=0"]


def test_cli_requires_jo_data_source(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "ntn_supersecret")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f'projects_data_source: "{PROJECTS_ID}"\ncontacts_data_source: "{CONTACTS_ID}"\n')
    assert cli.main(["jo-snapshot", "--config", str(cfg)]) == 2
    assert "jo_data_source" in capsys.readouterr().err


PROJECTS_ID = "11111111-1111-1111-1111-111111111111"
CONTACTS_ID = "22222222-2222-2222-2222-222222222222"
JO_ID = "3d8fb4d5-9e59-8073-a0d0-000b77632947"
