"""Config loading for ``shift_destinations`` (#84): per-report lists of DMs and chats, placeholder ids only."""
import pytest
import yaml

from t6_notion_reports.config import ConfigError, Destination, load_config

PROJECTS_ID = "1" * 8 + "-1111-1111-1111-" + "1" * 12
CONTACTS_ID = "2" * 8 + "-2222-2222-2222-" + "2" * 12
JO_ID = "3" * 8 + "-3333-3333-3333-" + "3" * 12
BASE = {"projects_data_source": PROJECTS_ID, "contacts_data_source": CONTACTS_ID, "jo_data_source": JO_ID,
        "shift_reports_enabled": True, "gateway_url": "http://gw:8780"}
ENV = {"NOTION_TOKEN": "tok", "SHIFT_SECRET": "s"}
USER_A, USER_B = 100000001, 100000002
GROUP, TOPIC = -1000000000001, 5


def load(tmp_path, **cfg):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({**BASE, **cfg}))
    return load_config(str(path), ENV)


def fails(tmp_path, **cfg):
    with pytest.raises(ConfigError) as err:
        load(tmp_path, **cfg)
    return str(err.value)


def test_valid_mixed_lists_load_per_slot_in_config_order(tmp_path):
    s = load(tmp_path, shift_destinations={
        "mid": [{"chat": GROUP, "topic": TOPIC}],
        "day": [USER_A, USER_B, {"chat": GROUP}],
        "night": [USER_A],
    }).shift
    assert s.destinations["1200"] == (Destination(GROUP, TOPIC),)
    assert s.destinations["1800"] == (Destination(USER_A, None, dm=True), Destination(USER_B, None, dm=True),
                                      Destination(GROUP, None))
    assert s.destinations["0500"] == (Destination(USER_A, None, dm=True),)


def test_names_are_optional_and_empty_or_missing_lists_are_allowed(tmp_path):
    s = load(tmp_path, shift_destinations={"day": [USER_A], "night": []}).shift
    assert s.destinations == {"1200": (), "1800": (Destination(USER_A, None, dm=True),), "0500": ()}


@pytest.mark.parametrize("bad", [
    {"weekly": [USER_A]},
    {"day": USER_A},
    {"day": ["abc"]},
    {"day": [True]},
    {"day": [1.5]},
    {"day": [{"chat": "abc"}]},
    {"day": [{"chat": GROUP, "topic": "x"}]},
    {"day": [{"topic": TOPIC}]},
    {"day": [{"chat": GROUP, "extra": 1}]},
    {"day": [USER_A, USER_A]},
    {"day": [{"chat": GROUP, "topic": TOPIC}, {"chat": GROUP, "topic": TOPIC}]},
    [USER_A],
])
def test_invalid_shapes_fail_naming_the_key_and_never_an_id(tmp_path, bad):
    msg = fails(tmp_path, shift_destinations=bad)
    assert "shift_destinations" in msg
    for secret in (str(USER_A), str(GROUP), str(TOPIC), "abc"):
        assert secret not in msg


def test_error_names_the_report_and_entry_number(tmp_path):
    msg = fails(tmp_path, shift_destinations={"day": [USER_A, "abc"]})
    assert "day" in msg and "entry 2" in msg
    msg = fails(tmp_path, shift_destinations={"night": [USER_A, USER_B, USER_A]})
    assert "night" in msg and "entry 3" in msg


def test_same_chat_with_different_topics_or_a_dm_is_not_a_duplicate(tmp_path):
    s = load(tmp_path, shift_destinations={"day": [GROUP, {"chat": GROUP, "topic": 1}, {"chat": GROUP, "topic": 2}]}).shift
    assert len(s.destinations["1800"]) == 3


@pytest.mark.parametrize("old", [{"shift_chat_id": GROUP}, {"shift_topic_id": TOPIC}])
def test_old_keys_fail_with_the_replacement_message(tmp_path, old):
    msg = fails(tmp_path, shift_destinations={"day": [USER_A]}, **old)
    assert "shift_destinations" in msg and "replaced" in msg and str(GROUP) not in msg


def test_enabled_with_no_destination_in_total_fails(tmp_path):
    assert "shift_destinations" in fails(tmp_path)
    assert "shift_destinations" in fails(tmp_path, shift_destinations={})
    assert "shift_destinations" in fails(tmp_path, shift_destinations={"day": [], "mid": []})


def test_disabled_reads_nothing_from_the_shift_keys(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({k: v for k, v in BASE.items() if k not in ("gateway_url", "shift_reports_enabled")}
                                   | {"shift_reports_enabled": False, "shift_destinations": "junk",
                                      "shift_chat_id": "junk", "shift_topic_id": "junk"}))
    assert load_config(str(path), {"NOTION_TOKEN": "tok"}).shift is None
