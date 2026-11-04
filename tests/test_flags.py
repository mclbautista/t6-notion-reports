"""Deterministic flags (#45), through the brief job with a fake Notion, LLM and gateway."""
import re
from dataclasses import replace

from fake_notion import CONTACTS, FakeNotion, date, relation
from t6_notion_reports.brief_job import run_brief
from t6_notion_reports.flags import ROLES, SEPARATE_ROLES
from test_gated_briefs import (
    AIRING, ARCHIVING, BRIEF, IN_PROGRESS, LEADS, SATURDAY, STANDBY, TVC, Gateway, brief, contacts, digest, dm,
    make_db, proj,
)

# Today is Sat 2026-11-14 in Manila.


def staffed(**props):
    """A project with every role filled and sane dates, so only what a test changes can be flagged."""
    # proj() sets the coordinator itself, from its coordinator argument.
    base = {role: relation("c-ana") for role in (*ROLES, *SEPARATE_ROLES) if role != "T6 Post Coordinator"}
    base.update({"Playdate": date("2026-11-20"), "Submission Deadline": date("2026-12-20")})
    base.update(props)
    return base


def one(status, **props):
    """Brief for a single unchanged project; the coordinator's DM, or the digest for a Lead (DMs skip Leads)."""
    page = proj("p1", "ONE", status, **props)
    gw = brief(make_db([page], [page]))
    return digest(gw) if status == LEADS else dm(gw)


def lines_of(text):
    return [re.sub(r"<[^>]+>", "", ln) for ln in text.splitlines()]


def body(text):
    """The project's lines after its heading."""
    ls = lines_of(text)
    start = next(i for i, ln in enumerate(ls) if "Show ONE" in ln)
    return ls[start + 1:]


def test_in_progress_and_tvc_flag_blank_playdate_and_submission_deadline_by_name():
    for status in (IN_PROGRESS, TVC):
        text = one(status, **staffed(Playdate=date(None), **{"Submission Deadline": date(None)}))
        assert "⚠️ No Playdate" in text and "⚠️ No Submission Deadline" in text
        assert "Archival" not in text


def test_airing_flags_blank_dates_but_standby_never_does():
    blank = staffed(Playdate=date(None), **{"Submission Deadline": date(None)})
    air = one(AIRING, **blank)
    assert "⚠️ No Playdate" in air and "⚠️ No Submission Deadline" in air
    sby = one(STANDBY, **blank)
    assert "No Playdate" not in sby and "No Submission Deadline" not in sby


def test_overdue_flag_shows_days_and_is_listed_first_for_in_progress_and_tvc():
    for status in (IN_PROGRESS, TVC):
        text = one(status, **staffed(Playdate=date("2026-11-10"), **{"Submission Deadline": date(None)}, Colorist=relation()))
        first = body(text)[0]
        assert "Playdate" in first and "overdue by 4 days" in first
        assert "No Submission Deadline" in text and "Missing roles" in text


def test_overdue_by_one_day_is_singular_and_today_is_not_overdue():
    assert "overdue by 1 day" in one(IN_PROGRESS, **staffed(**{"Submission Deadline": date("2026-11-13")}))
    assert "overdue" not in one(IN_PROGRESS, **staffed(**{"Submission Deadline": date("2026-11-14")}))


def test_a_datetime_deadline_is_judged_by_its_manila_day():
    text = one(IN_PROGRESS, **staffed(**{"Submission Deadline": date("2026-11-12T20:00:00.000+08:00")}))
    assert "Submission Deadline overdue by 2 days" in text


def test_airing_is_never_overdue():
    text = one(AIRING, **staffed(Playdate=date("2026-01-05"), **{"Submission Deadline": date("2026-02-05")}))
    assert "overdue" not in text.lower()
    assert "No Playdate" not in text


def test_standby_gets_no_date_flags_even_when_overdue():
    text = one(STANDBY, **staffed(Playdate=date("2026-11-01"), **{"Submission Deadline": date("2026-11-02")}))
    assert "overdue" not in text and "No Playdate" not in text


def test_in_progress_does_not_flag_archival_deadline():
    text = one(IN_PROGRESS, **staffed(**{"Archival Deadline": date("2027-01-05")}))
    assert "Archival" not in text


def test_archiving_flags_blank_and_overdue_archival_deadline_only():
    blank = one(ARCHIVING, **staffed(Playdate=date(None), **{"Submission Deadline": date(None)}))
    assert "⚠️ No Archival Deadline" in blank
    assert "No Playdate" not in blank and "No Submission Deadline" not in blank
    late = one(ARCHIVING, **staffed(**{"Archival Deadline": date("2026-11-04")}))
    assert "Archival Deadline overdue by 10 days" in body(late)[0]
    assert "overdue" not in one(ARCHIVING, **staffed(**{"Archival Deadline": date("2026-12-04")}))


def test_a_lead_with_a_blank_playdate_gets_no_flag():
    text = one(LEADS, **staffed(Playdate=date(None), **{"Submission Deadline": date(None)}))
    assert "⚠️" not in text and "No Playdate" not in text


def test_a_lead_with_a_past_playdate_is_flagged_overdue_first():
    text = one(LEADS, **staffed(Playdate=date("2026-11-04")))
    assert "Playdate overdue by 10 days" in body(text)[0]


def test_a_lead_with_a_playdate_under_30_days_away_gets_the_warning():
    assert "Playdate in 29 days" in one(LEADS, **staffed(Playdate=date("2026-12-13")))
    assert "Playdate today" in one(LEADS, **staffed(Playdate=date("2026-11-14")))


def test_a_lead_playdate_30_or_more_days_away_gets_no_warning():
    text = one(LEADS, **staffed(Playdate=date("2026-12-14")))
    assert "Playdate in" not in text and "⚠️" not in text


def test_the_lead_warning_window_is_configurable():
    page = proj("p1", "ONE", LEADS, **staffed(Playdate=date("2026-12-04")))  # 20 days away
    conn = make_db([page], [page])
    gw = Gateway()
    run_brief(conn, FakeNotion([], contacts()), CONTACTS, replace(BRIEF, lead_warning_days=10),
              SATURDAY, gateway=gw, log=lambda _: None)
    assert "Playdate in" not in dm(gw)
    assert "Playdate in 20 days" in digest(brief(conn))


def test_a_leads_submission_and_archival_dates_are_never_flagged():
    text = one(LEADS, **staffed(Playdate=date("2027-03-01"), **{"Submission Deadline": date("2026-01-01")}))
    assert "No Submission" not in text and "Archival" not in text
    assert "overdue" not in text


def test_missing_roles_render_as_one_line_naming_them():
    text = one(IN_PROGRESS, **staffed(**{"Online Editor": relation(), "Sound Tech": relation()}))
    roles = [ln for ln in lines_of(text) if "Missing roles" in ln]
    assert roles == ["⚠️ Missing roles: Online Editor, Sound Tech"]


def test_assistant_editor_and_coordinator_are_listed_with_the_roles():
    page = proj("p1", "ONE", IN_PROGRESS, **staffed(**{"Assistant Editor": relation()}))
    page["properties"]["T6 Post Coordinator"] = relation()
    text = digest(brief(make_db([page], [page])))
    assert "⚠️ Missing roles: Assistant Editor, T6 Post Coordinator" in text


def test_post_supe_is_not_flagged():
    assert "Post Supe" not in one(IN_PROGRESS, **staffed(**{"Post Supe": relation()}))


def test_director_and_cinematographer_are_flagged_separately_from_the_roles_line():
    text = one(IN_PROGRESS, **staffed(Director=relation(), Cinematographer=relation(), Colorist=relation()))
    ls = lines_of(text)
    assert "⚠️ No Director" in ls and "⚠️ No Cinematographer" in ls
    assert ["⚠️ Missing roles: Colorist"] == [ln for ln in ls if "Missing roles" in ln]


def test_roles_are_only_flagged_for_in_progress_tvc_and_airing():
    bare = staffed(Director=relation(), Editors=relation())
    for status in (STANDBY, ARCHIVING):
        assert "Missing roles" not in one(status, **bare) and "No Director" not in one(status, **bare)
    assert "No Director" in one(AIRING, **bare) and "No Director" in one(TVC, **bare)


def test_a_fully_staffed_project_has_no_roles_line():
    assert "Missing roles" not in one(IN_PROGRESS, **staffed())


def test_date_sanity_problems_are_listed_as_one_concern_each():
    text = one(IN_PROGRESS, **staffed(Playdate=date("2026-12-10"), **{"Submission Deadline": date("2026-12-01")}))
    concerns = [ln for ln in lines_of(text) if "before" in ln]
    assert len(concerns) == 1 and "Submission Deadline" in concerns[0] and "Playdate" in concerns[0]
    assert "Concerns" in text

    arch = one(ARCHIVING, **staffed(**{"Submission Deadline": date("2026-12-01"), "Archival Deadline": date("2026-11-20")}))
    concerns = [ln for ln in lines_of(arch) if "before" in ln]
    assert len(concerns) == 1 and "Archival Deadline" in concerns[0]



def test_dates_far_in_the_future_and_ordered_dates_raise_no_concern():
    text = one(IN_PROGRESS, **staffed(Playdate=date("2028-01-01"), **{"Submission Deadline": date("2028-02-01")}))
    assert "Concerns" not in text and "year" not in text


def test_a_project_with_no_changes_but_a_flag_counts_as_activity_for_the_coordinator():
    clean = proj("p1", "CLEAN", IN_PROGRESS, **staffed())
    flagged = proj("p2", "FLAG", IN_PROGRESS, coordinator="c-ana", **staffed(Playdate=date(None)))
    gw = brief(make_db([clean, flagged], [clean, flagged]))
    assert dm(gw, 111) == ""  # Pio: nothing to show, so no DM
    assert "No Playdate" in dm(gw, 222)
    text = digest(gw)
    assert "Show FLAG" in text and "No changes: Pio" in text and "No changes on any project" not in text


def test_flags_appear_in_the_digest_too():
    page = proj("p1", "ONE", IN_PROGRESS, **staffed(Playdate=date("2026-11-10")))
    text = digest(brief(make_db([page], [page])))
    assert "Playdate overdue by 4 days" in text


def test_flags_show_on_the_first_brief_and_on_changed_projects():
    old = proj("p1", "ONE", IN_PROGRESS, stage="Offline", **staffed(Playdate=date(None)))
    new = proj("p1", "ONE", IN_PROGRESS, stage="Online", **staffed(Playdate=date(None)))
    text = dm(brief(make_db([old], [new])))
    assert "No Playdate" in text and "Stage: Offline → Online" in text


def test_archiving_ignores_playdate_and_submission_dates():
    text = one(ARCHIVING, **staffed(Playdate=date("2026-10-01"), **{"Submission Deadline": date("2026-10-05"), "Archival Deadline": date("2026-12-20")}))
    assert "overdue" not in text and "No Playdate" not in text


def test_date_sanity_concerns_also_apply_to_standby_and_leads():
    backwards = {"Playdate": date("2026-12-10"), "Submission Deadline": date("2026-12-01")}
    for status in (STANDBY, LEADS):
        text = one(status, **staffed(**backwards))
        assert len([ln for ln in lines_of(text) if "before" in ln]) == 1
