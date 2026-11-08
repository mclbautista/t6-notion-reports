"""Plain input/output contracts for the payload-local privacy boundary (#67)."""
import pytest

from t6_notion_reports.redact import Mapping


@pytest.mark.parametrize("token", ["[person 1]", "[Person 1]", "[PERSON 1]", "[person  1]", "person 1", "[person 1 ]", "[ person1 ]"])
def test_issued_placeholder_variants_restore_the_original(token):
    mapping = Mapping()
    mapping.placeholder("person", "Dario")
    assert mapping.restore(f"Waiting for {token}") == "Waiting for Dario"


def test_shared_placeholder_restores_first_written_casing():
    mapping = Mapping()
    assert mapping.placeholder("person", "Dario") == mapping.placeholder("person", "DARIO")
    assert mapping.restore("[person 1]") == "Dario"


def test_unknown_id_is_not_silently_accepted():
    mapping = Mapping()
    mapping.placeholder("id", "123456789")
    assert mapping.restore("[id 1] and [id 2]") == "[id] and [id 2]"


def test_consolidation_drops_unresolved_items_and_fails_when_none_survive():
    from datetime import date
    from t6_notion_reports.consolidate import Change, Consolidation, Payload, SourceLine, validate

    text = "Pending [person 8]"
    payload = Payload({"today": "2026-11-08", "project": {"project_health": None}, "blank_date_properties": []},
                      {1: SourceLine("pm_notes", text)}, text)
    reply = Consolidation(changes=[Change(text=text, source="pm_notes", line_id=1, quote=text)])
    outcome = validate(reply, payload, date(2026, 11, 8))
    assert outcome.changes == ()
    assert outcome.failed


def test_condense_redacts_payload_and_restores_output():
    import json
    from datetime import date
    from t6_notion_reports.condense import condense_bullets
    from t6_notion_reports.contacts import Contact
    from t6_notion_reports.redact import Redactor

    redactor = Redactor.build([Contact("c", "Dario")], [{"other": {"Name": "Other Show"}}])
    class Llm:
        def complete(self, system, user, schema):
            assert "Dario" not in user and "Other Show" not in user and "123456789" not in user
            return schema(bullets=[json.loads(user)["new_blocks"][0].replace("person", "PERSON")])
    source = ["Dario confirmed Other Show 123456789"]
    result = condense_bullets(Llm(), "Own", source[0], source, date(2026, 11, 8), redactor=redactor)
    assert result.bullets == ["Dario confirmed Other Show [id]"]
    assert not result.raw


def test_leaves_redacts_payload_and_restores_before_contact_matching():
    import json
    from datetime import date
    from t6_notion_reports.contacts import Contact
    from t6_notion_reports.leaves import parse_leaves
    from t6_notion_reports.redact import Redactor

    redactor = Redactor.build([Contact("c", "Dario")], [{"other": {"Name": "Other Show"}}])
    class Llm:
        def complete(self, system, user, schema):
            assert "Dario" not in user and "Other Show" not in user and "123456789" not in user
            line = json.loads(user)["lines"][0]
            return schema(entries=[{"who": "[PERSON 1 ]", "quote": line, "kind": "leave",
                                    "start": "2026-11-09", "end": "2026-11-09"}])
    result = parse_leaves(Llm(), ["Dario leave Nov 9 (Other Show 123456789)"], date(2026, 11, 8), redactor=redactor)
    assert not result.failed
    assert result.entries[0].who == "Dario"


@pytest.mark.parametrize("variant", ["[Person 1]", "[PERSON 1]", "[person  1]", "person 1", "[person 1 ]"])
def test_consolidation_restores_variants_in_changes_concerns_and_quotes(variant):
    from datetime import date
    from t6_notion_reports.consolidate import Change, Concern, Consolidation, Payload, SourceLine, validate

    mapping = Mapping()
    mapping.placeholder("person", "Dario")
    line = "Pending [person 1]"
    payload = Payload({"today": "2026-11-08", "project": {"project_health": None}, "blank_date_properties": []},
                      {1: SourceLine("pm_notes", line)}, line, mapping)
    cite = {"text": f"Pending {variant}", "quote": f"Pending {variant}", "source": "pm_notes", "line_id": 1}
    result = validate(Consolidation(changes=[Change(**cite)], concerns=[Concern(kind="open_item", **cite)]),
                      payload, date(2026, 11, 8))
    assert result.changes[0].text == "Pending Dario"
    assert result.concerns == ("Pending Dario",)
    assert not result.failed


def test_consolidation_keeps_safe_siblings_when_an_item_cannot_restore():
    from datetime import date
    from t6_notion_reports.consolidate import Change, Consolidation, Payload, SourceLine, validate

    payload = Payload({"today": "2026-11-08", "project": {"project_health": None}, "blank_date_properties": []},
                      {1: SourceLine("pm_notes", "Pending [person 8]"), 2: SourceLine("pm_notes", "QC pending")},
                      "Pending [person 8]\nQC pending")
    cite = {"source": "pm_notes", "line_id": 1, "quote": "Pending [person 8]"}
    # Even a harmless text is rejected when another field on its item cannot restore.
    safe = Change(text="QC pending", quote="QC pending", source="pm_notes", line_id=2)
    result = validate(Consolidation(changes=[Change(text="Pending", **cite), safe]), payload, date(2026, 11, 8))
    assert not result.failed and [c.text for c in result.changes] == ["QC pending"]


def test_leaves_unresolved_item_fails_closed_and_safe_sibling_survives():
    from datetime import date
    from t6_notion_reports.leaves import parse_leaves

    class Llm:
        def __init__(self, safe):
            self.safe = safe
        def complete(self, system, user, schema):
            bad = {"who": "[person 9]", "quote": "leave Nov 9", "kind": "leave",
                   "start": "2026-11-09", "end": "2026-11-09"}
            return schema(entries=[bad, *([{**bad, "who": "ALL", "kind": "holiday"}] if self.safe else [])])
    assert parse_leaves(Llm(False), ["leave Nov 9"], date(2026, 11, 8)).failed
    result = parse_leaves(Llm(True), ["leave Nov 9"], date(2026, 11, 8))
    assert not result.failed and result.entries[0].who == "ALL"
