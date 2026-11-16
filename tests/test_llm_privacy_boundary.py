"""The brief's consolidation over a fake ``llm run`` API: what is sent, and what a refusal or a bad answer does (#127).

The API masks and restores the names; these tests use invented names and check only what the app sends and keeps.
"""
import json
from datetime import date

import pytest

from fake_llm_api import FakeLlmApi, invalid, ok, refused, unreachable
from t6_notion_reports.consolidate import build_payload, consolidate
from t6_notion_reports.llm_api import ApiLlm

TODAY = date(2026, 11, 14)
LINE = "• Mira waits for the grade, see tg://user?id=111"


def payload():
    own = {"Name": "Target Show", "Project Code": "OWN", "Status": "🚨 In-Progress", "PM Notes": "As of Nov 9\n" + LINE}
    return build_payload(own, TODAY, TODAY, [], (LINE,), {"page_date": "2026-11-09", "last_edited": None, "sent_at": None})


def adapter(api):
    return ApiLlm(api, "brief_consolidation", "m", sleep=lambda _: None)


def change(text, quote=LINE, line_id=2, **kw):
    return {"text": text, "source": "pm_notes", "line_id": line_id, "quote": quote, **kw}


def test_the_request_holds_the_real_payload_the_own_project_and_the_zero_retention_options():
    api = FakeLlmApi(ok({}))
    consolidate(adapter(api), payload(), TODAY)
    sent = api.sent[0]
    data = json.loads(sent.input_text)
    assert data["project"]["name"] == "Target Show"
    assert [r["text"] for r in data["pm_notes"]] == ["As of Nov 9", LINE]  # real text: the API masks it
    assert sent.allow_projects == ["Target Show", "OWN"]
    assert (sent.task_name, sent.masking_preset, sent.restore, sent.repair_quotes) == ("brief_consolidation", "strict", "per_field", True)
    assert sent.require_zdr and sent.temperature_zero and sent.model_hint == "m"


def test_an_accepted_item_is_the_restored_text_checked_against_the_real_line():
    outcome = consolidate(adapter(FakeLlmApi(ok({"changes": [change("Mira waits for the grade")]}))), payload(), TODAY)
    assert [c.text for c in outcome.changes] == ["Mira waits for the grade"] and not outcome.failed


def test_a_hidden_id_comes_back_as_an_id_mark_and_still_matches_its_line():
    quote = "• Mira waits for the grade, see [id]"
    outcome = consolidate(adapter(FakeLlmApi(ok({"changes": [change("Waits, see [id]", quote)]}))), payload(), TODAY)
    assert [c.text for c in outcome.changes] == ["Waits, see [id]"]


@pytest.mark.parametrize("item", [
    change("Mira waits", quote="• Mira waits for the grade"),            # a quote that is not the exact line
    change("Mira waits for the grade", line_id=9),                       # a line that does not exist
    change("Mira waits for zyxmarco"),                                   # a word the line does not have
    change("Mira waits for 12 days"),                                    # a number the line does not have
    change("Mira waits", source="minutes"),                              # a source the line is not from
    change("Mira waits", source="elsewhere"),
])
def test_each_rejected_item_kind_is_dropped_and_the_project_is_not_failed(item):
    outcome = consolidate(adapter(FakeLlmApi(ok({"changes": [item]}))), payload(), TODAY)
    assert outcome.changes == () and not outcome.failed


@pytest.mark.parametrize("answer", [
    refused("masking"), refused("masking", "leak"), refused("allow"), invalid(), invalid("invalid_json"),
    refused("provider", "timeout"), refused("provider", "api_error"), unreachable(),
], ids=lambda a: type(a).__name__ + str(getattr(a, "rule", getattr(a, "failure", ""))))
def test_a_refusal_an_invalid_answer_a_timeout_or_an_unreachable_api_fails_the_project(answer):
    assert consolidate(adapter(FakeLlmApi(answer)), payload(), TODAY).failed


def test_an_item_the_api_could_not_restore_is_dropped_alone():
    items = [change("[person 9] waits"), change("Mira waits for the grade")]
    outcome = consolidate(adapter(FakeLlmApi(ok({"changes": items}, unresolved=["changes[0].text"]))), payload(), TODAY)
    assert [c.text for c in outcome.changes] == ["Mira waits for the grade"] and not outcome.failed


def test_when_every_item_is_dropped_for_a_placeholder_the_project_failed():
    outcome = consolidate(adapter(FakeLlmApi(ok({"changes": [change("[person 9] waits")]}, unresolved=["changes[0].text"]))), payload(), TODAY)
    assert outcome.changes == () and outcome.failed


def test_a_provider_error_is_retried_before_the_project_fails():
    api = FakeLlmApi(refused("provider", "api_error"), ok({"changes": [change("Mira waits for the grade")]}))
    assert not consolidate(adapter(api), payload(), TODAY).failed and len(api.sent) == 2
