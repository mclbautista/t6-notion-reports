import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
import yaml

from fake_notion import (CONTACTS, MEETING, PROJECTS, FakeNotion, contact, meeting_page, project,
                         rich, select)
from t6_notion_reports import cli
from t6_notion_reports.cli import make_job, main
from t6_notion_reports.config import load_config
from t6_notion_reports.holidays import upcoming_entries
from t6_notion_reports.mom import GROUPS, build_draft, group_of
from t6_notion_reports.mom_job import MomAlreadySent, run_draft_mom
from t6_notion_reports.schedule import MANILA, Scheduler
from t6_notion_reports.snapshot_job import run_snapshot
from t6_notion_reports.snapshots import SCHEMA, mom_diff_pair

FRIDAY = datetime(2026, 11, 13, 10, tzinfo=timezone.utc)
MONDAY = datetime(2026, 11, 16, 3, tzinfo=timezone.utc)  # 11:00 Manila
D = date(2026, 11, 16)
NOTES_OLD = "As of Nov 6,\n  • Old news"
NOTES_NEW = NOTES_OLD + "\nAs of Nov 16,\n  • Delay in Mixing\n  • VBM will haggle"


def pages(monday):
    notes = NOTES_NEW if monday else NOTES_OLD
    return [
        project("ling", "Lingo", "LING", Stage=select("Archiving"), **{"PM Notes": rich(NOTES_OLD)}),
        project("lead-idle", "Idle Lead", "IDL", Status=select("👀  Lead")),
        project("lead-new", "New Lead", "NLD", Status=select("🌱 Leads"),
                **{"PM Notes": rich(NOTES_NEW if monday else "")}),
        project("nohealth", "No Health", "NH", **{"Project Health": select(None), "PM Notes": rich(NOTES_OLD)}),
        project("crit", "Crit", "cr", **{"Project Health": select("Critical"), "PM Notes": rich(notes)}),
        project("standby", "Stand", "SBY", Status=select("⏸ Standby")),
        project("empty", "Fresh", "FRS", **{"PM Notes": rich("")}),
    ]


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    run_snapshot(c, FakeNotion(pages(False), []), PROJECTS, CONTACTS, "friday", FRIDAY)
    run_snapshot(c, FakeNotion(pages(True), []), PROJECTS, CONTACTS, "monday", MONDAY)
    return c


def contacts():
    out = [contact("c-quil", "Quilla"), contact("c-oto", "Oto")]
    out[0]["properties"]["TelegramHandle"] = rich("@quil")
    return out


def draft(conn, **kw):
    n = FakeNotion(pages(True), contacts(), kw.pop("meetings", []), kw.pop("description", ""))
    res = run_draft_mom(conn, n, PROJECTS, CONTACTS, MEETING, kw.pop("day", D), log=lambda _: None)
    return n, res


def texts(blocks):
    out = []
    for b in blocks:
        body = b[b["type"]]
        out.append((b["type"], "".join(t["text"]["content"] for t in body["rich_text"])))
        out += texts(body.get("children", []))
    return out


def all_blocks(n):
    return n.created[0]["children"] + [c for _, cs in n.appended for c in cs]


# ---- group rules ----

def test_group_rules_first_match_wins():
    g = lambda **f: group_of({"Status": "In-Progress", "Stage": "Offline", "Project Health": "On Track", **f})
    assert g(Stage="Archiving", **{"Project Health": "Critical"}) == "archiving"  # archiving beats health
    assert g(Status="📦 Archiving") == "archiving"
    assert g(Stage="For Drives Turnover") == "archiving"
    assert g(Stage="Waiting for Payment") == "archiving"
    assert g(Status="👀  Lead") == "incoming"
    assert g(Status="🔏   Negotiations", **{"Project Health": "Critical"}) == "incoming"
    assert g(Status="🙋  Proposal Made") == "incoming"
    assert g(Status="🚨 In-Progress", **{"Project Health": "Critical"}) == "critical"
    assert g(**{"Project Health": "At Risk"}) == "at_risk"
    assert g(**{"Project Health": None}) == "on_track"


def test_group_order_on_the_page():
    assert [e for _, e, _ in GROUPS] == ["🔴", "🟠", "🟡", "🔵", "🟢"]


# ---- the draft page ----

def test_creates_page_with_title_date_and_relations(conn):
    n, res = draft(conn)
    props = n.created[0]["properties"]
    assert props["Name"]["title"][0]["text"]["content"] == "PSR MOM 2026-11-16"
    assert props["Meeting Date"]["date"]["start"] == "2026-11-16"
    related = {r["id"] for r in props["Project"]["relation"]}
    assert related == {"ling", "lead-new", "nohealth", "crit", "empty"}  # shown projects only
    assert res.page_id == "new-1"


def test_ling_under_archiving_lead_under_incoming_empty_health_flagged(conn):
    n, _ = draft(conn)
    t = texts(all_blocks(n))
    heads = [x for k, x in t if k == "heading_2"]
    assert heads[:4] == ["🔴 Critical / off track", "🟡 On track", "🔵 Archiving", "🟢 Incoming"]
    flat = [x for _, x in t]
    assert flat.index("🔵 Archiving") < flat.index("LING - last updated Nov 6") < flat.index("🟢 Incoming")
    assert "NH (Health not set) - last updated Nov 6" in flat
    assert "NLD" in flat and "Delay in Mixing" in flat and "VBM will haggle" in flat
    assert "CR" in flat  # code in capitals


def test_in_progress_without_changes_gets_last_updated_line(conn):
    n, _ = draft(conn)
    flat = [x for _, x in texts(all_blocks(n))]
    assert "FRS - no notes yet" in flat
    assert "LING - last updated Nov 6" in flat


def test_unchanged_leads_only_in_unchanged_list(conn):
    n, _ = draft(conn)
    t = texts(all_blocks(n))
    flat = [x for _, x in t]
    cut = flat.index("Unchanged (Notion only, not sent)")
    assert [x for x in flat[cut + 1:]] == ["IDL - no notes yet", "SBY - no notes yet"]
    assert not any(x.startswith(("IDL", "SBY")) for x in flat[:cut])


def test_others_prefilled_from_description_next_14_days(conn):
    desc = ("Notes about the db\nUpcoming Holidays and Leaves\n"
            "• Nov 20 - Oto on leave\n- Nov 30 Bonifacio Day\n• Nov 17-18: Rex leave\n• Nov 2 Old holiday")
    n, _ = draft(conn, description=desc)
    t = texts(all_blocks(n))
    flat = [x for _, x in t]
    i = flat.index("⚫️ Others")
    assert flat[i + 1:i + 4] == ["Leaves and holidays (next 14 days)", "Nov 20 - Oto on leave", "Nov 17-18: Rex leave"]
    assert "Heavy workloads" in flat and "Reminders" in flat
    assert "Bonifacio" not in " ".join(flat)


def test_header_attendees_copied_from_last_week_and_footer_mentions(conn):
    last = meeting_page("m0", "PSR MOM 2026-11-09", "2026-11-09", [("u1", "Quilla"), ("u2", "Oto")])
    n, _ = draft(conn, meetings=[last])
    assert n.created[0]["properties"]["Team Attendees"]["people"] == [
        {"object": "user", "id": "u1"}, {"object": "user", "id": "u2"}]
    flat = [x for _, x in texts(all_blocks(n))]
    assert "Attendees: Quilla, Oto" in flat
    assert "@quil Oto" in flat  # Oto has no handle: name is used


def test_header_present(conn):
    n, _ = draft(conn)
    t = texts(all_blocks(n))
    assert t[:2] == [("heading_1", "PSR MOM"), ("paragraph", "Monday, Nov 16, 2026")]


def test_long_pages_are_chunked_to_100_blocks(conn):
    many = [project(f"x{i}", f"X{i}", f"X{i:03d}") for i in range(130)]
    run_snapshot(conn, FakeNotion(many, []), PROJECTS, CONTACTS, "friday", FRIDAY + timedelta(hours=1))
    run_snapshot(conn, FakeNotion(many, []), PROJECTS, CONTACTS, "monday", MONDAY + timedelta(days=7))
    n = FakeNotion(many, contacts())
    run_draft_mom(conn, n, PROJECTS, CONTACTS, MEETING, date(2026, 11, 23), log=lambda _: None)
    assert len(n.created[0]["children"]) == 100 and n.appended


# ---- rerun / sent ----

def test_refuses_to_overwrite_sent_page(conn):
    sent = meeting_page("m1", "PSR MOM 2026-11-16", "2026-11-16", sent_at="2026-11-16T05:00:00Z")
    n = FakeNotion(pages(True), contacts(), [sent])
    with pytest.raises(MomAlreadySent):
        run_draft_mom(conn, n, PROJECTS, CONTACTS, MEETING, D, log=lambda _: None)
    assert not n.created and not n.trashed


def test_rerun_replaces_unsent_draft(conn):
    old = meeting_page("m1", "PSR MOM 2026-11-16", "2026-11-16")
    n, _ = draft(conn, meetings=[old])
    assert n.trashed == ["m1"] and len(n.created) == 1


def test_no_pair_yet_creates_nothing():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    run_snapshot(c, FakeNotion(pages(True), []), PROJECTS, CONTACTS, "monday", MONDAY)
    n = FakeNotion(pages(True), contacts())
    assert run_draft_mom(c, n, PROJECTS, CONTACTS, MEETING, D, log=lambda _: None).page_id is None
    assert not n.created


def test_mom_pair_uses_monday_on_or_before_date_and_friday_before_it(conn):
    assert mom_diff_pair(conn, D) == (1, 2)
    assert mom_diff_pair(conn, date(2026, 11, 15)) is None


# ---- CLI ----

def cli_setup(tmp_path, conn_path, meeting=True, monkeypatch=None):
    cfg = {"projects_data_source": "11111111-1111-1111-1111-111111111111",
           "contacts_data_source": "22222222-2222-2222-2222-222222222222",
           "database_path": str(conn_path)}
    if meeting:
        cfg["meeting_notes_data_source"] = "11111111-1111-1111-1111-111111111111"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    return str(path)


class Routed(FakeNotion):
    """FakeNotion that answers to the ids in the CLI config."""
    def query_data_source(self, ds, filter=None):
        return super().query_data_source({"11111111-1111-1111-1111-111111111111": PROJECTS,
                                          "22222222-2222-2222-2222-222222222222": CONTACTS,
                                          "11111111-1111-1111-1111-111111111111": MEETING}[ds])

    def create_page(self, ds, properties, children):
        return super().create_page(MEETING, properties, children)


def file_db(tmp_path):
    path = tmp_path / "psr.db"
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    run_snapshot(c, FakeNotion(pages(False), []), PROJECTS, CONTACTS, "friday", FRIDAY)
    run_snapshot(c, FakeNotion(pages(True), []), PROJECTS, CONTACTS, "monday", MONDAY)
    c.close()
    return path


def test_draft_mom_cli_date_and_refusal(tmp_path, monkeypatch, capsys):
    cfg = cli_setup(tmp_path, file_db(tmp_path), monkeypatch=monkeypatch)
    n = Routed(pages(True), contacts())
    assert main(["draft-mom", "--config", cfg, "--date", "2026-11-16"], notion=n) == 0
    assert n.created[0]["properties"]["Meeting Date"]["date"]["start"] == "2026-11-16"
    n.meetings.append(meeting_page("m1", "PSR MOM 2026-11-16", "2026-11-16", sent_at="2026-11-16T05:00:00Z"))
    assert main(["draft-mom", "--config", cfg, "--date", "2026-11-16"], notion=n) == 1
    assert len(n.created) == 1
    assert "already sent" in capsys.readouterr().err


def test_draft_mom_needs_meeting_notes_config(tmp_path, monkeypatch, capsys):
    cfg = cli_setup(tmp_path, file_db(tmp_path), meeting=False, monkeypatch=monkeypatch)
    assert main(["draft-mom", "--config", cfg], notion=FakeNotion()) == 2
    assert "meeting_notes_data_source" in capsys.readouterr().err


# ---- scheduler ----

def test_scheduler_drafts_right_after_monday_snapshot(tmp_path, monkeypatch):
    cfg = load_config(cli_setup(tmp_path, tmp_path / "psr.db", monkeypatch=monkeypatch))
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    n = Routed(pages(False), contacts())
    now = [datetime(2026, 11, 13, 17, 59, tzinfo=MANILA)]

    def sleep(seconds):
        now[0] += timedelta(seconds=seconds)

    order = []
    orig_create = n.create_page
    n.create_page = lambda *a: (order.append("draft"), orig_create(*a))[1]
    job = make_job(conn, n, cfg, None, lambda: now[0])
    wrapped = lambda kind: (job(kind), order.append(kind))
    end = datetime(2026, 11, 16, 12, tzinfo=MANILA)

    def swap_to_monday_data(kind):
        if kind == "friday":
            n.projects = pages(True)
        wrapped(kind)

    Scheduler(swap_to_monday_data, lambda: now[0], sleep, log=lambda _: None).run(stop=lambda: now[0] >= end)
    assert order == ["friday", "draft", "monday"]  # the draft happens inside the monday job
    assert len(n.created) == 1
    assert n.created[0]["properties"]["Name"]["title"][0]["text"]["content"] == "PSR MOM 2026-11-16"


# ---- holidays parser ----

def test_holidays_window_and_year_wrap():
    d = date(2026, 12, 28)
    got = upcoming_entries("Upcoming Holidays and Leaves\n- Dec 30 Rizal Day\n- Dec 31 - Jan 2: Oto\n- Jan 20 later\n- no date here", d)
    assert got == ["Dec 30 Rizal Day", "Dec 31 - Jan 2: Oto"]


def test_failed_create_keeps_the_old_unsent_draft(conn):
    old = meeting_page("m1", "PSR MOM 2026-11-16", "2026-11-16")
    n = FakeNotion(pages(True), contacts(), [old])
    n.create_page = lambda *a: (_ for _ in ()).throw(RuntimeError("notion down"))
    with pytest.raises(RuntimeError):
        run_draft_mom(conn, n, PROJECTS, CONTACTS, MEETING, D, log=lambda _: None)
    assert n.trashed == []
