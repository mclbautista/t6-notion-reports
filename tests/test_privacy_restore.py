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
