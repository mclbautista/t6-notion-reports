"""Test doubles for the LLM seam (``t6_notion_reports.llm.LlmClient``).

``LegacyLlm`` runs a fake with the old ``complete(system, user, schema)`` shape behind the seam and
restores the names in its reply the way a t6 task does. ``transport_llm`` builds the real client
over a fake HTTP transport ``(url, headers, body) -> (status, parsed JSON)``.
"""
import json
import urllib.error

from t6.llm import OpenAIChat
from t6.privacy import Masked, Registry, Session, Unresolved

from t6_notion_reports.llm import LlmError, T6Llm


class LegacyLlm:
    def __init__(self, fake):
        self.fake = fake

    def complete(self, tag, system, user: Masked, schema, session):
        value = self.fake.complete(system, user.text, schema)
        if session.leaks(value):
            raise LlmError("leak")
        try:
            return session.restore(value)
        except Unresolved as exc:
            raise LlmError("unresolved_placeholder") from exc


class AskLlm:
    """Base for fakes written as ``ask(system, user, schema)``: the seam's call, with the reply restored."""

    check_leaks = False  # True: a raw name in the reply fails the call, as a t6 task does

    def complete(self, tag, system, user, schema, session):
        value = self.ask(system, user.text, schema)
        if self.check_leaks and session.leaks(value):
            raise LlmError("leak")
        try:
            return session.restore(value)
        except Unresolved as exc:
            raise LlmError("unresolved_placeholder") from exc


def llm_of(fake):
    """``fake`` as an ``LlmClient``; None and objects that already are one pass through."""
    if fake is None or not hasattr(fake, "complete") or isinstance(fake, (LegacyLlm, T6Llm)):
        return fake
    return LegacyLlm(fake)


def transport_llm(transport, model="test-model", log=lambda text: None):
    def send(url, headers, body, timeout):
        status, data = transport(url, headers, json.loads(body))
        if status != 200:
            raise urllib.error.HTTPError(url, status, "error", {}, None)
        return json.dumps(data).encode()

    provider = OpenAIChat(base_url="https://openrouter.ai/api/v1", api_key="key", model=model,
                          retention="zdr_enforced", send=send)
    return T6Llm(provider, log)


def blank_session():
    return Session(Registry())
