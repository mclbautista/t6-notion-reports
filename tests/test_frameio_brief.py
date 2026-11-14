"""The briefing's Frame.io section: share-link activity per project, from t6-core's worker client."""
from datetime import datetime, timezone

from t6.frameio import BriefingLink, BriefingShow, Failed, SignInNeeded
from test_gated_briefs import (
    BRIEF, CONTACTS, FRIDAY, SATURDAY, FakeNotion, Gateway, contacts, dm, digest, make_db, proj, staffed,
)
from t6_notion_reports.brief_job import run_brief


class Reader:
    def __init__(self, answer):
        self.answer, self.since = answer, []

    def briefing(self, since):
        self.since.append(since)
        return self.answer


def link(name, active=True, day=12):
    return BriefingLink(share_id="s-" + name, name=name, url="https://f.io/" + name,
                        last_activity=datetime(2026, 11, day, 5, tzinfo=timezone.utc), active=active)


def show(name, pages, *links):
    return BriefingShow(frameio_project_id="fp-" + name, frameio_project_name=name,
                        project_page_ids=tuple(pages), links=tuple(links))


def run(reader, pages=None):
    pages = pages or [proj("p1", "ALPHA", **staffed())]
    gw, logs = Gateway(), []
    run_brief(make_db(pages, pages), FakeNotion([], contacts()), CONTACTS, BRIEF, SATURDAY, gateway=gw,
              frameio=reader, log=logs.append)
    return gw, logs


def test_an_active_share_link_shows_under_its_project_even_with_no_other_change():
    gw, _ = run(Reader((show("Alpha", ["p1"], link("Cut 3")),)))
    text = dm(gw)
    assert "Show ALPHA" in text and "Frame.io · Cut 3 · activity Nov 12" in text
    assert "https://f.io" not in text  # the share URL stays out of Telegram
    assert "Frame.io · Cut 3" in digest(gw)


def test_a_link_nobody_marked_active_is_left_out():
    gw, _ = run(Reader((show("Alpha", ["p1"], link("Cut 3", active=False)),)))
    assert "Frame.io" not in dm(gw) + digest(gw)


def test_a_show_with_no_linked_project_is_noted_for_cyril_only():
    gw, _ = run(Reader((show("Orphan", [], link("Cut 1")),)))
    assert "Frame.io shows with no linked project: Orphan" in digest(gw)
    assert "Orphan" not in dm(gw)


def test_since_is_the_previous_friday_snapshot_or_a_week_back_for_the_first_brief():
    reader = Reader(())
    run(reader)
    assert reader.since == [SATURDAY.replace(day=7)]  # one Monday and one Friday snapshot: no earlier Friday


def test_a_failed_worker_adds_a_note_and_the_brief_still_goes():
    for answer, note in ((Failed("boom secret"), "Frame.io unavailable"), (SignInNeeded(), "Frame.io needs a sign-in")):
        gw, logs = run(Reader(answer), [proj("p1", "ALPHA", **staffed(), stage="Online")])
        assert note in digest(gw)
        assert gw.sent
        assert not any("boom" in line for line in logs)


def test_no_reader_means_no_section_and_no_note():
    gw, _ = run(None)
    assert "Frame.io" not in dm(gw) + digest(gw)
