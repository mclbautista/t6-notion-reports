import re
from datetime import datetime, timedelta, timezone

import pytest

from fake_notion import (CONTACTS, JOB_ORDERS, PROJECTS, FakeNotion, contact, date, job_order, project,
                         relation, rich, select, status, formula)
from html_check import assert_valid_html
from t6_psr_brief import cli
from t6_psr_brief.jo_snapshot_job import run_jo_snapshot
from t6_psr_brief.jo_snapshots import connect
from t6_psr_brief.shift_job import run_shift_report

MNL = timezone(timedelta(hours=8))
PREV_1800 = datetime(2026, 11, 2, 18, tzinfo=MNL)
AM = datetime(2026, 11, 3, 5, tzinfo=MNL)
PM = datetime(2026, 11, 3, 18, tzinfo=MNL)
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


PEOPLE = [contact("c1", "Pio"), contact("c2", "Tim")]
PROJS = [project("pr1", name="Twenty Beauty", code="TWBTY")]


def world(*jos):
    return FakeNotion(projects=PROJS, contacts=PEOPLE, job_orders=jos)


def run(conn, fake, slot, when, banner=False):
    out, log = [], []
    report = run_shift_report(conn, fake, JOB_ORDERS, CONTACTS, PROJECTS, slot, when, banner, out.append, log.append)
    return report, "\n".join(out), log


def prior(conn, fake=None, when=PREV_1800, kind="1800"):
    """An earlier snapshot, so the next one is not the baseline."""
    run_jo_snapshot(conn, fake or world(), JOB_ORDERS, CONTACTS, PROJECTS, kind, when)


def headings(text):
    return re.findall(r"^<b>(.+?)</b>$", text, re.M)


def test_1800_department_lists_order_none_and_left_out():
    conn = connect()
    prior(conn)
    fake = world(
        jo("a1", "1", "AE", deadline=at(4, 12)),
        jo("a2", "2", "AE", deadline=None),
        jo("a3", "3", "AE", deadline=at(3, 20)),
        jo("v1", "4", "VFX"), jo("o1", "5", "OE"), jo("s1", "6", "SOUND"),
        jo("n1", "7", None),
        jo("d1", "8", "COLOR", st="Done"), jo("x1", "9", "ADMIN", st="Cancelled"),
    )
    _, text, _ = run(conn, fake, "1800", PM)
    assert headings(text)[:6] == ["Day shift end of shift · Tue Nov 3", "TOC tonight", "AE tomorrow", "OE", "VFX", "SOUND"]
    assert "COLOR" not in text.split("Day shift window")[0] and "ADMIN" not in text.split("Day shift window")[0]
    assert "No department" in headings(text) and headings(text).index("No department") == 6
    toc, ae = text.split("<b>TOC tonight</b>\n")[1].split("\n\n")[0], text.split("<b>AE tomorrow</b>\n")[1].split("\n\n")[0]
    assert toc == "none"
    # Sorted by deadline, no deadline last.
    numbers = re.findall(r"JO #(\d+)", ae)
    assert numbers == ["3", "1", "2"]
    assert "no deadline" in ae.splitlines()[-1]


def test_0500_headings_and_empty_report_says_none():
    conn = connect()
    prior(conn)
    _, text, _ = run(conn, world(), "0500", AM)
    assert headings(text)[:3] == ["Night shift end of shift · Tue Nov 3", "TOC to day shift", "AE"]
    assert text.count("\nnone\n") == 2
    assert "Last JO edit: none" in text


def test_unfinished_line_fields_link_and_deadline_format():
    conn = connect()
    prior(conn)
    fake = world(jo("abcd-1234", "48", "TOC", st="In progress", health="🔥 Urgent", task="Export <v2> & sons",
                    Assigned=relation("c1", "c2"), Projects=relation("pr1")))
    _, text, _ = run(conn, fake, "1800", PM)
    row = next(l for l in text.splitlines() if "JO #48" in l)
    assert row == ('<a href="https://www.notion.so/abcd1234">JO #48</a> · TWBTY · Export &lt;v2&gt; &amp; sons · '
                   "In progress · 🔥 Urgent · Tue Nov 3 18:00 · Pio, Tim")
    assert_valid_html(text)


def test_empty_relations_and_missing_deadline():
    conn = connect()
    prior(conn)
    _, text, _ = run(conn, world(jo("j1", "5", "TOC", deadline=None)), "1800", PM)
    assert "· no project · Export · Pending · 🔵 On Track · no deadline · unassigned" in text


def test_window_counts_per_department_and_total():
    conn = connect()
    # Older (05:00) snapshot: j1 Pending, j2 In progress, j3 Needs Intake Review, j5 Done already.
    prior(conn, world(
        jo("j1", "1", "AE", st="Pending", posted=at(2, 9)),
        jo("j2", "2", "AE", st="In progress", posted=at(2, 9)),
        jo("j3", "3", "VFX", st="Needs Intake Review", posted=at(2, 9)),
        jo("j5", "5", "AE", st="Done", posted=at(2, 9)),
        jo("j6", "6", "OE", st="Pending", posted=at(2, 9)),
        jo("j7", "7", "COOR", st="Pending", posted=at(2, 9)),
    ), AM, "0500")
    fake = world(
        jo("j1", "1", "AE", st="Done", posted=at(2, 9)),                       # processed + accomplished
        jo("j2", "2", "AE", st="Needs Completion Review", posted=at(2, 9)),    # already started: nothing
        jo("j3", "3", "VFX", st="In progress", posted=at(2, 9)),               # processed
        jo("j4", "4", "AE", st="Done", posted=at(3, 10)),                      # received + finished in the window: all three
        jo("j5", "5", "AE", st="Done", posted=at(2, 9)),                       # done before: nothing
        jo("j6", "6", "OE", st="Cancelled", posted=at(3, 11)),                 # cancelled: nowhere
        jo("j7", "7", "COOR", st="Pending", posted=at(2, 9)),                  # all zero: left out
        jo("j8", "8", None, st="Pending", posted=at(3, 12)),                   # received, no department
        jo("j9", "9", "TOC", st="Pending", posted=at(3, 18, 30)),              # after the snapshot: not in the window
    )
    report, text, _ = run(conn, fake, "1800", PM)
    breakdown = text.split("<b>Day shift window</b>\n")[1].split("\n\n")[0].splitlines()
    assert breakdown == [
        "AE: received 1 · processed 2 · accomplished 2",
        "VFX: received 0 · processed 1 · accomplished 0",
        "No department: received 1 · processed 0 · accomplished 0",
        "<b>Total</b>: received 2 · processed 3 · accomplished 2",
    ]
    assert "ADMIN" not in text and "OE:" not in text and "COOR:" not in text
    assert "JO #6" not in text
    assert (report.counts.received, report.counts.processed, report.counts.accomplished) == (2, 3, 2)


def test_baseline_reports_received_only_with_a_note():
    conn = connect()
    fake = world(jo("j1", "1", "AE", st="Done", posted=at(3, 10)), jo("j2", "2", "VFX", posted=at(3, 11)))
    report, text, log = run(conn, fake, "1800", PM)
    assert "First snapshot: processed and accomplished start next time." in text
    window = text.split("<b>Day shift window</b>\n")[1]
    assert "processed" not in window.split("Last JO edit")[0].replace("start next time", "").replace("processed and accomplished", "")
    assert "AE: received 1" in window and "VFX: received 1" in window and "<b>Total</b>: received 2" in window
    assert "baseline=True" in log[0]
    assert report.counts.processed == report.counts.accomplished == 0


def test_fallback_comparison_note_when_bounding_snapshot_is_missing():
    conn = connect()
    prior(conn, world(jo("j1", "1", "AE", st="Pending")), PREV_1800, "1800")  # no 05:00 snapshot today
    fake = world(jo("j1", "1", "AE", st="Done"))
    _, text, _ = run(conn, fake, "1800", PM)
    assert "Compared with 18:00 on Mon Nov 2" in text
    assert "accomplished 1" in text


def test_no_note_when_the_expected_snapshot_exists():
    conn = connect()
    prior(conn, world(), PREV_1800, "1800")
    prior(conn, world(), AM, "0500")
    _, text, _ = run(conn, world(), "1800", PM)
    assert "Compared with" not in text
    prior_night = connect()
    prior(prior_night, world(), AM, "0500")
    prior(prior_night, world(), PM, "1800")
    _, text, _ = run(prior_night, world(), "0500", NEXT_AM)
    assert "Compared with" not in text and "Night shift window" in text


def test_footer_is_newest_edit_among_the_jos_counted():
    conn = connect()
    prior(conn, world(jo("old", "1", "AE", st="Done", edited="2026-11-03T20:00:00.000Z")), AM, "0500")
    fake = world(
        jo("old", "1", "AE", st="Done", edited="2026-11-03T20:00:00.000Z"),   # unchanged and not counted
        jo("open", "2", "AE", st="Pending", edited="2026-11-03T02:30:00.000Z"),  # unfinished
        jo("new", "3", "VFX", st="Done", posted=at(3, 10), edited="2026-11-03T08:15:00.000Z"),  # counted
        jo("gone", "4", "OE", st="Cancelled", edited="2026-11-03T09:59:00.000Z"),  # cancelled: ignored
    )
    _, text, _ = run(conn, fake, "1800", PM)
    assert text.splitlines()[-1] == "Last JO edit: Tue Nov 3 16:15"


def test_long_report_splits_into_valid_parts_with_banner():
    conn = connect()
    prior(conn)
    jos = [jo(f"p{i}", str(i), "AE", task=f"Task <{i}> & " + "long " * 20, deadline=at(3, 18))
           for i in range(1, 120)]
    report, text, log = run(conn, world(*jos), "1800", PM, banner=True)
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
    _, _, log = run(conn, world(jo("j1", "1", "AE", task="Secret Task")), "1800", PM)
    assert len(log) == 1 and "Secret Task" not in log[0]
    assert re.fullmatch(r"shift slot=1800 snapshot=\d+ baseline=False unfinished=1 received=\d+ processed=\d+ "
                        r"accomplished=\d+ parts=1", log[0])


# ---- CLI ----------------------------------------------------------------------------------------

JO_ID = "3d8fb4d5-9e59-8073-a0d0-000b77632947"


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
    monkeypatch.setattr("t6_psr_brief.cli.GatewayClient", lambda *a, **k: pytest.fail("must not send"))
    assert cli.main(["shift-report", "--slot", "1800", "--dry-print", "--config", str(cfg)], notion=fake, now=PM) == 0
    captured = capsys.readouterr()
    assert "TOC tonight" in captured.out and "JO #48" in captured.out
    assert captured.err.startswith("shift slot=1800 ") and "baseline=True" in captured.err
    assert not (fake.created or fake.appended or fake.trashed or fake.sent_marked)


def test_cli_refuses_without_dry_print_and_rejects_unknown_slots(tmp_path, monkeypatch, capsys):
    cfg = cli_config(tmp_path, monkeypatch)
    assert cli.main(["shift-report", "--slot", "1800", "--config", str(cfg)]) == 2
    with pytest.raises(SystemExit):
        cli.main(["shift-report", "--slot", "1200", "--dry-print", "--config", str(cfg)])
