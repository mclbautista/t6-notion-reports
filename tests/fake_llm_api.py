"""A fake t6 API client for ``llm run``: a script of outcomes, and the inputs it was sent."""
from __future__ import annotations

from t6.client import APIError, Unavailable
from t6.verbs import Done, LLMRunOutput, Refused


def ok(output, unresolved=(), valid=True, failure=None):
    return Done(result=LLMRunOutput(
        output=output, model="m", provider="p", prompt_hash="h", schema_hash="s", tokens={},
        zdr=True, output_valid=valid, failure=failure, unresolved=list(unresolved)).model_dump(mode="json"))


def invalid(failure="schema_violation"):
    return ok(None, valid=False, failure=failure)


def refused(rule="masking", failure=None, retry_after=None):
    return Refused(rule=rule, reason="no", failure=failure, retry_after=retry_after)


def unreachable():
    return Unavailable("connection refused")


class FakeLlmApi:
    """Each call takes the next scripted outcome (the last one repeats); a callable gets the inputs; an Exception is raised."""

    def __init__(self, *script):
        self.script, self.sent = list(script), []

    def llm_run(self, inputs):
        self.sent.append(inputs)
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        step = step(inputs) if callable(step) else step
        if isinstance(step, Exception):
            raise step
        return step


def api_error(status):
    return APIError("boom", status)
