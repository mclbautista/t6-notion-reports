"""OpenRouterClient: a 200 whose JSON has unescaped quotes is repaired, real failures still raise."""
import json

import pytest
from pydantic import BaseModel

from t6_notion_reports.llm import LlmError, OpenRouterClient


class Item(BaseModel):
    quote: str
    n: int = 0


class Reply(BaseModel):
    items: list[Item]
    note: str | None = None


def client(content):
    def transport(url, headers, body):
        return 200, {"choices": [{"message": {"content": content}}]}

    return OpenRouterClient("key", transport=transport)


def ask(content):
    return client(content).complete("sys", "user", Reply)


def test_valid_json_unchanged():
    raw = json.dumps({"items": [{"quote": 'He said "go"', "n": 1}]})
    assert ask(raw).items[0].quote == 'He said "go"'


def test_unescaped_quotes_inside_quote_field_are_repaired():
    raw = '{"items": [{"quote": "DW said "ship it Friday" in the notes", "n": 2}], "note": "ok"}'
    with pytest.raises(ValueError):
        json.loads(raw)
    reply = ask(raw)
    assert reply.items[0].quote == 'DW said "ship it Friday" in the notes'
    assert reply.items[0].n == 2 and reply.note == "ok"


def test_quote_at_start_and_end_of_value_and_fenced():
    raw = '```json\n{"items": [{"quote": ""Hold" the "date""}]}\n```'
    assert ask(raw).items[0].quote == '"Hold" the "date"'


def test_raw_newline_in_string_and_existing_escapes_kept():
    raw = '{"items": [{"quote": "a \\"b\\" "c"\nd"}]}'
    assert ask(raw).items[0].quote == 'a "b" "c"\nd'


def test_unrepairable_output_still_raises():
    for raw in ["not json at all", '{"items": [{"quote": "unterminated', '{"items": "wrong shape"}', "[1]"]:
        with pytest.raises(LlmError):
            ask(raw)


def test_schema_mismatch_not_masked():
    with pytest.raises(LlmError):
        ask('{"items": [{"n": 1}]}')
