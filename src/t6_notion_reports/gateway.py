"""Client for the Sixto gateway's ``POST /notify`` (spec section 10)."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable

# (url, headers, body) -> (status, parsed JSON body or None). A ``None`` body means GET, else POST.
Transport = Callable[[str, dict, "dict | None"], "tuple[int, dict | None]"]

# What delivery_status() returns: the gateway's three states, or "unknown" when it can't tell.
DELIVERY_STATES = ("queued", "sent", "failed")
UNKNOWN = "unknown"


class GatewayError(Exception):
    """A send failed after all retries. The message never holds the secret or the text."""


def _urllib_transport(url: str, headers: dict, body: dict | None) -> tuple[int, dict | None]:
    if body is None:
        req = urllib.request.Request(url, None, headers, method="GET")
    else:
        req = urllib.request.Request(url, json.dumps(body).encode(), headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, _json(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, None


def _json(raw: bytes) -> dict | None:
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class GatewayClient:
    def __init__(
        self,
        base_url: str,
        secret: str,
        transport: Transport = _urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        retries: int = 3,
        backoff: float = 2.0,
    ):
        self._url = base_url.rstrip("/") + "/notify"
        self._headers = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}
        self._transport, self._sleep = transport, sleep
        self._retries, self._backoff = retries, backoff

    def notify(self, to: int, text: str, idempotency_key: str, topic: int | None = None) -> int:
        """Queue one HTML message. Returns the gateway's ``message_ref``.

        Anything but ``202`` is retried ``retries`` times with exponential backoff; then raises.
        """
        body: dict = {"to": to, "text": text, "format": "html", "idempotency_key": idempotency_key}
        if topic is not None:
            body["topic"] = topic
        last = "no response"
        for attempt in range(self._retries + 1):
            if attempt:
                self._sleep(self._backoff * 2 ** (attempt - 1))
            try:
                status, data = self._transport(self._url, self._headers, dict(body))
            except Exception as exc:  # network errors; type only, messages may carry the URL
                last = type(exc).__name__
                continue
            if status == 202 and data and "message_ref" in data:
                return data["message_ref"]
            last = f"status {status}"
        raise GatewayError(f"gateway rejected key={idempotency_key} after {self._retries + 1} attempts ({last})")

    def delivery_status(self, message_ref: int) -> str:
        """``queued``, ``sent`` or ``failed`` for one ``message_ref`` (``GET /notify/{ref}``).

        Never raises: a transport error, a non-200 status or an unexpected body is ``"unknown"``.
        One attempt, no retries; the caller polls.
        """
        try:
            status, data = self._transport(f"{self._url}/{int(message_ref)}", dict(self._headers), None)
        except Exception:
            return UNKNOWN
        # The body key is not pinned down in the spec: accept {"state": ..} or {"status": ..}.
        state = (data.get("state") or data.get("status")) if status == 200 and isinstance(data, dict) else None
        return state if isinstance(state, str) and state in DELIVERY_STATES else UNKNOWN
