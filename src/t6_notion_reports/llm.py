"""LLM access (spec section 11) through t6-core: typed tasks that need zero data retention.

``LlmClient`` is the one interface the rest of the code sees; tests use a fake. Every call is a
``t6.llm`` task with the strictest data policy (``zdr_enforced`` and ``pseudonymize``): the prompt
must be ``Masked`` text from a ``t6.privacy.Session``, t6-core sends ``provider.zdr = true`` to
OpenRouter, restores the names in the reply and fails the call on a leak or a leftover placeholder.
A failed call is never retried, so the caller shows "unavailable". Nothing here holds prompt text
in a log line: a trace has the task, the model, token counts, time and the failure reason.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

from pydantic import BaseModel
from urllib.parse import urljoin

from t6.llm import DECISIONS_URL, DataPolicy, Failed, Message, OpenAIChat, Provider, Request, Task, run
from t6.privacy import Masked, Session

DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
# JO shift report task summaries. A Qwen model with zero-data-retention endpoints on OpenRouter
# (Qwen Flash has none, so every ZDR call to it fails).
DEFAULT_SHIFT_MODEL = "qwen/qwen3.8-27b"
# The OpenRouter host comes from t6-core (ADR 0002: the app names no provider host itself).
OPENROUTER_BASE_URL = urljoin(DECISIONS_URL, "/api/v1")

T = TypeVar("T", bound=BaseModel)


class LlmError(Exception):
    """The call failed or its answer did not validate. The message is the failure reason only."""


class LlmClient(Protocol):
    def complete(self, tag: str, system: str, user: Masked, schema: type[T], session: Session) -> T:
        """Ask for JSON matching ``schema``. Returns it validated, with the names in ``session`` restored.

        ``tag`` names the prompt for the trace. Raises ``LlmError``.
        """
        ...


@dataclass(frozen=True)
class Prompt:
    """The task input: the whole user message, already masked."""

    text: Masked


def _render(system: str, schema: type[BaseModel]) -> Callable[[Prompt], Request]:
    schema_text = json.dumps(schema.model_json_schema())
    full = f"{system}\n\nAnswer with one JSON object matching this JSON Schema, and nothing else:\n{schema_text}"

    def render(prompt: Prompt) -> Request:
        return (Message(role="system", content=full), Message(role="user", content=prompt.text.text))

    return render


def openrouter_provider(api_key: str, model: str) -> Provider:
    """OpenRouter, declared ``zdr_enforced``: t6-core sends ``provider.zdr`` on every zero-retention task."""
    return OpenAIChat(base_url=OPENROUTER_BASE_URL, api_key=api_key, model=model, retention="zdr_enforced")


class T6Llm:
    def __init__(self, provider: Provider, log: Callable[[str], None] = lambda text: None):
        self._provider, self._log = provider, log

    def complete(self, tag: str, system: str, user: Masked, schema: type[T], session: Session) -> T:
        task = Task(tag, Prompt, schema, _render(system, schema), DataPolicy(), repair_quotes=True)
        result = run(task, Prompt(user), provider=self._provider, session=session)
        trace = result.trace
        reason = result.reason if isinstance(result, Failed) else "ok"
        self._log(
            f"llm task={trace.prompt_tag} model={trace.model} result={reason} tokens={trace.tokens.total} "
            f"latency_ms={trace.latency_ms} repaired={trace.repaired}"
        )
        if isinstance(result, Failed):
            raise LlmError(result.reason)
        return result.value


def openrouter_llm(api_key: str, model: str, log: Callable[[str], None] = lambda text: None) -> T6Llm:
    """The LLM client for one OpenRouter key and model."""
    return T6Llm(openrouter_provider(api_key, model), log)


class UnavailableLlm:
    """Stands in when no OpenRouter key is set: every call fails, so briefings say "unavailable"."""

    def complete(self, tag: str, system: str, user: Masked, schema: type[T], session: Session) -> T:
        raise LlmError("OPENROUTER_API_KEY is not set")
