"""Minimal official-Notion-API client (data source query only)."""
from __future__ import annotations

import json
import urllib.request
from typing import Callable

API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"

Transport = Callable[[str, dict, dict], dict]


def _urllib_transport(url: str, headers: dict, body: dict) -> dict:
    req = urllib.request.Request(url, json.dumps(body).encode(), headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


class NotionClient:
    def __init__(self, token: str, transport: Transport = _urllib_transport):
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self._transport = transport

    def query_data_source(self, data_source_id: str) -> list[dict]:
        url = f"{API}/data_sources/{data_source_id}/query"
        pages: list[dict] = []
        body: dict = {}
        while True:
            data = self._transport(url, self._headers, dict(body))
            pages.extend(data["results"])
            if not data.get("has_more"):
                return pages
            body["start_cursor"] = data["next_cursor"]
