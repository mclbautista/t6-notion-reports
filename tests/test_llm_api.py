"""``ApiLlm``: the one LLM adapter over ``llm run`` (#111)."""
import pytest
from pydantic import BaseModel

from fake_llm_api import FakeLlmApi, api_error, invalid, ok, refused, unreachable
from t6_notion_reports.llm_api import ApiLlm, LlmError, Reply, UnavailableLlm


class Out(BaseModel):
    items: list[str]


def make(*script, **kw):
    api, waits, log = FakeLlmApi(*script), [], []
    return api, waits, ApiLlm(api, "task", "model/x", sleep=waits.append, log=log.append, **kw), log


def test_the_request_and_the_validated_reply():
    api, _, llm, _ = make(ok({"items": ["a", "b"]}))
    reply = llm.complete("Do it.", "the text", Out, allow_projects=["Alpha"])
    assert reply == Reply(Out(items=["a", "b"]), 0)
    sent = api.sent[0]
    assert sent.prompt_template == "Do it.\n\n{input}" and sent.input_text == "the text"
    assert sent.output_schema == Out.model_json_schema() and sent.allow_projects == ["Alpha"]


class Nested(BaseModel):
    last: str = ""
    groups: list[dict] = []


def test_an_item_holding_an_unresolved_placeholder_is_dropped_alone_and_counted():
    out = {"last": "x", "groups": [{"t": "ok"}, {"t": "[person 9]", "u": "z"}, {"t": "fine"}, {"t": "w", "c": [{"q": "[id 3]"}]}]}
    _, _, llm, log = make(ok(out, unresolved=["groups[1].t", "groups[3].c[0].q"]))
    reply = llm.complete("s", "u", Nested)
    assert reply.value.groups == [{"t": "ok"}, {"t": "fine"}] and reply.dropped == 2
    assert log == ["llm task=task unresolved=2"]


def test_a_string_item_of_a_list_is_dropped():
    _, _, llm, _ = make(ok({"items": ["a", "[person 9]", "b"]}, unresolved=["items[1]"]))
    assert llm.complete("s", "u", Out).value.items == ["a", "b"]


def test_an_unresolved_field_outside_a_list_fails_the_answer():
    _, _, llm, _ = make(ok({"last": "[person 9]"}, unresolved=["last"]))
    with pytest.raises(LlmError, match="unresolved"):
        llm.complete("s", "u", Nested)


def test_constructor_options_pass_through():
    api, _, llm, _ = make(ok({"items": []}), masking_preset="names", allow_projects=["Beta"], repair_quotes=False, restore="whole")
    llm.complete("s", "u", Out, allow_projects=["Alpha"])
    sent = api.sent[0]
    assert (sent.masking_preset, sent.allow_projects, sent.repair_quotes, sent.restore) == ("names", ["Beta", "Alpha"], False, "whole")


@pytest.mark.parametrize("answer,words", [
    (refused("masking"), "rule=masking failure=none"),
    (refused("policy", "policy"), "rule=policy failure=policy"),
    (invalid("invalid_json"), "failure=invalid_json"),
    (api_error(403), "status=403"),
    (ok({"wrong": 1}), "schema"),
])
def test_a_failure_is_not_retried_and_names_only_the_rule_and_the_failure(answer, words):
    api, waits, llm, log = make(answer)
    with pytest.raises(LlmError, match=words):
        llm.complete("s", "secret text", Out)
    assert len(api.sent) == 1 and waits == []
    assert not any("secret" in line for line in log)


def test_a_provider_error_is_retried_then_succeeds():
    api, waits, llm, _ = make(refused("provider", "api_error"), refused("provider", "timeout", retry_after=7), ok({"items": ["x"]}))
    assert llm.complete("s", "u", Out).value.items == ["x"]
    assert len(api.sent) == 3 and waits == [2.0, 7]


def test_retry_after_is_capped():
    _, waits, llm, _ = make(refused("provider", "api_error", retry_after=900), ok({"items": []}))
    llm.complete("s", "u", Out)
    assert waits == [30.0]


def test_a_server_error_is_retried_and_gives_up_after_three_tries():
    api, waits, llm, _ = make(api_error(503))
    with pytest.raises(LlmError, match="status=503"):
        llm.complete("s", "u", Out)
    assert len(api.sent) == 3 and waits == [2.0, 5.0]


def test_an_unreachable_api_is_tried_three_times_then_later_calls_fail_at_once():
    api, waits, llm, _ = make(unreachable())
    for _ in range(2):
        with pytest.raises(LlmError, match="unreachable"):
            llm.complete("s", "u", Out)
    assert len(api.sent) == 3 and waits == [2.0, 5.0]


def test_unavailable_llm_always_fails():
    with pytest.raises(LlmError):
        UnavailableLlm().complete("s", "u", Out)
