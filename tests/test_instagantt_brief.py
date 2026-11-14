"""The briefing's Instagantt dates: each linked show's next tasks, from fixture exports."""
from datetime import date

from t6.instagantt import LinkedTimeline, LinkReport, Task, Timeline, Unmapped, parse_export
from test_gated_briefs import BRIEF, CONTACTS, SATURDAY, FakeNotion, Gateway, contacts, dm, digest, make_db, proj, staffed
from t6_notion_reports.brief_job import run_brief
from t6_notion_reports.instagantt_feed import instagantt_dates

EXPORT = """Project,Project ID,Task,Start,End
Alpha,ig-1,Rough cut,2026-11-02,2026-11-06
Alpha,ig-1,Picture lock,2026-11-20,2026-11-24
Alpha,ig-1,Delivery,2026-12-01,
Alpha,ig-1,Colour,2026-12-05,2026-12-08
Alpha,ig-1,Mix,2026-12-09,2026-12-10
Orphan,ig-2,Cut,2026-11-18,2026-11-19
"""


def report(**linked):
    timelines = {t.external_id: t for t in parse_export(EXPORT)}
    ok = tuple(LinkedTimeline(timelines[k], v) for k, v in linked.items())
    return LinkReport(ok, tuple(Unmapped(t, "no External Links row") for k, t in timelines.items() if k not in linked))


def run(load, pages=None):
    pages = pages or [proj("p1", "ALPHA", **staffed())]
    gw, logs = Gateway(), []
    run_brief(make_db(pages, pages), FakeNotion([], contacts()), CONTACTS, BRIEF, SATURDAY, gateway=gw,
              instagantt=load, log=logs.append)
    return gw, logs


def test_a_linked_show_lists_its_next_three_tasks_and_skips_finished_ones():
    gw, _ = run(lambda: report(**{"ig-1": "p1"}))
    text = dm(gw)
    assert "Show ALPHA" in text
    assert "Rough cut" not in text
    assert text.index("Picture lock · Nov 20 – Nov 24") < text.index("Delivery · Dec 1") < text.index("Colour · Dec 5 – Dec 8")
    assert "Mix" not in text
    assert "Picture lock" in digest(gw)


def test_unlinked_shows_are_listed_for_review_in_cyrils_digest_only():
    gw, _ = run(lambda: report(**{"ig-1": "p1"}))
    assert "Instagantt shows not linked to a project: Orphan" in digest(gw)
    assert "Orphan" not in dm(gw)


def test_a_show_outside_the_status_gate_is_left_out():
    gw, _ = run(lambda: report(**{"ig-1": "other-page"}))
    assert "Instagantt ·" not in dm(gw) + digest(gw)


def test_an_unreadable_export_adds_a_note_and_the_brief_still_goes():
    def boom():
        raise OSError("/secret/path is gone")
    gw, logs = run(boom, [proj("p1", "ALPHA", **staffed(), stage="Online")])
    assert "Instagantt unavailable" in digest(gw) and gw.sent
    assert not any("secret" in line for line in logs)


def test_the_feed_orders_by_start_and_marks_milestones_with_one_date():
    feed = instagantt_dates(lambda: LinkReport((LinkedTimeline(Timeline("A", "a", (
        Task("Later", date(2026, 12, 3), date(2026, 12, 3)), Task("Sooner", date(2026, 11, 15), date(2026, 11, 16)))), "p1"),), ()),
        {"p1": {}}, date(2026, 11, 14), print)
    assert feed.lines == {"p1": ("Instagantt · Sooner · Nov 15 – Nov 16", "Instagantt · Later · Dec 3")}
