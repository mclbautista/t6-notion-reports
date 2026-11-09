"""The ``TaskSummarizer`` seam: a short label for a Job Order's Task text (#73).

The report renderer only knows this protocol. Which implementation sits behind it (the fallback below,
a Haiku call, later a ``t6.llm`` task) is wiring, not rendering.
"""
from __future__ import annotations

from typing import Protocol

MAX_WORDS = 3
NO_TASK = "no task"


class TaskSummarizer(Protocol):
    def summarize(self, task_text: str) -> str | None:
        """A summary of at most three words, or None when there is none to give."""


def first_words(text: str, count: int = MAX_WORDS) -> str:
    return " ".join(text.split()[:count])


class FirstWordsSummarizer:
    """The fallback: the first three words of the Task. No network, no model."""

    def summarize(self, task_text: str) -> str | None:
        return first_words(task_text) or None


def task_label(task: object, summarizer: TaskSummarizer) -> str:
    """What a report line shows for a Task: the summarizer's reply when it is a valid one of at most
    three words, otherwise the first three words of the Task; an empty Task shows "no task"."""
    text = str(task or "").strip()
    if not text:
        return NO_TASK
    try:
        reply = summarizer.summarize(text)
    except Exception:  # a summarizer failure never stops a report
        reply = None
    reply = " ".join(reply.split()) if isinstance(reply, str) else ""
    return reply if reply and len(reply.split()) <= MAX_WORDS else first_words(text)
