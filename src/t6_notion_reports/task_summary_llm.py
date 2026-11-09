"""Haiku task summaries behind the ``TaskSummarizer`` seam, with a cache (#73, #77).

``LlmTaskSummarizer`` sends only the Task text, masked by ``redact`` and restored after. ``summarize_tasks``
is the report job's step between the snapshot and the render: it fills the ``jo_task_summaries`` cache so
a Task is never summarised twice, and hands the renderer a lookup. Moving onto the t6 adapter (#68) swaps
``LlmTaskSummarizer`` and its wiring only.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel

from .llm import LlmClient, LlmError
from .redact import Mapping, Redactor, unresolved
from .task_summary import MAX_WORDS, TaskSummarizer

SYSTEM = (
    "You label a Job Order's task for a shift report. Reply with a summary of the task in at most "
    f"{MAX_WORDS} words, as the JSON field \"summary\". Keep any placeholder such as [person 1] exactly as written."
)


class SummaryReply(BaseModel):
    summary: str


class LlmTaskSummarizer:
    def __init__(self, client: LlmClient, redactor: Redactor | None = None):
        self._client, self._redactor = client, redactor or Redactor()

    def summarize(self, task_text: str) -> str | None:
        mapping = Mapping()
        masked = self._redactor.redact(task_text, "", mapping)
        try:
            reply = self._client.complete(SYSTEM, masked, SummaryReply)
        except LlmError:
            return None
        restored = mapping.restore(reply.summary)
        return None if unresolved(restored) else restored


@dataclass(frozen=True)
class SummaryStats:
    new: int = 0
    cached: int = 0
    fallback: int = 0


class SummaryLookup:
    """A ``TaskSummarizer`` that only answers from summaries already made; anything else is None
    (the renderer then falls back to the first three words)."""

    def __init__(self, by_task: dict[str, str]):
        self._by_task = by_task

    def summarize(self, task_text: str) -> str | None:
        return self._by_task.get(task_text.strip())


def valid(summary: object) -> str | None:
    text = " ".join(summary.split()) if isinstance(summary, str) else ""
    return text if text and len(text.split()) <= MAX_WORDS else None


def summarize_tasks(
    conn: sqlite3.Connection, summarizer: TaskSummarizer, tasks: dict[str, object], now: datetime,
) -> tuple[SummaryLookup, SummaryStats]:
    """``tasks``: page id -> Task text. A cached (page id, hash of Task) makes no call; a new valid
    reply is stored; an empty, too long or failed reply is a fallback and is not stored."""
    found: dict[str, str] = {}
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
                found[text] = row[0]
                cached += 1
                continue
            try:
                summary = valid(summarizer.summarize(text))
            except Exception:  # an LLM failure never stops a report
                summary = None
            if summary is None:
                fallback += 1
                continue
            conn.execute("DELETE FROM jo_task_summaries WHERE page_id = ?", (page_id,))  # an older Task's summary
            conn.execute("INSERT INTO jo_task_summaries VALUES (?, ?, ?, ?, ?)", (page_id, digest, summary, stamp, stamp))
            found[text] = summary
            new += 1
    return SummaryLookup(found), SummaryStats(new, cached, fallback)
