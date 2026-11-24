"""Task summaries: the summarizer over a fake ``llm run`` API, the cache and its retention (#77)."""
import hashlib
import re

import pytest
from datetime import datetime, timedelta, timezone

from fake_llm_api import FakeLlmApi, invalid, ok, refused, unreachable
from t6_notion_reports.jo_snapshots import connect, take_snapshot
from t6_notion_reports.llm_api import ApiLlm
from t6_notion_reports.shift_job import run_shift_report
from t6_notion_reports.task_summary_llm import SYSTEM, LlmTaskSummarizer, summarize_tasks
from test_shift_report import PM, jo, prior, world

NOW = datetime(2026, 11, 3, 19, tzinfo=timezone(timedelta(hours=8)))


def summarizer(api):
    return LlmTaskSummarizer(ApiLlm(api, "shift_task_summary", "m", sleep=lambda _: None))


def reply(text="Color grade"):
    return ok({"summary": text})


def test_valid_reply_is_returned_and_only_the_task_is_sent_through_llm_run():
    api = FakeLlmApi(reply())
    assert summarizer(api).summarize("Color grade the final cut") == "Color grade"
    sent = api.sent[0]
    assert sent.input_text == "Color grade the final cut"  # the API masks; the app sends the text as it is
    assert sent.require_zdr and sent.temperature_zero and sent.masking_preset == "strict"
    assert sent.restore == "per_field" and sent.repair_quotes and sent.allow_projects == []
    assert sent.task_name == "shift_task_summary" and sent.model_hint == "m"
    assert sent.prompt_template.endswith("\n\n{input}") and "placeholder" not in sent.prompt_template


FAILURES = {
    "refused": refused("masking"),
    "provider error": refused("provider", "api_error"),
    "timeout": refused("provider", "timeout"),
    "output invalid": invalid(),
    "unreachable": unreachable(),
    "unresolved placeholder": ok({"summary": "Ask [person 9]"}, unresolved=["summary"]),
}


@pytest.mark.parametrize("failure", FAILURES.values(), ids=FAILURES.keys())
def test_every_failure_gives_no_summary_and_the_report_shows_three_words(failure):
    conn = connect()
    lookup, stats = summarize_tasks(conn, summarizer(FakeLlmApi(failure)), {"p1": "Color grade the final cut"}, NOW)
    assert (stats.new, stats.cached, stats.fallback) == (0, 0, 1)
    assert lookup.for_page("p1").summarize("Color grade the final cut") is None
    assert conn.execute("SELECT COUNT(*) FROM jo_task_summaries").fetchone()[0] == 0


def test_four_word_reply_becomes_a_fallback_and_is_not_cached():
    conn = connect()
    for api in (FakeLlmApi(reply("one two three four")),):
        lookup, stats = summarize_tasks(conn, summarizer(api), {"p1": "Color grade the final cut"}, NOW)
        assert (stats.new, stats.cached, stats.fallback) == (0, 0, 1)
        assert lookup.for_page("p1").summarize("Color grade the final cut") is None
    assert conn.execute("SELECT COUNT(*) FROM jo_task_summaries").fetchone()[0] == 0


def test_cached_jo_makes_no_call_and_a_changed_task_is_summarised_again():
    conn = connect()
    t = FakeLlmApi(reply())
    s = summarizer(t)
    _, stats = summarize_tasks(conn, s, {"p1": "Color grade the final cut"}, NOW)
    assert (stats.new, stats.cached) == (1, 0)
    lookup, stats = summarize_tasks(conn, s, {"p1": "Color grade the final cut"}, NOW)
    assert (stats.new, stats.cached, stats.fallback) == (0, 1, 0) and len(t.sent) == 1
    assert lookup.for_page("p1").summarize("Color grade the final cut") == "Color grade"
    _, stats = summarize_tasks(conn, s, {"p1": "Fix the audio sync"}, NOW)
    assert stats.new == 1 and len(t.sent) == 2
    assert conn.execute("SELECT COUNT(*) FROM jo_task_summaries").fetchone()[0] == 2  # the old Task's summary stays cached


def test_the_prompt_puts_deliverable_codes_and_day_or_episode_numbers_first():
    for code in ("FTR", "Ep1", "TLR", "TSR", "IMF", "DCP", "D23-24", "Ep1-4", "Ep2"):
        assert code in SYSTEM


def test_a_summary_cached_under_an_older_prompt_is_made_again():
    conn = connect()
    old_hash = hashlib.sha256(b"Export DCP for FTR D23-24").hexdigest()  # the key before the prompt counted
    conn.execute("INSERT INTO jo_task_summaries VALUES (?, ?, ?, ?, ?)",
                 ("p1", old_hash, "Export the DCP", NOW.isoformat(), NOW.isoformat()))
    t = FakeLlmApi(reply("FTR DCP D23-24"))
    lookup, stats = summarize_tasks(conn, summarizer(t), {"p1": "Export DCP for FTR D23-24"}, NOW)
    assert (stats.new, stats.cached) == (1, 0) and len(t.sent) == 1
    assert lookup.for_page("p1").summarize("Export DCP for FTR D23-24") == "FTR DCP D23-24"


def test_task_a_then_b_then_a_again_reuses_the_cached_summary_of_a():
    conn = connect()
    t = FakeLlmApi(reply())
    s = summarizer(t)
    for task in ("Color grade the final cut", "Fix the audio sync"):
        summarize_tasks(conn, s, {"p1": task}, NOW)
    assert len(t.sent) == 2
    lookup, stats = summarize_tasks(conn, s, {"p1": "Color grade the final cut"}, NOW)
    assert (stats.new, stats.cached, stats.fallback) == (0, 1, 0) and len(t.sent) == 2
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
    run_shift_report(db, fake, "1900", PM, False, out.append, log.append,
                     summarizer=FirstOnly())
    assert log[0].endswith("summaries=1 cached=0 fallback=1")
    labels = re.findall(r"#(\d+)</a> - (.+?) - Pending", "\n".join(out))
    assert sorted(label for _, label in labels) == ["Color grade", "Export the final"]


def test_the_report_sends_the_task_text_as_it_is_and_shows_the_restored_summary():
    fake = world(jo("j1", "1", "TOC", task="Ask Oto about grade"))
    api = FakeLlmApi(ok({"summary": "Ask Oto"}))
    db = connect()
    prior(db)
    out = []
    run_shift_report(db, fake, "1900", PM, False, out.append, lambda _: None,
                     llm=ApiLlm(api, "shift_task_summary", "m"))
    assert [i.input_text for i in api.sent] == ["Ask Oto about grade"]
    assert "</a> - Ask Oto - Pending" in "\n".join(out)


def test_report_logs_summary_counts_and_survives_an_llm_failure():
    db = connect()
    prior(db)
    fake = world(jo("j1", "1", "TOC", task="Color grade the final cut"))
    log, out = [], []
    run_shift_report(db, fake, "1900", PM, False, out.append, log.append,
                     llm=ApiLlm(FakeLlmApi(refused("provider", "api_error")), "t", "m", sleep=lambda _: None))
    assert log[0].endswith("summaries=0 cached=0 fallback=1") and "Color grade" not in log[0]
    assert "</a> - Color grade the - Pending" in "\n".join(out)
    log.clear()
    run_shift_report(db, fake, "1900", PM, False, out.append, log.append,
                     llm=ApiLlm(FakeLlmApi(reply()), "t", "m"))
    assert log[0].endswith("summaries=1 cached=0 fallback=0")
    log.clear()
    run_shift_report(db, fake, "1900", PM, False, out.append, log.append,
                     llm=ApiLlm(FakeLlmApi(refused("provider", "api_error")), "t", "m", sleep=lambda _: None))
    assert log[0].endswith("summaries=0 cached=1 fallback=0")


def test_retention_deletes_summaries_unused_for_90_days_and_keeps_used_ones():
    conn = connect()
    old, recent = NOW - timedelta(days=91), NOW - timedelta(days=10)
    for page, used in (("old", old), ("recent", recent)):
        conn.execute("INSERT INTO jo_task_summaries VALUES (?, 'h', 'Label', ?, ?)",
                     (page, old.isoformat(), used.isoformat()))
    take_snapshot(conn, [], NOW, "1900")
    assert [r[0] for r in conn.execute("SELECT page_id FROM jo_task_summaries")] == ["recent"]


def test_once_a_call_failed_on_a_timeout_the_rest_of_the_slot_makes_no_call():
    from t6_notion_reports.llm_api import LlmUnavailable

    class Down:
        def __init__(self):
            self.asked = []

        def summarize(self, text):
            self.asked.append(text)
            raise LlmUnavailable("timeout")

    s = Down()
    lookup, stats = summarize_tasks(connect(), s, {f"p{n}": f"Task {n}" for n in range(15)}, NOW)
    assert len(s.asked) == 1 and stats.fallback == 15 and stats.new == 0


def test_after_the_total_budget_no_more_calls_are_made_and_the_rest_fall_back():
    class Slow:
        def __init__(self):
            self.asked = 0

        def summarize(self, text):
            self.asked += 1
            clock[0] += 120
            return "Grade the cut"

    clock = [0.0]
    s = Slow()
    _, stats = summarize_tasks(connect(), s, {f"p{n}": f"Task {n}" for n in range(10)}, NOW, budget=300, monotonic=lambda: clock[0])
    assert s.asked == 3 and stats.new == 3 and stats.fallback == 7


def test_a_schema_mismatch_is_one_fallback_and_does_not_stop_the_rest():
    from t6_notion_reports.llm_api import LlmError

    class Odd:
        def __init__(self):
            self.asked = 0

        def summarize(self, text):
            self.asked += 1
            if self.asked == 1:
                raise LlmError("did not match")
            return "Grade the cut"

    s = Odd()
    _, stats = summarize_tasks(connect(), s, {"p1": "Task 1", "p2": "Task 2"}, NOW)
    assert s.asked == 2 and stats.new == 1 and stats.fallback == 1


def test_a_timeout_that_outlasts_its_attempts_is_unavailable_to_the_summarizer():
    from t6_notion_reports.llm_api import LlmUnavailable
    with pytest.raises(LlmUnavailable):
        summarizer(FakeLlmApi(refused("provider", "timeout"))).summarize("Color grade the final cut")
