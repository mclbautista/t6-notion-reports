"""Haiku task summaries: the OpenRouter summarizer with a fake transport, the cache and its retention (#77)."""
import re
from datetime import datetime, timedelta, timezone

from fake_llm import transport_llm
from fake_notion import CONTACTS, JOB_ORDERS, PROJECTS
from t6_notion_reports.jo_snapshots import connect, take_snapshot
from t6_notion_reports.shift_job import run_shift_report
from t6_notion_reports.task_summary_llm import LlmTaskSummarizer, summarize_tasks
from test_shift_report import PM, jo, prior, world

NOW = datetime(2026, 11, 3, 19, tzinfo=timezone(timedelta(hours=8)))


class Transport:
    def __init__(self, content='{"summary": "Color grade"}', status=200):
        self.content, self.status, self.bodies = content, status, []

    def __call__(self, url, headers, body):
        self.bodies.append(body)
        if self.status != 200:
            return self.status, None
        return 200, {"choices": [{"message": {"content": self.content}}]}


def summarizer(transport):
    return LlmTaskSummarizer(transport_llm(transport))


def test_valid_reply_is_returned_and_only_the_task_is_sent_with_zero_data_retention():
    t = Transport()
    assert summarizer(t).summarize("Color grade the final cut") == "Color grade"
    body = t.bodies[0]
    assert body["provider"] == {"zdr": True}
    assert [m["content"] for m in body["messages"] if m["role"] == "user"] == ["Color grade the final cut"]


def test_four_word_reply_and_error_become_fallbacks_and_are_not_cached():
    conn = connect()
    for transport in (Transport('{"summary": "one two three four"}'), Transport(status=500)):
        lookup, stats = summarize_tasks(conn, summarizer(transport), {"p1": "Color grade the final cut"}, NOW)
        assert (stats.new, stats.cached, stats.fallback) == (0, 0, 1)
        assert lookup.for_page("p1").summarize("Color grade the final cut") is None
    assert conn.execute("SELECT COUNT(*) FROM jo_task_summaries").fetchone()[0] == 0


def test_cached_jo_makes_no_call_and_a_changed_task_is_summarised_again():
    conn = connect()
    t = Transport()
    s = summarizer(t)
    _, stats = summarize_tasks(conn, s, {"p1": "Color grade the final cut"}, NOW)
    assert (stats.new, stats.cached) == (1, 0)
    lookup, stats = summarize_tasks(conn, s, {"p1": "Color grade the final cut"}, NOW)
    assert (stats.new, stats.cached, stats.fallback) == (0, 1, 0) and len(t.bodies) == 1
    assert lookup.for_page("p1").summarize("Color grade the final cut") == "Color grade"
    _, stats = summarize_tasks(conn, s, {"p1": "Fix the audio sync"}, NOW)
    assert stats.new == 1 and len(t.bodies) == 2
    assert conn.execute("SELECT COUNT(*) FROM jo_task_summaries").fetchone()[0] == 2  # the old Task's summary stays cached


def test_task_a_then_b_then_a_again_reuses_the_cached_summary_of_a():
    conn = connect()
    t = Transport()
    s = summarizer(t)
    for task in ("Color grade the final cut", "Fix the audio sync"):
        summarize_tasks(conn, s, {"p1": task}, NOW)
    assert len(t.bodies) == 2
    lookup, stats = summarize_tasks(conn, s, {"p1": "Color grade the final cut"}, NOW)
    assert (stats.new, stats.cached, stats.fallback) == (0, 1, 0) and len(t.bodies) == 2
    assert lookup.for_page("p1").summarize("Color grade the final cut") == "Color grade"


class FirstOnly:
    """Succeeds on the first call, fails on every later one."""

    def __init__(self):
        self.calls = 0

    def summarize(self, task_text):
        self.calls += 1
        return "Color grade" if self.calls == 1 else None


def test_one_jos_fallback_does_not_borrow_another_jos_summary_for_identical_task_text():
    db = connect()
    prior(db)
    fake = world(jo("j1", "1", "TOC", task="Export the final cut"), jo("j2", "2", "TOC", task="Export the final cut"))
    log, out = [], []
    run_shift_report(db, fake, JOB_ORDERS, CONTACTS, PROJECTS, "1900", PM, False, out.append, log.append,
                     summarizer=FirstOnly())
    assert log[0].endswith("summaries=1 cached=0 fallback=1")
    labels = re.findall(r"#(\d+)</a> - (.+?) - Pending", "\n".join(out))
    assert sorted(label for _, label in labels) == ["Color grade", "Export the final"]


def test_names_in_the_task_are_masked_on_the_way_out_and_restored():
    conn = world(jo("j1", "1", "TOC", task="Ask Oto about grade")), None
    fake, seen = conn[0], []

    def transport(url, headers, body):
        user = [m["content"] for m in body["messages"] if m["role"] == "user"][0]
        seen.append(user)
        return 200, {"choices": [{"message": {"content": '{"summary": "Ask [person 1]"}'}}]}

    db = connect()
    prior(db)
    out = []
    run_shift_report(db, fake, JOB_ORDERS, CONTACTS, PROJECTS, "1900", PM, False, out.append, lambda _: None,
                     llm=transport_llm(transport))
    assert seen == ["Ask [person 1] about grade"] and "Oto" not in seen[0]
    assert "</a> - Ask Oto - Pending" in "\n".join(out)


def test_report_logs_summary_counts_and_survives_an_llm_failure():
    db = connect()
    prior(db)
    fake = world(jo("j1", "1", "TOC", task="Color grade the final cut"))
    log, out = [], []
    run_shift_report(db, fake, JOB_ORDERS, CONTACTS, PROJECTS, "1900", PM, False, out.append, log.append,
                     llm=transport_llm(Transport(status=500)))
    assert log[0].endswith("summaries=0 cached=0 fallback=1") and "Color grade" not in log[0]
    assert "</a> - Color grade the - Pending" in "\n".join(out)
    log.clear()
    run_shift_report(db, fake, JOB_ORDERS, CONTACTS, PROJECTS, "1900", PM, False, out.append, log.append,
                     llm=transport_llm(Transport()))
    assert log[0].endswith("summaries=1 cached=0 fallback=0")
    log.clear()
    run_shift_report(db, fake, JOB_ORDERS, CONTACTS, PROJECTS, "1900", PM, False, out.append, log.append,
                     llm=transport_llm(Transport(status=500)))
    assert log[0].endswith("summaries=0 cached=1 fallback=0")


def test_retention_deletes_summaries_unused_for_90_days_and_keeps_used_ones():
    conn = connect()
    old, recent = NOW - timedelta(days=91), NOW - timedelta(days=10)
    for page, used in (("old", old), ("recent", recent)):
        conn.execute("INSERT INTO jo_task_summaries VALUES (?, 'h', 'Label', ?, ?)",
                     (page, old.isoformat(), used.isoformat()))
    take_snapshot(conn, [], NOW, "1900")
    assert [r[0] for r in conn.execute("SELECT page_id FROM jo_task_summaries")] == ["recent"]
