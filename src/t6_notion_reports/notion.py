"""Minimal official-Notion-API client: the Minutes writes. Every read goes through the t6 API (t6_api.py)."""
from __future__ import annotations

import json
import urllib.request
from typing import Callable

API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"

Transport = Callable[[str, dict, dict], dict]


def _urllib_transport(url: str, headers: dict, body: dict, method: str = "POST") -> dict:
    data = None if method == "GET" else json.dumps(body).encode()
    req = urllib.request.Request(url, data, headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


class NotionClient:
    """POST calls go to ``transport(url, headers, body)``; GET and PATCH pass the method as a 4th argument."""

    def __init__(self, token: str, transport: Transport = _urllib_transport):
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self._transport = transport

    def _call(self, method: str, path: str, body: dict) -> dict:
        if method == "POST":
            return self._transport(f"{API}{path}", self._headers, body)
        return self._transport(f"{API}{path}", self._headers, body, method)

    def create_page(self, data_source_id: str, properties: dict, children: list[dict]) -> dict:
        """Create a page in a data source. Notion takes at most 100 children here; use append_children."""
        body = {"parent": {"type": "data_source_id", "data_source_id": data_source_id},
                "properties": properties, "children": children}
        return self._call("POST", "/pages", body)

    def update_page(self, page_id: str, body: dict) -> dict:
        return self._call("PATCH", f"/pages/{page_id}", body)

    def append_children(self, block_id: str, children: list[dict]) -> dict:
        return self._call("PATCH", f"/blocks/{block_id}/children", {"children": children})
