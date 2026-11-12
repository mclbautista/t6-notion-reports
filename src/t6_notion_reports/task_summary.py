"""The ``TaskSummarizer`` seam: a short label for a Job Order's Task text (#73).

The report renderer only knows this protocol. Which implementation sits behind it (the fallback below,
a Haiku call, later a ``t6.llm`` task) is wiring, not rendering.
"""
from __future__ import annotations

import re
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


LIST_NUMBER = re.compile(r"^\s*\(?\d+[.)]\s+")


def task_label(task: object, summarizer: TaskSummarizer) -> str:
    """What a report line shows for a Task: the summarizer's reply when it is a valid one of at most
    three words, otherwise the first three words of the Task; an empty Task shows "no task". A leading
    list number ("1. process D15") is dropped and what is left starts with a capital letter."""
    text = str(task or "").strip()
    if not text:
        return NO_TASK
    label = _label(text, summarizer)
    unnumbered = LIST_NUMBER.sub("", label)
    return unnumbered[:1].upper() + unnumbered[1:] if unnumbered and unnumbered != label else label


def _label(text: str, summarizer: TaskSummarizer) -> str:
    try:
        reply = summarizer.summarize(text)
    except Exception:  # a summarizer failure never stops a report
        reply = None
    reply = " ".join(reply.split()) if isinstance(reply, str) else ""
    return reply if reply and len(reply.split()) <= MAX_WORDS else first_words(text)


def for_page(summarizer: TaskSummarizer, page_id: str) -> TaskSummarizer:
    """The summarizer to use for one Job Order: a per-page lookup when the summarizer has one."""
    scoped = getattr(summarizer, "for_page", None)
    return scoped(page_id) if scoped else summarizer
