"""The Minutes draft writes through the t6 API: order, chunks, failures, and the block conversion."""
import sqlite3
from datetime import date

import pytest

from fake_api import down, refused
from fake_notion import MEETING, FakeNotion, meeting_page, project
from t6.verbs import NoteBlock
from t6_notion_reports.minutes_api import MinutesApiError, to_note_blocks
from t6_notion_reports.mom import rich_text
from t6_notion_reports.mom_job import MomAlreadySent, run_draft_mom
from t6_notion_reports.snapshot_job import run_snapshot
from t6_notion_reports.snapshots import SCHEMA
from test_mom import D, FRIDAY, MONDAY, conn, contacts, pages  # noqa: F401  (conn is a fixture)

OLD = lambda: meeting_page("old", "PSR MOM 2026-11-16", "2026-11-16")  # noqa: E731


def run(conn, notes=(), api_kw=None):
    n = FakeNotion(pages(True), contacts(), list(notes))
    n.api.fail.update((api_kw or {}).get("fail", {}))
    logs = []
    return n, run_draft_mom(conn, n, MEETING, D, log=logs.append), logs


def test_the_new_page_is_created_before_the_old_one_is_trashed(conn):
    n, res, _ = run(conn, [OLD()])
    assert [v for v, _ in n.api.writes] == ["create", "trash"]
    assert n.api.verbs("trash")[0].note == "old" and res.page_id == "new-1"


def test_a_page_over_100_blocks_goes_as_create_then_appends(conn):
    many = [project(f"x{i}", f"X{i}", f"X{i:03d}") for i in range(130)]
    run_snapshot(conn, FakeNotion(many, []), "friday", FRIDAY.replace(hour=11))
    run_snapshot(conn, FakeNotion(many, []), "monday", MONDAY.replace(day=23))
    n = FakeNotion(many, contacts())
    run_draft_mom(conn, n, MEETING, date(2026, 11, 23), log=lambda _: None)
    sizes = [len(i.blocks) for i in n.api.verbs("create", "append")]
    assert [v for v, _ in n.api.writes][0] == "create" and sizes[0] == 100 and all(s <= 100 for s in sizes) and len(sizes) > 1
    assert {i.note for i in n.api.verbs("append")} == {"new-1"}


def test_a_failed_create_stops_the_job_and_keeps_the_old_draft(conn):
    for outcome in (refused("notion"), down()):
        with pytest.raises(MinutesApiError):
            run(conn, [OLD()], {"fail": {"create": outcome}})
    n = FakeNotion(pages(True), contacts(), [OLD()])
    n.api.fail["create"] = refused("minutes")
    with pytest.raises(MinutesApiError, match="rule=minutes"):
        run_draft_mom(conn, n, MEETING, D, log=lambda _: None)
    assert n.api.verbs("trash") == [] and not n.meetings[0].get("in_trash")


def test_a_failed_trash_is_logged_with_the_rule_and_leaves_two_drafts(conn):
    n, res, logs = run(conn, [OLD()], {"fail": {"trash": refused("sent")}})
    assert res.page_id == "new-1"
    assert any("trash failed" in line and "rule=sent" in line for line in logs)
    live = [p for p in n.meetings if not p.get("in_trash")]
    assert len(live) == 2
    n.api.fail.clear()  # the next run trashes both
    run_draft_mom(conn, n, MEETING, D, log=lambda _: None)
    assert [p["id"] for p in n.meetings if not p.get("in_trash")] == ["new-2"]  # both earlier drafts are gone


def test_a_sent_draft_refuses_and_writes_nothing(conn):
    sent = meeting_page("s", "PSR MOM 2026-11-16", "2026-11-16", sent_at="2026-11-16T05:00:00Z")
    n = FakeNotion(pages(True), contacts(), [sent])
    with pytest.raises(MomAlreadySent):
        run_draft_mom(conn, n, MEETING, D, log=lambda _: None)
    assert n.api.writes == []


def test_attendees_come_from_the_latest_earlier_draft_without_its_body(conn):
    meetings = [meeting_page("a", "PSR MOM 2026-11-02", "2026-11-02", [("u0", "Old")]),
                meeting_page("b", "PSR MOM 2026-11-09", "2026-11-09", [("u1", "Quilla")]),
                meeting_page("c", "PSR MOM 2026-11-23", "2026-11-23", [("u2", "Later")])]
    n = FakeNotion(pages(True), contacts(), meetings)
    seen = []
    orig = n.api.meeting_notes_list
    n.api.meeting_notes_list = lambda i: (seen.append(i), orig(i))[1]
    run_draft_mom(conn, n, MEETING, D, log=lambda _: None)
    assert n.api.verbs("create")[0].attendee_ids == ["u1"]
    latest = [i for i in seen if i.latest]
    assert len(latest) == 1 and latest[0].before == "2026-11-16" and latest[0].include_body is False


def block(kind, text, children=(), **extra):
    return {"object": "block", "type": kind, kind: {"rich_text": [{"type": "text", "text": {"content": text}}],
                                                    **({"children": list(children)} if children else {}), **extra}}


def test_to_note_blocks_covers_every_kind_the_draft_uses():
    got = to_note_blocks([
        block("heading_1", "Title"), block("heading_2", "Group"), block("paragraph", "Body"),
        block("numbered_list_item", "One"), block("to_do", "Check", checked=True),
        block("bulleted_list_item", "Parent", [block("bulleted_list_item", "Child"),
                                               block("toggle", "Raw", [block("bulleted_list_item", "Src")])]),
    ])
    assert [b.kind for b in got] == ["heading_1", "heading_2", "paragraph", "numbered_list_item", "to_do",
                                      "bulleted_list_item"]
    assert got[4].checked is True and got[0].checked is None
    assert got[5].children[1].kind == "toggle" and got[5].children[1].children[0].text == "Src"
    assert got[0] == NoteBlock(kind="heading_1", text="Title", spans=[{"text": "Title"}], children=[])


def test_to_note_blocks_keeps_bold_and_every_chunk_of_a_long_text():
    bold = {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rich_text("x" * 4500, True)}}
    (got,) = to_note_blocks([bold])
    assert [len(s.text) for s in got.spans] == [2000, 2000, 500] and all(s.bold for s in got.spans)
    assert got.text == "x" * 4500
    link = {"type": "paragraph", "paragraph": {"rich_text": [
        {"type": "text", "text": {"content": "a", "link": {"url": "https://example.test"}},
         "annotations": {"italic": True, "code": True}}]}}
    span = to_note_blocks([link])[0].spans[0]
    assert (span.url, span.italic, span.code, span.bold) == ("https://example.test", True, True, False)
