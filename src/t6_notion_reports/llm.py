"""LLM client (spec section 11): OpenRouter, Claude Haiku by default, zero data retention enforced.

``LlmClient`` is the one interface the rest of the code sees; tests use a fake. Every request sent
by ``OpenRouterClient`` carries ``provider.zdr = true``, and a failed call is never retried, least
of all without it.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Callable, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
# JO shift report task summaries. A Qwen model with zero-data-retention endpoints on OpenRouter
# (Qwen Flash has none, so every ZDR call to it fails).
DEFAULT_SHIFT_MODEL = "qwen/qwen3.8-27b"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

T = TypeVar("T", bound=BaseModel)
# (url, headers, body) -> (status, parsed JSON body or None)
Transport = Callable[[str, dict, dict], "tuple[int, dict | None]"]


class LlmError(Exception):
    """The call failed or its answer did not validate. The message never holds prompt text or the key."""


class LlmClient(Protocol):
    def complete(self, system: str, user: str, schema: type[T]) -> T:
        """Ask for JSON matching ``schema`` and return it validated. Raises ``LlmError``."""
        ...


def _urllib_transport(url: str, headers: dict, body: dict) -> tuple[int, dict | None]:
    req = urllib.request.Request(url, json.dumps(body).encode(), headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, _json(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, None


def _json(raw: bytes) -> dict | None:
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class UnavailableLlm:
    """Stands in when no OpenRouter key is set: every call fails, so briefings say "unavailable"."""

    def complete(self, system: str, user: str, schema: type[T]) -> T:
        raise LlmError("OPENROUTER_API_KEY is not set")


class OpenRouterClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, transport: Transport = _urllib_transport):
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._model, self._transport = model, transport

    def complete(self, system: str, user: str, schema: type[T]) -> T:
        schema_text = json.dumps(schema.model_json_schema())
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": f"{system}\n\nAnswer with one JSON object matching this JSON Schema, and nothing else:\n{schema_text}"},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "provider": {"zdr": True},  # never dropped, never retried without
        }
        try:
            status, data = self._transport(OPENROUTER_URL, self._headers, body)
        except Exception as exc:  # network errors: type only, messages may carry the URL
            raise LlmError(f"request failed ({type(exc).__name__})") from None
        if status != 200 or not data:
            raise LlmError(f"OpenRouter answered status {status}")
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LlmError("OpenRouter answer has no message content") from None
        try:
            text = _strip_fence(content)
        except TypeError:
            raise LlmError("OpenRouter answer did not match the schema") from None
        try:
            return schema.model_validate_json(text)
        except ValidationError as exc:
            if not any(err["type"] == "json_invalid" for err in exc.errors()):
                raise LlmError("OpenRouter answer did not match the schema") from None
        # Not valid JSON: models sometimes leave double quotes unescaped inside a string (a quote
        # field quoting a phrase). Repair that one defect; anything else still fails.
        try:
            return schema.model_validate_json(_repair_quotes(text))
        except ValidationError:
            raise LlmError("OpenRouter answer did not match the schema") from None


def _strip_fence(text: object) -> str:
    if not isinstance(text, str):
        raise TypeError("content is not text")
    match = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text, re.DOTALL)
    return match.group(1) if match else text


_AFTER_STRING = {",", "}", "]", ":"}


def _repair_quotes(text: str) -> str:
    """Escape double quotes (and raw newlines) that sit inside a JSON string value.

    Inside a string, a quote closes it only if the next non-space character is ``, } ] :`` or the
    text ends; otherwise it is part of the content and gets a backslash. Valid escapes are kept.
    """
    out: list[str] = []
    in_string = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if not in_string:
            in_string = ch == '"'
            out.append(ch)
        elif ch == "\\" and i + 1 < n:
            out.append(text[i : i + 2])
            i += 1
        elif ch == '"':
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j >= n or text[j] in _AFTER_STRING:
                in_string = False
                out.append(ch)
            else:
                out.append('\\"')
        elif ch in "\n\r\t":
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[ch])
        else:
            out.append(ch)
        i += 1
    return "".join(out)
