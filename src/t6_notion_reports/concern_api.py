"""The Concern verbs, through the t6 API (``concern observe``, ``list``, ``record-message``).

Concerns are shared information: the API keeps them, reports keeps none. A call that is not answered, or is
refused, raises ``ConcernApiError``, an ``OSError``; its message names the verb and the rule, never a Concern
text, a name or an id. Each call has its own time limit: the brief is never held for the API.
"""
from __future__ import annotations

import copy
import hashlib
from typing import Any

from t6.client import APIError, Unavailable
from t6.verbs import (
    ConcernFinding, ConcernListInput, ConcernObserveInput, ConcernRecordMessageInput, ConcernUnavailable,
    Done, Preview, Refused,
)

OBSERVE_TIMEOUT = 30
LIST_TIMEOUT = 30
RECORD_TIMEOUT = 20


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


def open_concerns(client) -> list[dict[str, Any]]:
    """Every Concern that is not closed (``concern list`` with ``open``), oldest first: id, project_id, owner_id,
    waiting_on, text, state ("open" or "snoozed") and more."""
    return call("concern list", limited(client, LIST_TIMEOUT).concern_list, ConcernListInput(open=True))["concerns"]


def raise_at_psr_concerns(client) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The Concerns for the brief (``concern list`` with ``raise_at_psr``): the open Concerns, and the pending closes
    ("Closing after PSR"). Each has id, project_id, owner_id, waiting_on, age_seconds and evidence_at; the reply has no text.
    The pending closes are the reply's second list, ``pending``; an API without it has none."""
    reply = call("concern list", limited(client, LIST_TIMEOUT).concern_list, ConcernListInput(raise_at_psr=True))
    return reply["concerns"], reply.get("pending") or []


def record_message(client, concern: str, chat: int, message_id: int, brief_date: str, key: str) -> None:
    """``concern record-message``: the Telegram message ``message_id`` of ``chat`` carried ``concern`` in the brief of
    ``brief_date``. The verb is two-step: the first call answers a preview with a token, and the same call with the same
    key and ``confirm`` set to that token does it. A repeat of a done call is answered as done."""
    method = limited(client, RECORD_TIMEOUT).concern_record_message
    inputs = ConcernRecordMessageInput(
        concern=concern, chat=str(chat), message_id=str(message_id), brief_date=brief_date, idempotency_key=key,
    )
    try:
        outcome = method(inputs)
        if isinstance(outcome, Preview):
            outcome = method(inputs.model_copy(update={"confirm": outcome.token}))
    except APIError as exc:
        raise ConcernApiError(f"concern record-message http status={exc.status}") from None
    if isinstance(outcome, Unavailable):
        raise ConcernApiError("concern record-message unreachable")
    if isinstance(outcome, Refused):
        raise ConcernApiError(f"concern record-message refused rule={outcome.rule}")
    if not isinstance(outcome, Done):
        raise ConcernApiError(f"concern record-message answered {outcome.outcome}")
