"""Input/output contracts for the privacy boundary of one payload (#67): names go out masked and come back restored."""
import json
from fake_llm import AskLlm
from datetime import date

import pytest
from t6.llm.testing import FakeProvider
from t6.privacy import Person, Registry, Session

from fake_llm import AskLlm
from t6_notion_reports.consolidate import Consolidation, build_payload, consolidate
from t6_notion_reports.contacts import Contact
from t6_notion_reports.llm import LlmError, T6Llm
from t6_notion_reports.masking import Masker

TODAY = date(2026, 11, 8)
STATE = {"Name": "Own", "PM Notes": "As of Nov 8\n• Pending Dario"}


def llm_replying(*replies):
    provider = FakeProvider()
    for reply in replies:
        provider.reply(reply if isinstance(reply, str) else json.dumps(reply))
    return T6Llm(provider), provider


def payload():
    masker = Masker.build([Contact("c", "Dario")], [{"p-own": STATE}])
    return build_payload(STATE, TODAY, TODAY, [], masker=masker, page_id="p-own")


def change(text="Pending [person 1]", quote="• Pending [person 1]", line_id=2):
    return {"text": text, "source": "pm_notes", "line_id": line_id, "quote": quote}


@pytest.mark.parametrize("token", ["[person 1]", "[Person 1]", "[PERSON 1]", "[person  1]", "person 1", "[person 1 ]", "[ person1 ]"])
def test_issued_placeholder_variants_restore_the_original(token):
    session = Session(Registry((Person("Dario"),)), options="strict")
    session.mask("Dario")
    assert session.restore(f"Waiting for {token}") == "Waiting for Dario"


def test_shared_placeholder_restores_first_written_casing():
    session = Session(Registry((Person("Dario"),)), options="strict")
    assert session.mask("Dario DARIO").text == "[person 1] [person 1]"
    assert session.restore("[person 1]") == "Dario"


@pytest.mark.parametrize("variant", ["[Person 1]", "[PERSON 1]", "[person  1]", "person 1", "[person 1 ]"])
def test_consolidation_restores_variants_in_changes_concerns_and_quotes(variant):
    cite = change(f"Pending {variant}", f"• Pending {variant}")
    concern = {"kind": "open_item", **cite}
    llm, _ = llm_replying({"changes": [cite], "concerns": [concern]})
    result = consolidate(llm, payload(), TODAY)
    assert [c.text for c in result.changes] == ["Pending Dario"]
    assert result.concerns == ("Pending Dario",)
    assert not result.failed


def test_a_placeholder_the_prompt_never_issued_fails_the_whole_call():
    llm, _ = llm_replying({"changes": [change("Pending [person 8]", "• Pending [person 8]")]})
    result = consolidate(llm, payload(), TODAY)
    assert result.failed and result.changes == ()


def test_a_name_that_leaks_in_the_reply_fails_the_whole_call():
    llm, _ = llm_replying({"changes": [change("Dario is pending", "• Pending Dario")]})
    assert consolidate(llm, payload(), TODAY).failed


def test_a_quote_of_a_line_with_an_id_still_matches_the_line():
    state = {"Name": "Own", "PM Notes": "As of Nov 8\n• Sent 123456789 to the lab"}
    masker = Masker.build([], [{"p-own": state}])
    data = build_payload(state, TODAY, TODAY, [], masker=masker, page_id="p-own")
    sent = data.data["pm_notes"][1]["text"]
    assert "123456789" not in sent and "[id 1]" in sent
    llm, _ = llm_replying({"changes": [{"text": "Sent to the lab", "source": "pm_notes", "line_id": 2, "quote": sent}]})
    result = consolidate(llm, data, TODAY)
    assert [c.text for c in result.changes] == ["Sent to the lab"]


def test_condense_masks_payload_and_restores_output():
    from t6_notion_reports.condense import condense_bullets

    masker = Masker.build([Contact("c", "Dario")], [{"other": {"Name": "Other Show"}}])

    class Llm(AskLlm):
        def ask(self, system, user, schema):
            assert "Dario" not in user and "Other Show" not in user and "123456789" not in user
            return schema(bullets=[json.loads(user)["new_blocks"][0].replace("person", "PERSON")])

    source = ["Dario confirmed Other Show 123456789"]
    result = condense_bullets(Llm(), "Own", source[0], source, TODAY, masker=masker)
    assert result.bullets == ["Dario confirmed Other Show [id]"]
    assert not result.raw


def test_leaves_masks_payload_and_restores_before_contact_matching():
    from t6_notion_reports.leaves import parse_leaves

    masker = Masker.build([Contact("c", "Dario")], [{"other": {"Name": "Other Show"}}])

    class Llm(AskLlm):
        def ask(self, system, user, schema):
            assert "Dario" not in user and "Other Show" not in user and "123456789" not in user
            line = json.loads(user)["lines"][0]
            return schema(entries=[{"who": "[PERSON 1 ]", "quote": line, "kind": "leave",
                                    "start": "2026-11-09", "end": "2026-11-09"}])

    result = parse_leaves(Llm(), ["Dario leave Nov 9 (Other Show 123456789)"], TODAY, masker=masker)
    assert not result.failed
    assert result.entries[0].who == "Dario"


def test_leaves_an_unresolved_placeholder_fails_closed():
    from t6_notion_reports.leaves import parse_leaves

    bad = {"who": "[person 9]", "quote": "leave Nov 9", "kind": "leave", "start": "2026-11-09", "end": "2026-11-09"}
    llm, _ = llm_replying({"entries": [bad, {**bad, "who": "ALL", "kind": "holiday"}]})
    assert parse_leaves(llm, ["leave Nov 9"], TODAY).failed


def test_a_failed_call_raises_only_the_reason():
    llm, provider = llm_replying("not json")
    with pytest.raises(LlmError, match="invalid_json"):
        llm.complete("t-v1", "sys", Session(Registry()).mask("x"), Consolidation, Session(Registry()))
    assert provider.calls[0].zdr
