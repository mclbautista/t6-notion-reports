"""Haiku task summaries behind the ``TaskSummarizer`` seam, with a cache (#73, #77).

``LlmTaskSummarizer`` sends only the Task text, masked by t6-core's pseudonymizer and restored after.
``summarize_tasks`` is the report job's step between the snapshot and the render: it fills the
``jo_task_summaries`` cache so a Task is never summarised twice, and hands the renderer a lookup.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel
from t6.privacy import Masked

from .llm import LlmClient, LlmError
from .masking import Masker
from .task_summary import MAX_WORDS, TaskSummarizer

SYSTEM = (
    "You label a Job Order's task for a shift report. Reply with a summary of the task in at most "
    f"{MAX_WORDS} words, as the JSON field \"summary\". Keep any placeholder such as [person 1] exactly as written."
)


class SummaryReply(BaseModel):
    summary: str


class LlmTaskSummarizer:
    def __init__(self, client: LlmClient, masker: Masker | None = None):
        self._client, self._masker = client, masker or Masker()

    def summarize(self, task_text: str) -> str | None:
        session = self._masker.session()
        try:
            reply = self._client.complete("task-summary-v1", SYSTEM, session.mask(task_text), SummaryReply, session)
        except LlmError:
            return None
        return reply.summary


@dataclass(frozen=True)
class SummaryStats:
    new: int = 0
    cached: int = 0
    fallback: int = 0


class _PageSummaries:
    """What one Job Order's line may show: its own summary for its own current Task text, else None."""

    def __init__(self, found: tuple[str, str] | None):
        self._found = found

    def summarize(self, task_text: str) -> str | None:
        return self._found[1] if self._found and self._found[0] == task_text.strip() else None


class SummaryLookup:
    """Summaries already made, by Job Order page id. ``for_page`` gives the ``TaskSummarizer`` for one
    page, so a fallback on one JO never shows another JO's summary of identical Task text. Anything not
    found is None (the renderer then falls back to the first three words)."""

    def __init__(self, by_page: dict[str, tuple[str, str]]):
        self._by_page = by_page  # page id -> (Task text, summary)

    def for_page(self, page_id: str) -> TaskSummarizer:
        return _PageSummaries(self._by_page.get(page_id))


def valid(summary: object) -> str | None:
    text = " ".join(summary.split()) if isinstance(summary, str) else ""
    return text if text and len(text.split()) <= MAX_WORDS else None


def summarize_tasks(
    conn: sqlite3.Connection, summarizer: TaskSummarizer, tasks: dict[str, object], now: datetime,
) -> tuple[SummaryLookup, SummaryStats]:
    """``tasks``: page id -> Task text. A cached (page id, hash of Task) makes no call; a new valid
    reply is stored (older Tasks' rows stay until retention removes them, so A, B, A makes two calls); an empty, too long or failed reply is a fallback and is not stored."""
    found: dict[str, tuple[str, str]] = {}
    new = cached = fallback = 0
    stamp = now.isoformat()
    with conn:
        for page_id, task in tasks.items():
            text = str(task or "").strip()
            if not text:
                continue
            digest = hashlib.sha256(text.encode()).hexdigest()
            row = conn.execute("SELECT summary FROM jo_task_summaries WHERE page_id = ? AND task_hash = ?",
                               (page_id, digest)).fetchone()
            if row:
                conn.execute("UPDATE jo_task_summaries SET last_used_at = ? WHERE page_id = ? AND task_hash = ?",
                             (stamp, page_id, digest))
                found[page_id] = (text, row[0])
                cached += 1
                continue
            try:
                summary = valid(summarizer.summarize(text))
            except Exception:  # an LLM failure never stops a report
                summary = None
            if summary is None:
                fallback += 1
                continue
            conn.execute("INSERT INTO jo_task_summaries VALUES (?, ?, ?, ?, ?)", (page_id, digest, summary, stamp, stamp))
            found[page_id] = (text, summary)
            new += 1
    return SummaryLookup(found), SummaryStats(new, cached, fallback)
