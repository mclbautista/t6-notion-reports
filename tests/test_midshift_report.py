import re
from datetime import datetime, timedelta, timezone

from fake_notion import FakeNotion
from html_check import assert_valid_html
from test_shift_report import PEOPLE, PROJS, at, jo, prior, run, world
from t6_notion_reports.jo_snapshots import connect
from fake_notion import relation

MNL = timezone(timedelta(hours=8))
NOON = datetime(2026, 11, 3, 11, 30, tzinfo=MNL)


def mid(*jos, banner=False, seed=True):
    conn = connect()
    if seed:
        prior(conn)
    return run(conn, world(*jos), "1130", NOON, banner)


def section(text, heading):
    return text.split(f"<b>{heading}</b>\n")[1].split("\n\n")[0].splitlines()


def numbers(rows):
    return re.findall(r">Task(\d+)<", "\n".join(rows))


def tag(n, **kw):
    kw.setdefault("task", f"Task{n}")
    return jo(f"p{n}", str(n), **kw)


def test_header_counts_from_health_exclude_cancelled_and_closed():
    _, text, _ = mid(
        tag(1, health="🔥 Urgent"), tag(2, health="⚠️ Delayed"), tag(3, health="🔴 Blocked"),
        tag(4, health="🔵 On Track"), tag(5, health="🔥 Urgent", st="Cancelled"),
        tag(6, health="🔴 Blocked", st="Done"), tag(7, health="🔥 Urgent", st="In progress"))
    assert "Active JOs: 5 · urgent 2 · delayed 1 · blocked 1" in text
    assert text.splitlines()[1] == "<b>Day shift midshift · Tue Nov 3</b>"


def test_carried_over_boundary_at_0500_and_missing_date():
    _, text, _ = mid(
        tag(1, posted=at(3, 4, 59)), tag(2, posted=at(3, 5)), tag(3, posted=None), tag(4, posted=at(3, 11)))
    assert sorted(numbers(section(text, "Carried over from night"))) == ["1", "3"]
    assert sorted(numbers(section(text, "Came in this morning"))) == ["2", "4"]


def test_only_needs_intake_review_and_pending_are_queued():
    _, text, _ = mid(tag(1, st="Needs Intake Review"), tag(2, st="Pending"), tag(3, st="In progress"),
                     tag(4, st="Needs Completion Review"), tag(5, st="Done"), tag(6, st="Cancelled"))
    queued = numbers(section(text, "Carried over from night") + section(text, "Came in this morning"))
    assert sorted(queued) == ["1", "2"]


def test_flagged_lead_each_group_blocked_delayed_urgent_then_deadline_order():
    _, text, _ = mid(
        tag(1, health="🔵 On Track", deadline=at(3, 14)),
        tag(2, health="🔥 Urgent", deadline=at(3, 13)),
        tag(3, health="⚠️ Delayed", deadline=at(3, 9)),
        tag(4, health="🔴 Blocked", deadline=at(5, 9)),
        tag(5, health="🔵 On Track", deadline=None),
        tag(6, health="🔵 On Track", deadline=at(3, 12)),
        tag(7, health="⚠️ Delayed", deadline=at(3, 8)),
        tag(8, health="⚡ Advanced", deadline=at(3, 10), posted=at(3, 9)),
    )
    assert numbers(section(text, "Carried over from night")) == ["4", "7", "3", "2", "6", "1", "5"]
    assert numbers(section(text, "Came in this morning")) == ["8"]


def test_line_fields_link_escape_and_empty_relations():
    _, text, _ = mid(jo("abcd-1234", "48", "TOC", task="Export <v2> & sons", health="🔥 Urgent",
                        Assigned=relation("c1", "c2"), Projects=relation("pr1")))
    row = next(l for l in text.splitlines() if "Export" in l)
    assert row == ('FAKEX · <a href="https://www.notion.so/abcd1234">Export &lt;v2&gt; &amp; sons</a> · TOC · '
                   "Tue Nov 3 18:00 · Oto, Rex · 🔥 Urgent")
    _, text, _ = mid(jo("j1", "1", None, deadline=None))
    assert "no project · " in text and " · no department · no deadline · unassigned · 🔵 On Track" in text
    assert_valid_html(text)


def test_empty_groups_say_none_footer_present_and_no_window_counts():
    _, text, _ = mid()
    assert text.count("\nnone\n") == 2
    assert "Active JOs: 0 · urgent 0 · delayed 0 · blocked 0" in text
    assert "Last JO edit: none" in text
    assert "received" not in text and "window" not in text.lower()


def test_footer_is_newest_edit_among_active_jos():
    _, text, _ = mid(tag(1, edited="2026-11-03T02:30:00.000Z"), tag(2, st="In progress", edited="2026-11-03T03:00:00.000Z"),
                     tag(3, st="Cancelled", edited="2026-11-03T04:00:00.000Z"))
    assert text.splitlines()[-1] == "Last JO edit: Tue Nov 3 11:00"


def test_works_as_baseline_and_splits_long_reports_with_banner():
    jos = [tag(i, task=f"Task <{i}> & " + "long " * 20) for i in range(1, 120)]
    report, text, log = mid(*jos, banner=True, seed=False)
    assert len(report.messages) > 1
    for part in report.messages:
        assert len(part) <= 4096 and part.startswith("🧪 TEST\n")
        assert_valid_html(part)
    assert "baseline=True" in log[0] and f"parts={len(report.messages)}" in log[0] and "slot=1130" in log[0]
    assert "Secret" not in log[0]
