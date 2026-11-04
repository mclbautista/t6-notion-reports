"""Aged Concerns (#46), through the brief job over several weekly runs."""
import re
from dataclasses import replace
from datetime import timedelta

from fake_notion import CONTACTS, PROJECTS, FakeNotion, date, relation
from t6_notion_reports.brief_job import run_brief
from t6_notion_reports.snapshot_job import run_snapshot
from test_flags import lines_of, staffed
from test_gated_briefs import BRIEF, FRIDAY, digest, IN_PROGRESS, SATURDAY, Gateway, contacts, dm, make_db, proj

# Submission before Playdate is a date-sanity Concern. The missing Colorist is a flag, so the
# project is listed every week and the Concern coming and going is visible.
SLIP = {"Playdate": date("2026-12-10"), "Submission Deadline": date("2026-12-01")}
LATER = {"Playdate": date("2027-01-10"), "Submission Deadline": date("2027-02-01")}  # sane, and not yet overdue
LISTED = {"Colorist": relation()}


def props(concern=True):
    return staffed(**LISTED, **(SLIP if concern else LATER))


def run(conn, weeks=0, brief=BRIEF, **kw):
    gw = Gateway()
    run_brief(conn, FakeNotion([], contacts()), CONTACTS, brief, SATURDAY + timedelta(weeks=weeks), gateway=gw, **kw)
    return gw


def setup(concern=True):
    page = proj("p1", "ONE", IN_PROGRESS, **props(concern))
    return make_db([page], [page])


def friday(conn, concern):
    run_snapshot(conn, FakeNotion([proj("p1", "ONE", IN_PROGRESS, **props(concern))], contacts()), PROJECTS, CONTACTS,
                 "friday", FRIDAY + timedelta(minutes=1))


def concern_lines(text):
    return [ln for ln in lines_of(text) if "before" in ln and not ln.startswith("Resolved")]


def resolved_lines(text):
    return [ln for ln in lines_of(text) if ln.startswith("Resolved last week")]


def test_a_concern_shows_its_first_listed_date_and_age_over_three_weekly_runs():
    conn = setup()
    ages = []
    for week in range(3):
        (line,) = concern_lines(dm(run(conn, week)))
        ages.append(re.search(r"since (\w+ \d+) \((\d+) wks?\)", line).groups())
    assert ages == [("Nov 14", "0"), ("Nov 14", "1"), ("Nov 14", "2")]
    assert "(1 wk)" in concern_lines(dm(run(conn, 1)))[0]


def test_a_concern_open_more_than_four_weeks_gets_the_marker_and_not_before():
    conn = setup()
    run(conn, 0)
    assert "chronic" not in dm(run(conn, 4))
    assert "chronic" in dm(run(conn, 5))


def test_the_chronic_threshold_is_configurable():
    conn = setup()
    run(conn, 0)
    assert "chronic" in dm(run(conn, 2, replace(BRIEF, concern_chronic_weeks=1)))


def test_a_missing_concern_shows_in_the_resolved_line_for_exactly_one_brief_then_is_gone():
    conn = setup()
    run(conn, 0)
    friday(conn, concern=False)
    second = dm(run(conn, 1))
    assert not concern_lines(second)
    (resolved,) = resolved_lines(second)
    assert "is before" in resolved and "Submission Deadline" in resolved
    assert not resolved_lines(dm(run(conn, 2)))


def test_the_resolved_line_is_one_line_per_project_however_many_concerns_resolved():
    page = proj("p1", "ONE", IN_PROGRESS, **staffed(**LISTED, **SLIP, **{"Archival Deadline": date("2026-11-20")}))
    conn = make_db([page], [page])
    run(conn, 0)
    friday(conn, concern=False)
    assert len(resolved_lines(dm(run(conn, 1)))) == 1


def test_a_concern_that_comes_back_ages_again_from_the_new_date():
    conn = setup()
    run(conn, 0)
    friday(conn, concern=False)
    run(conn, 1)
    run(conn, 2)
    friday(conn, concern=True)
    (line,) = concern_lines(dm(run(conn, 3)))
    assert "since Dec 5 (0 wks)" in line


def test_rerunning_the_same_brief_on_the_same_day_changes_no_stored_date():
    conn = setup()
    run(conn, 0)
    before = conn.execute("SELECT * FROM concerns").fetchall()
    run(conn, 0)
    assert conn.execute("SELECT * FROM concerns").fetchall() == before
    assert "since Nov 14 (0 wks)" in dm(run(conn, 0))


def test_a_same_day_rerun_still_shows_the_resolved_line():
    conn = setup()
    run(conn, 0)
    friday(conn, concern=False)
    run(conn, 1)
    assert resolved_lines(dm(run(conn, 1)))


def test_a_dry_run_shows_ages_but_stores_nothing():
    conn = setup()
    out = []
    run_brief(conn, FakeNotion([], contacts()), CONTACTS, BRIEF, SATURDAY, dry_print=True, out=out.append)
    assert "since Nov 14" in "\n".join(out)
    assert conn.execute("SELECT COUNT(*) FROM concerns").fetchone()[0] == 0


def test_a_project_with_only_a_concern_counts_as_activity_for_the_quiet_coordinator_rule():
    quiet = proj("p0", "QUIET", IN_PROGRESS, **staffed(**LATER))
    noisy = proj("p1", "ONE", IN_PROGRESS, coordinator="c-ana", **staffed(**SLIP))
    conn = make_db([quiet, noisy], [quiet, noisy])
    gw = run(conn)
    assert dm(gw, 111) == ""  # Pio has nothing to show
    assert "since Nov 14" in dm(gw, 222)
    assert "No changes: Pio" in digest(gw)


def test_a_resolved_concern_lists_its_project_once_even_with_nothing_else_to_show():
    conn = make_db([proj("p1", "ONE", IN_PROGRESS, **staffed(**SLIP))] * 1, [proj("p1", "ONE", IN_PROGRESS, **staffed(**SLIP))])
    run(conn, 0)
    run_snapshot(conn, FakeNotion([proj("p1", "ONE", IN_PROGRESS, **staffed(**LATER))], contacts()), PROJECTS, CONTACTS,
                 "friday", FRIDAY + timedelta(minutes=1))
    assert resolved_lines(dm(run(conn, 1)))
    assert not resolved_lines(dm(run(conn, 2)))


class FailingLlm:
    def complete(self, system, user, schema):
        from t6_notion_reports.llm import LlmError
        raise LlmError("down")


def test_a_fixed_date_problem_resolves_even_when_the_projects_llm_call_fails():
    conn = setup()
    assert concern_lines(dm(run(conn, 0, llm=FailingLlm())))
    friday(conn, concern=False)
    text = dm(run(conn, 1, llm=FailingLlm()))
    assert concern_lines(text) == []
    assert len(resolved_lines(text)) == 1 and "before" in resolved_lines(text)[0]


def test_an_llm_concern_stays_open_with_its_age_while_the_call_fails():
    from test_consolidation import Llm, line_id, reply_with
    from fake_notion import rich
    notes = "As of Nov 9\n• Client notes still pending"
    page = proj("p1", "ONE", IN_PROGRESS, **props(concern=False), **{"PM Notes": rich(notes)})
    conn = make_db([page], [page])
    concern = lambda p: [{"kind": "open_item", "text": "Client notes are still pending", "source": "pm_notes",
                          "line_id": line_id(p, "pending"), "quote": "• Client notes still pending"}]
    run(conn, 0, llm=Llm(reply_with(concerns=concern)))
    text = dm(run(conn, 1, llm=FailingLlm()))
    assert "Client notes are still pending · since Nov 14 (1 wk)" in text and "Resolved" not in text


def test_a_store_made_before_the_source_column_is_upgraded_in_place():
    conn = setup()
    conn.execute("DROP TABLE concerns")
    conn.execute("CREATE TABLE concerns (page_id TEXT NOT NULL, key TEXT NOT NULL, text TEXT NOT NULL,"
                 " first_on TEXT NOT NULL, last_on TEXT NOT NULL, resolved_on TEXT, PRIMARY KEY (page_id, key))")
    assert concern_lines(dm(run(conn, 0)))
    assert "source" in {r[1] for r in conn.execute("PRAGMA table_info(concerns)")}
