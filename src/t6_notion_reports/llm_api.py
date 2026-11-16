"""The one LLM adapter: every task runs through ``llm run`` on the t6 API.

The API masks the text, runs the model (zero retention, temperature 0), repairs quotes and restores
names; this app holds no model key. ``ApiLlm`` is the ``LlmClient`` the tasks use. A failed call raises
``LlmError``; its message names the rule and the failure class, never text.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Generic, Mapping, Protocol, Sequence, TypeVar

from pydantic import BaseModel, ValidationError
from t6.client import APIError, Client, Unavailable
from t6.verbs import Done, LLMRunInput, LLMRunOutput, Refused

T = TypeVar("T", bound=BaseModel)

LLM_TIMEOUT = 90  # per call; the server's own provider timeout is 60
RETRY_DELAYS = (2.0, 5.0)  # a failed call is tried again after these waits, then gives up
RETRY_AFTER_MAX = 30.0
RETRY_FAILURES = {"timeout", "api_error"}  # refusals that may pass on their own


class LlmError(Exception):
    """The call failed or its answer did not validate. The message never holds prompt text."""


@dataclass(frozen=True)
class Reply(Generic[T]):
    """The validated answer. ``dropped``: list items left out because a field still held a placeholder the API could not restore."""

    value: T
    dropped: int = 0


class LlmClient(Protocol):
    def complete(self, system: str, user: str, schema: type[T], *, allow_projects: Sequence[str] = ()) -> Reply[T]:
        """Ask for JSON matching ``schema`` and return it validated. Raises ``LlmError``."""
        ...


class UnavailableLlm:
    """Stands in when LLM calls are off: every call fails, so briefings say "unavailable"."""

    def complete(self, system: str, user: str, schema: type[T], *, allow_projects: Sequence[str] = ()) -> Reply[T]:
        raise LlmError("LLM calls are turned off")


def client_from_env(env: Mapping[str, str] | None = None) -> Client | None:
    """A client for ``llm run`` (90 s per call); None when ``T6_API_URL`` is unset."""
    env = os.environ if env is None else env
    if not env.get("T6_API_URL", "").strip():
        return None
    return Client(env["T6_API_URL"].strip(), env.get("T6_API_SECRET", ""), timeout=LLM_TIMEOUT)


class ApiLlm:
    def __init__(self, client, task_name: str, model_hint: str, masking_preset: str = "strict", *,
                 allow_projects: Sequence[str] = (), repair_quotes: bool = True, restore: str = "per_field",
                 log: Callable[[str], None] = lambda _: None,
                 sleep: Callable[[float], None] = time.sleep):
        self._client, self._task, self._model, self._preset = client, task_name, model_hint, masking_preset
        self._allow, self._repair, self._restore = tuple(allow_projects), repair_quotes, restore
        self._log, self._sleep = log, sleep
        self._down = False  # the API did not answer at all: later calls of this run do not wait again

    def complete(self, system: str, user: str, schema: type[T], *, allow_projects: Sequence[str] = ()) -> Reply[T]:
        inputs = LLMRunInput(
            task_name=self._task, prompt_template=system + "\n\n{input}", input_text=user,
            output_schema=schema.model_json_schema(), masking_preset=self._preset, model_hint=self._model,
            require_zdr=True, temperature_zero=True, allow_projects=[*self._allow, *allow_projects],
            repair_quotes=self._repair, restore=self._restore)
        output = self._run(inputs)
        if output.unresolved:
            self._log(f"llm task={self._task} unresolved={len(output.unresolved)}")
        data, dropped = _drop_unresolved(output.output, output.unresolved, self._task)
        try:
            value = schema.model_validate(data)
        except ValidationError:
            raise LlmError(f"{self._task}: answer did not match the schema") from None
        return Reply(value, dropped)

    def _run(self, inputs: LLMRunInput) -> LLMRunOutput:
        if self._down:
            raise LlmError(f"{self._task}: API unreachable")
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                return self._once(inputs)
            except _Retry as again:
                if attempt == len(RETRY_DELAYS):
                    self._down = again.unreachable
                    raise LlmError(str(again)) from None
                self._sleep(min(again.wait or RETRY_DELAYS[attempt], RETRY_AFTER_MAX))
        raise AssertionError("unreachable")

    def _once(self, inputs: LLMRunInput) -> LLMRunOutput:
        """One call. Raises ``_Retry`` when it may pass on a second try, ``LlmError`` when it will not."""
        try:
            outcome = self._client.llm_run(inputs)
        except APIError as exc:
            note = f"API error status={exc.status}"
            self._log(f"llm task={self._task} {note}")
            if exc.status is None or exc.status == 429 or exc.status >= 500:
                raise _Retry(f"{self._task}: {note}") from None
            raise LlmError(f"{self._task}: {note}") from None
        if isinstance(outcome, Unavailable):
            self._log(f"llm task={self._task} api unreachable")
            raise _Retry(f"{self._task}: API unreachable", unreachable=True)
        if isinstance(outcome, Refused):
            note = f"refused rule={outcome.rule} failure={outcome.failure or 'none'}"
            self._log(f"llm task={self._task} {note}")
            if outcome.failure in RETRY_FAILURES:
                raise _Retry(f"{self._task}: {note}", outcome.retry_after or 0.0)
            raise LlmError(f"{self._task}: {note}")
        if not isinstance(outcome, Done):
            raise LlmError(f"{self._task}: API answered {outcome.outcome}")
        output = LLMRunOutput.model_validate(outcome.result)
        if not output.output_valid:
            self._log(f"llm task={self._task} output invalid failure={output.failure}")
            raise LlmError(f"{self._task}: output invalid failure={output.failure}")
        return output


_STEP = re.compile(r"\.?([^.\[\]]+)|\[(\d+)\]")


def _drop_unresolved(data: Any, paths: Sequence[str], task: str) -> tuple[Any, int]:
    """Remove each list item that holds an unresolved field (path ``changes[2].text`` drops ``changes[2]``), as one
    bad placeholder drops one item. A field outside any list cannot be dropped: the answer fails."""
    doomed: dict[str, set[int]] = {}
    for path in paths:
        head, bracket, rest = path.partition("[")
        if not bracket:
            raise LlmError(f"{task}: a field kept an unresolved placeholder")
        doomed.setdefault(head, set()).add(int(rest.partition("]")[0]))
    for head, indexes in doomed.items():
        node = data
        for key, _ in (m.groups() for m in _STEP.finditer(head)):
            node = node[key]
        for index in sorted(indexes, reverse=True):
            del node[index]
    return data, sum(map(len, doomed.values()))


class _Retry(Exception):
    def __init__(self, message: str, wait: float = 0.0, unreachable: bool = False):
        super().__init__(message)
        self.wait, self.unreachable = wait, unreachable
