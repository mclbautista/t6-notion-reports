"""The Concern verbs, through the t6 API (``concern observe``, ``scan``, ``list``, ``record-message``).

Concerns are shared information: the API keeps them, reports keeps none. A call that is not answered, or is
refused, raises ``ConcernApiError``, an ``OSError``; its message names the verb and the rule, never a Concern
text, a name or an id. Each call has its own time limit: the brief is never held for the API.
"""
from __future__ import annotations

import copy
import hashlib
from typing import Any

from t6.client import APIError, Unavailable
from t6.verbs import ConcernFinding, ConcernObserveInput, ConcernUnavailable, Done, Refused

OBSERVE_TIMEOUT = 30


class ConcernApiError(OSError):
    """The API did not do the call: unreachable, refused or an error."""


def limited(client, seconds: float):
    """The same client with its own time limit for each call."""
    other = copy.copy(client)
    other.timeout = seconds
    return other


def call(what: str, method, inputs) -> dict[str, Any]:
    try:
        outcome = method(inputs)
    except APIError as exc:
        raise ConcernApiError(f"{what} http status={exc.status}") from None
    if isinstance(outcome, Unavailable):
        raise ConcernApiError(f"{what} unreachable")
    if isinstance(outcome, Refused):
        raise ConcernApiError(f"{what} refused rule={outcome.rule}")
    if not isinstance(outcome, Done):
        raise ConcernApiError(f"{what} answered {outcome.outcome}")
    return outcome.result


def observe(
    client, run_date: str, findings: list[tuple[str, str, str, str | None]], unavailable: set[tuple[str, str]],
    dry_run: bool,
) -> dict[str, Any]:
    """One ``concern observe`` for the run. ``findings`` are (project, text, source, owner); the reply holds, for each
    project, its open Concerns and the ones closed since the last run. The API closes nothing here."""
    inputs = ConcernObserveInput(
        run_date=run_date, dry_run=dry_run, idempotency_key="",
        findings=[ConcernFinding(project=p, text=t, source=s, owner=o) for p, t, s, o in findings],
        unavailable=[ConcernUnavailable(project=p, source=s) for p, s in sorted(unavailable)],
    )
    # The key is per day and per request: a rerun of the same brief gets the same answer, a rerun whose findings
    # differ is a new request, not a refused reuse of the key.
    digest = hashlib.sha256(inputs.model_dump_json(exclude={"idempotency_key", "dry_run"}).encode()).hexdigest()[:8]
    inputs = inputs.model_copy(update={"idempotency_key": f"psr-concerns-{run_date}-{digest}"})
    return call("concern observe", limited(client, OBSERVE_TIMEOUT).concern_observe, inputs)
