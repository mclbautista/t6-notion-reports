"""Synthetic fixtures for #94's exact per-project outbound privacy boundary."""
import json
from datetime import date

import pytest

from t6_notion_reports.consolidate import build_payload, consolidate
from t6_notion_reports.contacts import Contact
from t6_notion_reports.redact import Redactor

TODAY = date(2026, 11, 14)
UUID = "01234567-89ab-cdef-0123-456789abcdef"
RAW = ("• [Dario Quill] and will wait for [Mira]; ids [111],111. and 111, "
       "@dario_handle tg://user?id=111 notion.so/" + UUID + " " + UUID +
       "; c-known p-other [Other Show] [AB]; Zephyr waits")
PROTECTED = ("• [[person 1]] and [person 2] wait for [[person 3]]; ids [[id 1]],[id 1]. and [id 1], "
             "[id 2] [id 3] [id 4] [id 5]; [id 6] [id 7] [[project 1]] [[project 2]]; Zephyr waits")


def payload():
    own = {"Name": "Target Show", "Project Code": "OWN", "Status": "🚨 In-Progress",
           "PM Notes": "As of Nov 9\n" + RAW}
    redactor = Redactor.build(
        [Contact("c-known", "Dario Quill", "dario_handle", 111, "Will"), Contact("c-mini", "Mira")],
        [{"p-own": own, "p-other": {"Name": "Other Show", "Project Code": "AB"}}],
    )
    return build_payload(own, TODAY, TODAY, [], (RAW,),
                         {"page_date": "2026-11-09", "last_edited": None, "sent_at": None},
                         redactor=redactor, page_id="p-own")


class RecordingLlm:
    def __init__(self, reply=None):
        self.reply = reply or (lambda p: {})
        self.sent = []

    def complete(self, system, user, schema):
        self.sent.append(user)
        return schema(**self.reply(json.loads(user)))


def test_exact_outbound_payload_protects_embedded_known_values_in_notes_and_minutes():
    llm = RecordingLlm()
    consolidate(llm, payload(), TODAY)
    assert json.loads(llm.sent[0]) == {
        "today": "2026-11-14",
        "project": {"name": "Target Show", "stage": None, "status": "🚨 In-Progress", "project_health": None},
        "dates": {"playdate": None, "submission_deadline": None, "archival_deadline": None},
        "blank_date_properties": ["playdate", "submission_deadline"],
        "pm_notes": [
            {"line_id": 1, "block": "As of Nov 9", "text": "As of Nov 9"},
            {"line_id": 2, "block": "As of Nov 9", "text": PROTECTED},
        ],
        "snapshot_changes": [],
        "minutes": {"page_date": "2026-11-09", "last_edited": None, "sent_at": None,
                    "lines": [{"line_id": 3, "text": PROTECTED}]},
    }
    for private in ("Dario", "Quill", "Mira", "will", "111", "@dario_handle", "tg://", UUID,
                    "notion.so", "c-known", "p-other", "Other Show", "AB"):
        assert private not in llm.sent[0]
    # Owner-approved limitation: a name absent from Contacts is not recognized as a name.
    assert "Zephyr" in llm.sent[0]


def test_grounding_uses_exact_protected_quote_and_restores_only_after_validation():
    def reply(data):
        quote = data["pm_notes"][1]["text"]
        return {"changes": [
            {"text": "[person 1] waits", "source": "pm_notes", "line_id": 2, "quote": quote},
            {"text": "[person 3] waits", "source": "minutes", "line_id": 3, "quote": quote},
            {"text": "Zephyr waits", "source": "pm_notes", "line_id": 2, "quote": RAW},
            {"text": "Invented waits", "source": "pm_notes", "line_id": 2, "quote": quote},
        ]}
    llm = RecordingLlm(reply)
    result = consolidate(llm, payload(), TODAY)
    assert not result.failed
    assert tuple(c.text for c in result.changes) == ("Dario Quill waits", "Mira waits")


@pytest.mark.parametrize("name,alias,text", [
    ("Will", "", "will WILL Will"),
    ("Dario Quill", "Will", "will WILL Will"),
    ("Dario Quill", "", "[dario quill] (DARIO QUILL) Dario Quill"),
])
def test_known_full_names_and_aliases_are_hidden_in_all_casing(name, alias, text):
    from t6_notion_reports.redact import Mapping
    redactor = Redactor.build([Contact("c-known", name, aliases=alias)], [])
    assert redactor.redact(text, "p-own", Mapping()) in (
        "[person 1] [person 1] [person 1]", "[[person 1]] ([person 1]) [person 1]")


def test_known_short_project_references_and_page_ids_are_protected():
    from t6_notion_reports.redact import Mapping
    redactor = Redactor.build([], [{"p-other": {"Name": "Up", "Project Code": "X"}}])
    assert redactor.redact("[Up] (X) p-other", "p-own", Mapping()) == "[[project 1]] ([project 2]) [id 1]"


def test_all_textual_project_properties_except_own_identity_are_protected():
    state = {"Name": "Target Show", "Stage": "Review [Dario Quill]", "Status": "Waiting for [111]",
             "Project Health": "Pending [Other Show]"}
    redactor = Redactor.build([Contact("c-known", "Dario Quill", telegram_id=111)],
                              [{"p-own": state, "p-other": {"Name": "Other Show"}}])
    result = build_payload(state, TODAY, TODAY, [], redactor=redactor, page_id="p-own")
    assert result.data["project"] == {"name": "Target Show", "stage": "Review [[person 1]]",
                                      "status": "Waiting for [[id 1]]", "project_health": "Pending [[project 1]]"}


def test_small_known_numeric_ids_do_not_rescan_issued_placeholder_numbers():
    from t6_notion_reports.redact import Mapping
    redactor = Redactor.build([Contact("c-known", "Dario", telegram_id=1)], [])
    assert redactor.redact("Dario [1] Dario 1 10 1.25", "p-own", Mapping()) == (
        "[person 1] [[id 1]] [person 1] [id 1] 10 1.25")


@pytest.mark.parametrize("reverse", [False, True])
def test_partial_name_ambiguity_never_weakens_a_known_full_name_or_alias(reverse):
    from t6_notion_reports.redact import Mapping
    contacts = [Contact("c-full", "Will"), Contact("c-alias", "Mira", aliases="Mark"),
                Contact("c-partial", "Will Mark Quill")]
    redactor = Redactor.build(reversed(contacts) if reverse else contacts, [])
    assert redactor.redact("will mark", "p-own", Mapping()) == "[person 1] [person 2]"
