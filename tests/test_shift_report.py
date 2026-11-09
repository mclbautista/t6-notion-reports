import re
from datetime import datetime, timedelta, timezone

import pytest

from fake_notion import (CONTACTS, JOB_ORDERS, PROJECTS, FakeNotion, contact, date, job_order, project,
                         relation, rich, select, status, formula)
from html_check import assert_valid_html
from t6_notion_reports import cli
from t6_notion_reports.jo_snapshot_job import run_jo_snapshot
from t6_notion_reports.jo_snapshots import connect
from t6_notion_reports.shift_job import run_shift_report

MNL = timezone(timedelta(hours=8))
PREV_1900 = datetime(2026, 11, 2, 19, tzinfo=MNL)
AM = datetime(2026, 11, 3, 5, tzinfo=MNL)
MID = datetime(2026, 11, 3, 11, 30, tzinfo=MNL)
PM = datetime(2026, 11, 3, 19, tzinfo=MNL)
NEXT_AM = datetime(2026, 11, 4, 5, tzinfo=MNL)


def at(day, hour, minute=0):
    return f"2026-11-{day:02d}T{hour:02d}:{minute:02d}:00.000+08:00"


def jo(page_id, number="48", dept="AE", st="Pending", deadline=at(3, 18), posted=at(2, 9), edited="2026-11-03T01:00:00.000Z",
       health="🔵 On Track", task="Export", **props):
    base = {
        "JO Number": rich(number), "Task": rich(task), "Department": select(dept), "Status": status(st),
        "Deadline": date(deadline), "Date": date(posted), "Health": formula(health),
    }
    base.update(props)
    return job_order(page_id, edited=edited, **base)


PEOPLE = [contact("c1", "Oto"), contact("c2", "Rex")]
PROJS = [project("pr1", name="Fake Expo", code="FAKEX")]


def world(*jos):
    return FakeNotion(projects=PROJS, contacts=PEOPLE, job_orders=jos)


def run(conn, fake, slot, when, banner=False):
    out, log = [], []
    report = run_shift_report(conn, fake, JOB_ORDERS, CONTACTS, PROJECTS, slot, when, banner, out.append, log.append)
    return report, "\n".join(out), log


def prior(conn, fake=None, when=MID, kind="1130"):
    """An earlier snapshot, so the next one is not the baseline."""
    run_jo_snapshot(conn, fake or world(), JOB_ORDERS, CONTACTS, PROJECTS, kind, when)


def headings(text):
    return re.findall(r"^<b>(.+?)</b>$", text, re.M)


class FakeSummarizer:
    def __init__(self, replies=None):
        self.replies, self.calls = replies or {}, []

    def summarize(self, task_text):
        self.calls.append(task_text)
        return self.replies.get(task_text, "Short label")


def blocks_of(text, heading):
    """The lines under a bold heading, up to the next blank line."""
    return text.split(f"<b>{heading}</b>\n")[1].split("\n\n")[0].splitlines()


def test_headers_and_section_order_per_slot():
    conn = connect()
    prior(conn)
    fake = world(*(jo(f"d{i}", str(i), d) for i, d in enumerate(
        ["VFX", "OE", "SOUND", "COLOR", "ADMIN", "COOR", "Zed", None, "AE", "TOC"], 1)))
    _, text, _ = run(conn, fake, "1900", PM)
    assert headings(text) == [
        "END OF SHIFT REPORT (DAY SHIFT)", "🔵 For TOC (Night Shift)", "🟢 For AE", "⚫️ For OE", "🟡 For COLOR",
        "🟠 For SOUND", "🟤 For VFX", "🟣 For COOR &amp; ADMIN", "Zed", "🔴 No Department", "✅ Since 11:30 report"]
    assert text.splitlines()[2] == "Tue Nov 3 / Tuesday (19:00)"
    prior_night = connect()
    prior(prior_night, world(), PM, "1900")
    _, text, _ = run(prior_night, world(), "0500", NEXT_AM)
    assert headings(text) == ["END OF SHIFT REPORT (NIGHT SHIFT)", "🔵 For TOC (Day Shift)", "🟢 For AE",
                              "✅ Since 19:00 report"]
    assert text.splitlines()[2] == "Wed Nov 4 / Wednesday (05:00)"


def test_toc_ae_and_tally_always_appear_others_left_out_when_empty_or_finished():
    conn = connect()
    prior(conn)
    fake = world(jo("d1", "8", "COLOR", st="Done"), jo("x1", "9", "ADMIN", st="Cancelled"))
    _, text, _ = run(conn, fake, "1900", PM)
    assert blocks_of(text, "🔵 For TOC (Night Shift)") == ["none"]
    assert blocks_of(text, "🟢 For AE") == ["none"]
    assert "COLOR" not in text.split("Since 11:30")[0] and "ADMIN" not in text
    assert "✅ Since 11:30 report" in text and "Last JO edit" in text


def test_blocks_alphabetical_unassigned_last_shared_jo_in_each_block_and_deadline_order():
    conn = connect()
    prior(conn)
    fake = world(
        jo("s1", "1", "AE", deadline=at(4, 9), Assigned=relation("c2", "c1")),
        jo("s2", "2", "AE", deadline=None, Assigned=relation("c1")),
        jo("s3", "3", "AE", deadline=at(3, 20), Assigned=relation("c1")),
        jo("s4", "4", "AE", Assigned=relation()),
    )
    _, text, _ = run(conn, fake, "1900", PM)
    ae = blocks_of(text, "🟢 For AE")
    names = [l for l in ae if "<a " not in l]
    assert names == ["Oto", "Rex", "Unassigned"]
    oto = ae[1:ae.index("Rex")]
    assert [re.search(r"#(\d+)", l).group(1) for l in oto] == ["3", "1", "2"]
    assert oto[1].endswith("Wed Nov 4 09:00 - also: Rex")
    assert ae[ae.index("Rex") + 1].endswith("- also: Oto")
    assert not ae[-1].count("also")


def test_line_format_health_emoji_link_and_escaping():
    conn = connect()
    prior(conn)
    fake = world(
        jo("abcd-1234", "48", "TOC", st="In progress", health="🔥 Urgent", task="Export <v2> & sons",
           Projects=relation("pr1")),
        jo("b1", "49", "TOC", health="🔴 Blocked"), jo("b2", "50", "TOC", health="⚠️ Delayed"),
        jo("b3", "51", "TOC", health="🔵 On Track"), jo("b4", "52", "TOC", health="🟡 At Risk"),
    )
    summarizer = FakeSummarizer({"Export <v2> & sons": "Export <v2> &"})
    out, log = [], []
    run_shift_report(conn, fake, JOB_ORDERS, CONTACTS, PROJECTS, "1900", PM, False, out.append, log.append,
                     summarizer=summarizer)
    text = "\n".join(out)
    lines = [l for l in text.splitlines() if "notion.so" in l]
    assert lines[0] == ('🔥 Urgent <a href="https://www.notion.so/abcd1234">FAKEX #48</a> - Export &lt;v2&gt; &amp; - '
                        "In progress - Tue Nov 3 18:00")
    assert lines[1].startswith('🔴 Blocked <a ') and lines[2].startswith('⚠️ Delayed <a ')
    assert lines[3].startswith('<a ') and lines[4].startswith('<a ')
    assert_valid_html(text)


def test_jo_id_fallbacks():
    conn = connect()
    prior(conn)
    fake = FakeNotion(projects=PROJS + [project("pr2", name="Second Show", code="")], contacts=PEOPLE, job_orders=[
        jo("j1", "1", "TOC", Projects=relation("pr1", "pr2")),
        jo("j2", "2", "TOC"),
        jo("j3", "", "TOC", Projects=relation("pr1")),
        jo("j4", "4", "TOC", Projects=relation("pr2")),
    ])
    _, text, _ = run(conn, fake, "1900", PM)
    ids = re.findall(r">([^<]+)</a>", text)
    assert ids == ["FAKEX/Second Show #1", "no project #2", "Second Show #4", "FAKEX #?"]


def test_task_comes_from_the_summarizer_with_fallbacks():
    conn = connect()
    prior(conn)
    fake = world(
        jo("j1", "1", "TOC", task="Color grade the final cut"),
        jo("j2", "2", "TOC", task="Subtitle QC pass for episode"),
        jo("j3", "3", "TOC", task="Master delivery check"),
        jo("j4", "4", "TOC", task=""),
        jo("j5", "5", "TOC", task="Conform timeline again"),
    )
    summarizer = FakeSummarizer({"Color grade the final cut": "Color grade",
                                 "Subtitle QC pass for episode": "one two three four",
                                 "Master delivery check": None})
    out = []
    run_shift_report(conn, fake, JOB_ORDERS, CONTACTS, PROJECTS, "1900", PM, False, out.append, lambda _: None,
                     summarizer=summarizer)
    tasks = re.findall(r"</a> - (.+?) - Pending", "\n".join(out))
    assert tasks == ["Color grade", "Subtitle QC pass", "Master delivery check", "no task", "Short label"]
    assert "" not in summarizer.calls and len(summarizer.calls) == 4


def test_default_summarizer_is_the_first_three_words():
    conn = connect()
    prior(conn)
    _, text, _ = run(conn, world(jo("j1", "1", "TOC", task="Color grade the final cut")), "1900", PM)
    assert "</a> - Color grade the - Pending" in text


def test_empty_relations_and_missing_deadline():
    conn = connect()
    prior(conn)
    _, text, _ = run(conn, world(jo("j1", "5", "TOC", deadline=None)), "1900", PM)
    assert "no project #5</a> - Export - Pending - no deadline" in text
    assert "Unassigned\n" in text


def test_window_counts_per_department_and_total():
    conn = connect()
    # Older (11:30) snapshot: j1 Pending, j2 In progress, j3 Needs Intake Review, j5 Done already.
    prior(conn, world(
        jo("j1", "1", "AE", st="Pending", posted=at(2, 9)),
        jo("j2", "2", "AE", st="In progress", posted=at(2, 9)),
        jo("j3", "3", "VFX", st="Needs Intake Review", posted=at(2, 9)),
        jo("j5", "5", "AE", st="Done", posted=at(2, 9)),
        jo("j6", "6", "OE", st="Pending", posted=at(2, 9)),
        jo("j7", "7", "COOR", st="Pending", posted=at(2, 9)),
    ), MID, "1130")
    fake = world(
        jo("j1", "1", "AE", st="Done", posted=at(2, 9)),                       # processed + accomplished
        jo("j2", "2", "AE", st="Needs Completion Review", posted=at(2, 9)),    # already started: nothing
        jo("j3", "3", "VFX", st="In progress", posted=at(2, 9)),               # processed
        jo("j4", "4", "AE", st="Done", posted=at(3, 12)),                      # received + finished in the window: all three
        jo("j5", "5", "AE", st="Done", posted=at(2, 9)),                       # done before: nothing
        jo("j6", "6", "OE", st="Cancelled", posted=at(3, 12)),                 # cancelled: nowhere
        jo("j7", "7", "COOR", st="Pending", posted=at(2, 9)),                  # all zero: left out
        jo("j8", "8", None, st="Pending", posted=at(3, 13)),                   # received, no department
        jo("j9", "9", "TOC", st="Pending", posted=at(3, 19, 30)),              # after the snapshot: not in the window
    )
    report, text, _ = run(conn, fake, "1900", PM)
    breakdown = blocks_of(text, "✅ Since 11:30 report")
    assert breakdown == [
        "AE: received 1 · processed 2 · accomplished 2",
        "VFX: received 0 · processed 1 · accomplished 0",
        "No department: received 1 · processed 0 · accomplished 0",
        "<b>Total</b>: received 2 · processed 3 · accomplished 2",
    ]
    tally = text.split("Since 11:30 report")[1]
    assert "ADMIN" not in tally and "OE:" not in tally and "COOR:" not in tally
    assert (report.counts.received, report.counts.processed, report.counts.accomplished) == (2, 3, 2)


def test_baseline_reports_received_only_with_a_note():
    conn = connect()
    fake = world(jo("j1", "1", "AE", st="Done", posted=at(3, 12)), jo("j2", "2", "VFX", posted=at(3, 13)))
    report, text, log = run(conn, fake, "1900", PM)
    assert "First snapshot: processed and accomplished start next time." in text
    window = text.split("<b>✅ Since 11:30 report</b>\n")[1]
    assert "processed" not in window.split("Last JO edit")[0].replace("start next time", "").replace("processed and accomplished", "")
    assert "AE: received 1" in window and "VFX: received 1" in window and "<b>Total</b>: received 2" in window
    assert "baseline=True" in log[0]
    assert report.counts.processed == report.counts.accomplished == 0


def test_fallback_comparison_note_when_bounding_snapshot_is_missing():
    conn = connect()
    prior(conn, world(jo("j1", "1", "AE", st="Pending")), PREV_1900, "1900")  # no 11:30 snapshot today
    fake = world(jo("j1", "1", "AE", st="Done"))
    _, text, _ = run(conn, fake, "1900", PM)
    assert "Compared with 19:00 on Mon Nov 2" in text
    assert "accomplished 1" in text


def test_no_note_when_the_expected_snapshot_exists():
    conn = connect()
    prior(conn, world(), PREV_1900, "1900")
    prior(conn, world(), AM, "0500")
    prior(conn, world(), MID, "1130")
    _, text, _ = run(conn, world(), "1900", PM)
    assert "Compared with" not in text and "Since 11:30 report" in text
    prior_night = connect()
    prior(prior_night, world(), AM, "0500")
    prior(prior_night, world(), PM, "1900")
    _, text, _ = run(prior_night, world(), "0500", NEXT_AM)
    assert "Compared with" not in text and "Since 19:00 report" in text


def test_footer_is_newest_edit_among_the_jos_counted():
    conn = connect()
    prior(conn, world(jo("old", "1", "AE", st="Done", edited="2026-11-03T20:00:00.000Z")), MID, "1130")
    fake = world(
        jo("old", "1", "AE", st="Done", edited="2026-11-03T20:00:00.000Z"),   # unchanged and not counted
        jo("open", "2", "AE", st="Pending", edited="2026-11-03T02:30:00.000Z"),  # unfinished
        jo("new", "3", "VFX", st="Done", posted=at(3, 10), edited="2026-11-03T08:15:00.000Z"),  # counted
        jo("gone", "4", "OE", st="Cancelled", edited="2026-11-03T09:59:00.000Z"),  # cancelled: ignored
    )
    _, text, _ = run(conn, fake, "1900", PM)
    assert text.splitlines()[-1] == "Last JO edit: Tue Nov 3 16:15"


def test_long_report_splits_into_valid_parts_with_banner():
    conn = connect()
    prior(conn)
    jos = [jo(f"p{i}", str(i), "AE", task=f"Task <{i}> & " + "long " * 20, deadline=at(3, 18))
           for i in range(1, 120)]
    report, text, log = run(conn, world(*jos), "1900", PM, banner=True)
    assert len(report.messages) > 1
    for part in report.messages:
        assert len(part) <= 4096
        assert part.startswith("🧪 TEST\n")
        assert_valid_html(part)
    assert f"parts={len(report.messages)}" in log[0]
    assert report.messages[-1].endswith("Last JO edit: Tue Nov 3 09:00")


def test_run_logs_one_counts_line_with_no_text():
    conn = connect()
    prior(conn)
    _, _, log = run(conn, world(jo("j1", "1", "AE", task="Secret Task")), "1900", PM)
    assert len(log) == 1 and "Secret Task" not in log[0]
    assert re.fullmatch(r"shift slot=1900 snapshot=\d+ baseline=False unfinished=1 received=\d+ processed=\d+ "
                        r"accomplished=\d+ parts=1", log[0])


# ---- CLI ----------------------------------------------------------------------------------------

JO_ID = "33333333-3333-3333-3333-333333333333"


def cli_config(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "ntn_supersecret")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f'projects_data_source: "{"1" * 8}-1111-1111-1111-{"1" * 12}"\n'
                   f'contacts_data_source: "{"2" * 8}-2222-2222-2222-{"2" * 12}"\n'
                   f'jo_data_source: "{JO_ID}"\ndatabase_path: "{tmp_path / "psr.db"}"\n')
    return cfg


class RoutedNotion(FakeNotion):
    def query_data_source(self, data_source_id, filter=None):
        route = {JO_ID: JOB_ORDERS, "1" * 8 + "-1111-1111-1111-" + "1" * 12: PROJECTS,
                 "2" * 8 + "-2222-2222-2222-" + "2" * 12: CONTACTS}
        return super().query_data_source(route[data_source_id], filter)


def test_cli_dry_print_prints_and_sends_nothing(tmp_path, monkeypatch, capsys):
    cfg = cli_config(tmp_path, monkeypatch)
    fake = RoutedNotion(projects=PROJS, contacts=PEOPLE, job_orders=[jo("j1", "48", "TOC")])
    # Nothing but Notion reads is possible here: no gateway client exists on this path.
    monkeypatch.setattr("t6_notion_reports.cli.GatewayClient", lambda *a, **k: pytest.fail("must not send"))
    assert cli.main(["shift-report", "--slot", "1900", "--dry-print", "--config", str(cfg)], notion=fake, now=PM) == 0
    captured = capsys.readouterr()
    assert "For TOC (Night Shift)" in captured.out and "no project #48" in captured.out
    assert captured.err.startswith("shift slot=1900 ") and "baseline=True" in captured.err
    assert not (fake.created or fake.appended or fake.trashed or fake.sent_marked)


def test_cli_send_needs_shift_enabled_and_rejects_unknown_slots(tmp_path, monkeypatch, capsys):
    cfg = cli_config(tmp_path, monkeypatch)
    assert cli.main(["shift-report", "--slot", "1900", "--config", str(cfg)]) == 2
    with pytest.raises(SystemExit):
        cli.main(["shift-report", "--slot", "0900", "--dry-print", "--config", str(cfg)])


# ---- slots moved to 11:30 and 19:00 (#74) ------------------------------------------------------

OLD_SCHEMA = """
CREATE TABLE jo_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at    TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('0500', '1200', '1800', 'manual')),
    is_baseline INTEGER NOT NULL DEFAULT 0
);
"""


def test_existing_database_keeps_old_kinds_and_accepts_the_new_ones(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(OLD_SCHEMA)
    old.execute("INSERT INTO jo_snapshots (taken_at, kind, is_baseline) VALUES ('2026-11-02T10:00:00+00:00', '1800', 0)")
    old.commit()
    old.close()
    conn = connect(path)
    assert conn.execute("SELECT kind FROM jo_snapshots").fetchall() == [("1800",)]
    prior(conn, world(jo("j1")), AM, "0500")
    prior(conn, world(jo("j1")), PM, "1900")
    prior(conn, world(jo("j1")), AM + timedelta(minutes=390), "1130")
    assert sorted(k for (k,) in conn.execute("SELECT kind FROM jo_snapshots")) == ["0500", "1130", "1800", "1900"]
    assert connect(path).execute("SELECT count(*) FROM jo_snapshots").fetchone() == (4,)  # a second connect is a no-op


def test_first_0500_report_after_the_move_falls_back_to_the_old_1800_snapshot():
    conn = connect()
    prior(conn, world(jo("j1", "1", "AE", st="Pending")), datetime(2026, 11, 3, 18, tzinfo=MNL), "1800")
    _, text, _ = run(conn, world(jo("j1", "1", "AE", st="Done")), "0500", NEXT_AM)
    assert "Compared with 18:00 on Tue Nov 3" in text and "Since 19:00 report" in text


def test_0500_report_compares_with_the_previous_1900_snapshot_without_a_note():
    conn = connect()
    prior(conn, world(), PM, "1900")
    _, text, _ = run(conn, world(), "0500", NEXT_AM)
    assert "Compared with" not in text


def test_cli_accepts_the_new_slots_and_rejects_the_retired_ones(tmp_path, monkeypatch, capsys):
    cfg = cli_config(tmp_path, monkeypatch)
    for slot in ("0500", "1130", "1900"):
        fake = RoutedNotion(projects=PROJS, contacts=PEOPLE, job_orders=[jo("j1", "48", "TOC")])
        assert cli.main(["shift-report", "--slot", slot, "--dry-print", "--config", str(cfg)], notion=fake, now=PM) == 0
    capsys.readouterr()
    for slot in ("1200", "1800"):
        with pytest.raises(SystemExit):
            cli.main(["shift-report", "--slot", slot, "--dry-print", "--config", str(cfg)])


def test_baseline_windows_open_at_1130_for_the_1900_report_and_1900_yesterday_for_0500():
    from t6_notion_reports.shift_job import nominal_window_start
    conn = connect()
    run_jo_snapshot(conn, world(), JOB_ORDERS, CONTACTS, PROJECTS, "1900", PM)
    from t6_notion_reports.jo_snapshots import load_latest
    assert nominal_window_start("1900", load_latest(conn)) == MID
    assert nominal_window_start("0500", load_latest(conn)) == datetime(2026, 11, 2, 19, tzinfo=MNL)


# ---- new turnover layout (#75) -----------------------------------------------------------------

def test_tally_counts_coor_and_admin_separately_and_windows_run_report_to_report():
    conn = connect()
    prior(conn, world(), PM, "1900")
    fake = world(jo("c1", "1", "COOR", posted=at(3, 20)), jo("a1", "2", "ADMIN", posted=at(3, 21)),
                 jo("old", "3", "COOR", posted=at(3, 18)))  # received before the 19:00 snapshot: outside
    _, text, _ = run(conn, fake, "0500", NEXT_AM)
    assert blocks_of(text, "✅ Since 19:00 report") == [
        "COOR: received 1 · processed 0 · accomplished 0",
        "ADMIN: received 1 · processed 0 · accomplished 0",
        "<b>Total</b>: received 2 · processed 0 · accomplished 0",
    ]
    assert "🟣 For COOR &amp; ADMIN" in text


# ---- 11:30 report on the new layout (#76) ------------------------------------------------------

def test_1130_header_sections_and_every_unfinished_jo_listed():
    conn = connect()
    prior(conn, world(), AM, "0500")
    fake = world(
        jo("n1", "1", "AE", st="Pending"), jo("n2", "2", "AE", st="In progress"),
        jo("n3", "3", "AE", st="Needs Completion Review"), jo("n4", "4", "AE", st="Done"),
        jo("n5", "5", "TOC", st="Cancelled"), jo("n6", "6", "OE", st="Needs Intake Review"))
    _, text, _ = run(conn, fake, "1130", MID)
    assert headings(text) == ["MID SHIFT DAY REPORT", "🔵 For TOC (Day Shift)", "🟢 For AE", "⚫️ For OE",
                              "✅ Since 05:00 report"]
    assert text.splitlines()[2] == "Tue Nov 3 / Tuesday (11:30)"
    ae = "\n".join(blocks_of(text, "🟢 For AE"))
    assert all(f"#{n}<" in ae for n in "123") and "#4<" not in ae and "#5<" not in text
    assert "#6<" in "\n".join(blocks_of(text, "⚫️ For OE"))
    assert blocks_of(text, "🔵 For TOC (Day Shift)") == ["none"]


def test_1130_has_no_carried_over_split_or_header_counts():
    conn = connect()
    prior(conn, world(), AM, "0500")
    _, text, _ = run(conn, world(jo("n1", "1", "AE", posted=at(3, 9))), "1130", MID)
    for gone in ("Carried over", "Came in this morning", "Active JOs", "midshift"):
        assert gone not in text


def test_1130_tally_counts_from_the_0500_snapshot_of_that_day():
    conn = connect()
    prior(conn, world(jo("a", "1", "AE", st="Pending", posted=at(2, 9)),
                      jo("b", "2", "OE", st="Pending", posted=at(2, 9))), AM, "0500")
    fake = world(jo("a", "1", "AE", st="Done", posted=at(2, 9)),
                 jo("b", "2", "OE", st="In progress", posted=at(2, 9)),
                 jo("c", "3", "AE", st="Pending", posted=at(3, 8)),
                 jo("old", "4", "AE", st="Pending", posted=at(3, 4)))  # received before 05:00: outside
    _, text, _ = run(conn, fake, "1130", MID)
    assert blocks_of(text, "✅ Since 05:00 report") == [
        "AE: received 1 · processed 1 · accomplished 1",
        "OE: received 0 · processed 1 · accomplished 0",
        "<b>Total</b>: received 1 · processed 2 · accomplished 1",
    ]
    assert "Compared with" not in text


def test_1130_baseline_and_fallback_notes():
    conn = connect()
    _, text, _ = run(conn, world(jo("a", "1", "AE", posted=at(3, 6))), "1130", MID)
    assert "First snapshot: processed and accomplished start next time." in text
    assert "AE: received 1" in text  # window opens at 05:00 today
    conn = connect()
    prior(conn, world(), PREV_1900, "1900")  # no 05:00 snapshot today
    _, text, _ = run(conn, world(), "1130", MID)
    assert "Compared with 19:00 on Mon Nov 2" in text


def test_1900_tally_opens_from_the_1130_snapshot_when_it_exists():
    conn = connect()
    prior(conn, world(jo("a", "1", "AE", st="Pending")), MID, "1130")
    _, text, _ = run(conn, world(jo("a", "1", "AE", st="Done")), "1900", PM)
    assert "✅ Since 11:30 report" in text and "Compared with" not in text
    assert "AE: received 0 · processed 1 · accomplished 1" in text


def test_1130_baseline_window_start_is_0500_today():
    from t6_notion_reports.jo_snapshots import load_latest
    from t6_notion_reports.shift_job import nominal_window_start
    conn = connect()
    run_jo_snapshot(conn, world(), JOB_ORDERS, CONTACTS, PROJECTS, "1130", MID)
    assert nominal_window_start("1130", load_latest(conn)) == AM


def test_1130_html_banner_and_splitting_still_hold():
    jos = [jo(f"p{i}", str(i), "AE", task=f"Task <{i}> & " + "long " * 20) for i in range(1, 120)]
    report, _, log = run(connect(), world(*jos), "1130", MID, banner=True)
    assert len(report.messages) > 1
    for part in report.messages:
        assert len(part) <= 4096 and part.startswith("🧪 TEST\n")
        assert_valid_html(part)
    assert "slot=1130" in log[0] and "baseline=True" in log[0]
