"""Notion through t6-core's client (ADR 0002): the app's calls keep their shape, the HTTP is core's.

Core's client retries 429 and 5xx, honours Retry-After and raises one ``NotionError``.
"""
from __future__ import annotations

from t6.notion import NotionClient as CoreNotionClient
from t6.notion.errors import NotionError


class NotionClient:
    def __init__(self, token: str, client: CoreNotionClient | None = None):
        self._client = client or CoreNotionClient(token)

    def retrieve_data_source(self, data_source_id: str) -> dict:
        return self._client.retrieve_data_source(data_source_id)

    def create_page(self, data_source_id: str, properties: dict, children: list[dict]) -> dict:
        """Create a page in a data source. Notion takes at most 100 children here; use append_children."""
        return self._client.create_page({
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": properties, "children": children,
        })

    def update_page(self, page_id: str, body: dict) -> dict:
        return self._client.update_page(page_id, body)

    def append_children(self, block_id: str, children: list[dict]) -> list[dict]:
        return self._client.append_block_children(block_id, children)

    def list_block_children(self, block_id: str) -> list[dict]:
        """All direct children of a page or block, oldest first."""
        return self._client.list_block_children(block_id)

    def query_data_source(self, data_source_id: str, filter: dict | None = None) -> list[dict]:
        return self._client.query_data_source(data_source_id, filter=filter)


def is_outage(error: BaseException) -> bool:
    """A failure to reach Notion (no answer, rate limit or a 5xx), not a refusal of the request."""
    if isinstance(error, NotionError):
        return error.status is None or error.status == 429 or error.status >= 500
    return isinstance(error, OSError)


def error_name(error: BaseException) -> str:
    """The failure's type, or for Notion its error code; never the message, which may hold data."""
    return error.code if isinstance(error, NotionError) else type(error).__name__
