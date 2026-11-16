"""The Concern lines come from the API (#119): the brief hands over its findings with ``concern observe`` and renders the reply."""
import re
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from fake_api import FakeConcerns, down, refused
from fake_notion import CONTACTS, PROJECTS, FakeNotion, date, relation
from t6_notion_reports.brief_job import HISTORY_UNAVAILABLE_NOTE, run_brief
from t6_notion_reports.snapshot_job import run_snapshot
from test_flags import lines_of, staffed
from test_gated_briefs import BRIEF, FRIDAY, digest, IN_PROGRESS, SATURDAY, Gateway, contacts, dm, make_db, proj

# Submission before Playdate is a date-sanity Concern. The missing Colorist is a flag, so the
# project is listed every week and the Concern coming and going is visible.
SLIP = {"Playdate": date("2026-12-10"), "Submission Deadline": date("2026-12-01")}
LATER = {"Playdate": date("2027-01-10"), "Submission Deadline": date("2027-02-01")}  # sane, and not yet overdue
LISTED = {"Colorist": relation()}
SANITY = "Submission Deadline (Dec 1, 2026) is before Playdate (Dec 10, 2026)"


def props(concern=True):
    return staffed(**LISTED, **(SLIP if concern else LATER))


def run(conn, store, weeks=0, brief=BRIEF, notion=None, logs=None, **kw):
    gw = Gateway()
    notion = notion or FakeNotion.live(conn, contacts(), concerns=store)
    run_brief(conn, notion, brief, SATURDAY + timedelta(weeks=weeks), gateway=gw,
              log=(logs.append if logs is not None else lambda _: None), **kw)
    return gw


def setup(concern=True):
    page = proj("p1", "ONE", IN_PROGRESS, **props(concern))
    return make_db([page], [page]), FakeConcerns()


def friday(conn, concern):
    run_snapshot(conn, FakeNotion([proj("p1", "ONE", IN_PROGRESS, **props(concern))], contacts()),
                 "friday", FRIDAY + timedelta(minutes=1))


def concern_lines(text):
    return [ln for ln in lines_of(text) if "before" in ln and not ln.startswith("Resolved")]


def resolved_lines(text):
    return [ln for ln in lines_of(text) if ln.startswith("Resolved last week")]


# ---- the lines come from the reply ----

def test_a_concern_shows_its_first_listed_date_and_age_over_three_weekly_runs():
    conn, store = setup()
    ages = []
    for week in range(3):
        (line,) = concern_lines(dm(run(conn, store, week)))
        ages.append(re.search(r"since (\w+ \d+) \((\d+) wks?\)", line).groups())
    assert ages == [("Nov 14", "0"), ("Nov 14", "1"), ("Nov 14", "2")]
    assert "(1 wk)" in concern_lines(dm(run(conn, store, 1)))[0]


def test_a_concern_open_more_than_28_days_gets_the_marker_the_api_decides_and_not_before():
    conn, store = setup()
    run(conn, store, 0)
    assert "chronic" not in dm(run(conn, store, 4))
    assert "chronic" in dm(run(conn, store, 5))


def test_a_line_the_run_no_longer_finds_stays_as_not_seen_this_week_until_a_person_closes_it():
    conn, store = setup()
    run(conn, store, 0)
    friday(conn, concern=False)
    for week in (1, 2):
        text = dm(run(conn, store, week))
        (line,) = concern_lines(text)
        assert line.endswith(f"since Nov 14 ({week} wk{'s' if week > 1 else ''}) · not seen this week")
        assert not resolved_lines(text)


def test_a_line_found_in_this_run_does_not_say_not_seen():
    conn, store = setup()
    assert "not seen" not in dm(run(conn, store, 0))
    assert "not seen" not in dm(run(conn, store, 1))


def test_resolved_last_week_lists_what_any_path_closed_since_the_previous_brief_once():
    conn, store = setup()
    run(conn, store, 0)
    store.close("is before", "2026-11-18")  # a person, a settling event or expiry: the API closed it
    friday(conn, concern=False)
    second = dm(run(conn, store, 1))
    assert not concern_lines(second)
    (resolved,) = resolved_lines(second)
    assert "is before" in resolved and "Submission Deadline" in resolved
    assert not resolved_lines(dm(run(conn, store, 2)))


def test_the_resolved_line_is_one_line_per_project_however_many_concerns_closed():
    page = proj("p1", "ONE", IN_PROGRESS, **staffed(**LISTED, **SLIP, **{"Archival Deadline": date("2026-11-20")}))
    conn, store = make_db([page], [page]), FakeConcerns()
    run(conn, store, 0)
    for part in ("Submission Deadline", "Archival Deadline"):
        store.close(part, "2026-11-18")
    friday(conn, concern=False)
    assert len(resolved_lines(dm(run(conn, store, 1)))) == 1


def test_rerunning_the_same_brief_on_the_same_day_shows_the_same_lines():
    conn, store = setup()
    first = dm(run(conn, store, 0))
    assert dm(run(conn, store, 0)) == first
    assert len(store.open()) == 1


def test_the_lines_include_every_open_concern_of_the_project_also_those_others_raised():
    conn, store = setup()
    run(conn, store, 0)
    store.items.append({"id": "t6c_person", "project": "p1", "key": "k", "text": "Vendor quote still missing", "source": "person",
                        "opened_on": "2026-11-01", "state": "open", "closed_on": None, "owner": None})
    text = dm(run(conn, store, 1))
    assert "Vendor quote still missing · since Nov 1 (2 wks) · not seen this week" in text
    assert text.index("Vendor quote") < text.index("is before")  # the API's order: oldest first


# ---- what is handed over ----

def observed(store):
    return store.observed[-1]


def test_findings_carry_their_source_and_the_run_date_and_the_key_is_per_day_and_request():
    conn, store = setup()
    run(conn, store, 0)
    sent = observed(store)
    assert (sent.run_date, sent.dry_run) == ("2026-11-14", False)
    assert re.fullmatch(r"psr-concerns-2026-11-14-[0-9a-f]{8}", sent.idempotency_key)
    assert [(f.project, f.text, f.source) for f in sent.findings] == [("p1", SANITY, "flag_rule")]


def test_the_owner_is_the_single_coordinator_when_the_api_knows_that_person_else_empty():
    conn, store = setup()
    notion = FakeNotion.live(conn, contacts(), concerns=store)
    notion.t6_ids = {"c-oto": "t6p_oto"}
    run(conn, store, notion=notion)
    assert observed(store).findings[0].owner == "t6p_oto"
    run(conn, FakeConcerns(), notion=FakeNotion.live(conn, contacts()))  # no T6 ID bound
    two = proj("p1", "ONE", IN_PROGRESS, **props())
    two["properties"]["T6 Post Coordinator"] = relation("c-oto", "c-ida")
    conn2, store2 = make_db([two], [two]), FakeConcerns()
    notion = FakeNotion.live(conn2, contacts(), concerns=store2)
    notion.t6_ids = {"c-oto": "t6p_oto", "c-ida": "t6p_ida"}
    run(conn2, store2, notion=notion)
    assert observed(store2).findings[0].owner is None


def test_a_project_with_a_t6_id_is_sent_by_it_and_its_lines_come_back_to_it():
    conn, store = setup()
    notion = FakeNotion.live(conn, contacts(), concerns=store)
    notion.t6_ids = {"p1": "t6_project_one"}
    text = dm(run(conn, store, notion=notion))
    assert observed(store).findings[0].project == "t6_project_one"
    assert concern_lines(text)


class FailingLlm:
    def complete(self, system, user, schema, **kw):
        from t6_notion_reports.llm_api import LlmError, Reply
        raise LlmError("down")


def test_a_source_that_could_not_be_read_is_sent_as_unavailable_so_the_api_keeps_its_lines():
    conn, store = setup()
    run(conn, store, 0, llm=FailingLlm())
    assert {(u.project, u.source) for u in observed(store).unavailable} == {("p1", "pm_notes_read"), ("p1", "leave")}
    run(conn, store, 1)  # no LLM at all, no leave call: the same
    assert {(u.project, u.source) for u in observed(store).unavailable} == {("p1", "pm_notes_read"), ("p1", "leave")}


def test_a_dry_run_sends_dry_run_shows_the_ages_and_stores_nothing():
    conn, store = setup()
    run(conn, store, 0)
    out = []
    run_brief(conn, FakeNotion.live(conn, contacts(), concerns=store), BRIEF, SATURDAY + timedelta(weeks=1), dry_print=True, out=out.append)
    assert observed(store).dry_run is True
    assert "since Nov 14 (1 wk)" in "\n".join(out)
    assert len(store.runs) == 1  # the dry run left no run behind


# ---- the API does not answer ----

@pytest.mark.parametrize("outcome", [down(), refused("concern")])
def test_when_the_api_does_not_answer_the_lines_show_without_age_and_the_digest_says_so(outcome):
    conn, store = setup()
    run(conn, store, 0)
    notion = FakeNotion.live(conn, contacts(), concerns=store)
    notion.api.fail["concern observe"] = outcome
    logs = []
    gw = run(conn, store, 5, notion=notion, logs=logs)
    (line,) = concern_lines(dm(gw))
    assert line == "• " + SANITY  # no age, no chronic mark
    assert not resolved_lines(dm(gw)) and "since" not in line
    assert HISTORY_UNAVAILABLE_NOTE in digest(gw) and HISTORY_UNAVAILABLE_NOTE == "⚠️ Concern history unavailable"
    assert any(log.startswith("brief concerns unavailable error=concern observe") for log in logs)
    assert all("Submission" not in log for log in logs)  # the rule name only, never a text


def test_with_a_working_api_the_digest_has_no_history_warning():
    conn, store = setup()
    assert "Concern history" not in digest(run(conn, store, 0))


def test_a_project_with_only_a_concern_counts_as_activity_for_the_quiet_coordinator_rule():
    quiet = proj("p0", "QUIET", IN_PROGRESS, **staffed(**LATER))
    noisy = proj("p1", "ONE", IN_PROGRESS, coordinator="c-ida", **staffed(**SLIP))
    conn = make_db([quiet, noisy], [quiet, noisy])
    gw = run(conn, FakeConcerns())
    assert dm(gw, 111) == ""  # Oto has nothing to show
    assert "since Nov 14" in dm(gw, 222)
    assert "No changes: Oto" in digest(gw)


def test_a_closed_concern_lists_its_project_once_even_with_nothing_else_to_show():
    conn, store = make_db([proj("p1", "ONE", IN_PROGRESS, **staffed(**SLIP))], [proj("p1", "ONE", IN_PROGRESS, **staffed(**SLIP))]), FakeConcerns()
    run(conn, store, 0)
    store.close("is before", "2026-11-18")
    run_snapshot(conn, FakeNotion([proj("p1", "ONE", IN_PROGRESS, **staffed(**LATER))], contacts()),
                 "friday", FRIDAY + timedelta(minutes=1))
    assert resolved_lines(dm(run(conn, store, 1)))
    assert not resolved_lines(dm(run(conn, store, 2)))


# ---- a Concern that rests on a date from the PM Notes ----

NOTE_DATE_NOTES = "As of Nov 9\n• Submission is Dec 1, waiting on the client"


def note_date_setup():
    from fake_notion import rich
    page = proj("p1", "ONE", IN_PROGRESS, **staffed(**LISTED, Playdate=date("2026-12-20"), **{"Submission Deadline": date(None)}),
                **{"PM Notes": rich(NOTE_DATE_NOTES)})
    return make_db([page], [page]), FakeConcerns()


def note_date_llm():
    from test_consolidation import Llm, line_id, reply_with
    quote = "• Submission is Dec 1, waiting on the client"
    dates = lambda p: [{"property": "submission_deadline", "iso_date": "2026-12-01",
                        "line_id": line_id(p, "waiting on the client"), "quote": quote}]
    return Llm(reply_with(dates_from_notes=dates))


def test_a_notes_dependent_ordering_concern_is_pm_notes_read_and_keeps_its_age_when_extraction_fails():
    conn, store = note_date_setup()
    first = dm(run(conn, store, 0, llm=note_date_llm()))
    assert len(concern_lines(first)) == 1
    assert [(f.source) for f in observed(store).findings] == ["pm_notes_read"]
    for week in (1, 2):
        text = dm(run(conn, store, week, llm=FailingLlm()))
        (line,) = concern_lines(text)
        assert f"since Nov 14 ({week} wk" in line and "Resolved" not in text
        assert ("p1", "pm_notes_read") in {(u.project, u.source) for u in observed(store).unavailable}
    recovered = dm(run(conn, store, 3, llm=note_date_llm()))
    assert "since Nov 14 (3 wks)" in concern_lines(recovered)[0] and "not seen" not in recovered


def test_a_stored_date_problem_is_a_flag_rule_finding_with_or_without_an_llm():
    conn, store = setup()
    for llm in (None, FailingLlm()):
        run(conn, store, 0, llm=llm)
        assert [f.source for f in observed(store).findings] == ["flag_rule"]


def test_a_rerun_the_same_day_with_other_findings_is_a_new_request_not_a_refused_key():
    conn, store = setup()
    first = dm(run(conn, store, 0))
    friday(conn, concern=False)
    logs = []
    again = dm(run(conn, store, 0, logs=logs))
    assert "unavailable" not in "".join(logs)
    assert concern_lines(again)  # still open: the API closes nothing
    assert len(store.requests) == 2 and first
