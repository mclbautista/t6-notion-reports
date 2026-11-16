from types import SimpleNamespace

import pytest

from t6_notion_reports import cli
from t6_notion_reports.config import ConfigError, load_config

ID_A = "11111111-1111-1111-1111-111111111111"
ID_B = "22222222222222222222222222222222"
ENV = {}


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return str(p)


def good(**over):
    cfg = {"database_path": "data/psr.db"}
    cfg.update(over)
    return "\n".join(f'{k}: "{v}"' for k, v in cfg.items() if v is not None)


def test_loads_valid_config_with_default_db(tmp_path):
    cfg = load_config(write(tmp_path, good()), ENV)
    assert (cfg.database_path, cfg.ignored_keys) == ("data/psr.db", ())


def test_old_data_source_keys_are_accepted_ignored_and_reported(tmp_path):
    cfg = load_config(write(tmp_path, good(projects_data_source="not-an-id", contacts_data_source=ID_B, jo_data_source=ID_A)), ENV)
    assert cfg.ignored_keys == ("projects_data_source", "contacts_data_source", "jo_data_source")


@pytest.mark.parametrize("over,key", [
    ({"database_path": " "}, "database_path"),
    ({"surprise": "hunter2"}, "surprise"),
])
def test_bad_config_names_key_never_value(tmp_path, over, key):
    with pytest.raises(ConfigError) as err:
        load_config(write(tmp_path, good(**over)), ENV)
    assert key in str(err.value) and "hunter2" not in str(err.value)


def test_bad_yaml_and_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not valid YAML") as err:
        load_config(write(tmp_path, "a: [hunter2"), ENV)
    assert "hunter2" not in str(err.value)
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "nope.yaml"), ENV)


def test_serve_fails_boot_with_key_name_on_stderr(tmp_path, capsys, monkeypatch):
    path = write(tmp_path, good(database_path=" "))
    assert cli.main(["serve", "--config", path]) == 2
    err = capsys.readouterr().err
    assert "database_path" in err


BRIEF_YAML = """
projects_data_source: "11111111-1111-1111-1111-111111111111"
contacts_data_source: "22222222222222222222222222222222"
gateway_url: "http://192.0.2.10:8780"
psr_group_chat_id: -1000000000001
psr_group_topic_id: 1
cyril_contact_id: "11111111-1111-1111-1111-111111111111"
"""


@pytest.mark.parametrize("suffix, expected", [
    ("", None),
    ("coordinator_recipients: []\n", ()),
    (f'coordinator_recipients: [" {ID_A} ", "{ID_B}"]\n', (ID_A, ID_B)),
])
def test_coordinator_recipients_config(tmp_path, suffix, expected):
    brief = load_config(write(tmp_path, BRIEF_YAML + suffix), ENV).brief
    assert brief is not None
    assert brief.coordinator_recipients == expected
    # This setting must not change identity conventions for existing config fields.
    assert brief.cyril_contact_id == "11111111-1111-1111-1111-111111111111"
    assert brief.digest_recipients == (brief.cyril_contact_id,)


@pytest.mark.parametrize("bad", [
    "null", "hunter2", "true", "false", "123", "{}",
    "[null]", "[true]", "[false]", "[123]", "[{}]", "[[]]",
    "['']", "[' ']", "[hunter2]", "['11111111-1111-1111-1111-11111111111z']",
])
def test_invalid_coordinator_recipients_fail_boot_naming_key(tmp_path, bad):
    path = write(tmp_path, BRIEF_YAML + f"coordinator_recipients: {bad}\n")
    with pytest.raises(ConfigError) as caught:
        load_config(path, ENV)
    error = str(caught.value)
    assert "invalid config key: coordinator_recipients" in error
    assert "hunter2" not in error


def test_no_briefing_keys_means_no_brief_settings(tmp_path):
    assert load_config(write(tmp_path, good()), ENV).brief is None


def test_loads_briefing_settings_and_secret(tmp_path):
    cfg = load_config(write(tmp_path, BRIEF_YAML + "test_banner: true\n"), {**ENV, "PSR_SECRET": " hunter2 "})
    b = cfg.brief
    assert (b.gateway_url, b.group_chat_id, b.group_topic_id, b.test_banner, b.psr_secret) == (
        "http://192.0.2.10:8780", -1000000000001, 1, True, "hunter2")
    assert load_config(write(tmp_path, BRIEF_YAML), ENV).brief.psr_secret is None


@pytest.mark.parametrize("line,key", [
    ('gateway_url: "http://192.0.2.10:8780"', "gateway_url"),
    ("psr_group_chat_id: -1000000000001", "psr_group_chat_id"),
    ('cyril_contact_id: "11111111-1111-1111-1111-111111111111"', "cyril_contact_id"),
])
def test_partial_briefing_config_names_the_missing_key(tmp_path, line, key):
    with pytest.raises(ConfigError, match=key):
        load_config(write(tmp_path, BRIEF_YAML.replace(line, "")), ENV)


@pytest.mark.parametrize("bad,key", [
    ('gateway_url: "ftp://hunter2"', "gateway_url"),
    ('psr_group_chat_id: "hunter2"', "psr_group_chat_id"),
    ('psr_group_topic_id: hunter2', "psr_group_topic_id"),
    ('test_banner: hunter2', "test_banner"),
])
def test_bad_briefing_value_names_key_never_value(tmp_path, bad, key):
    lines = [ln for ln in BRIEF_YAML.splitlines() if not ln.startswith(key)]
    with pytest.raises(ConfigError) as err:
        load_config(write(tmp_path, "\n".join(lines) + "\n" + bad + "\n"), ENV)
    assert key in str(err.value) and "hunter2" not in str(err.value)


def test_old_matching_keys_are_accepted_and_ignored(tmp_path):
    text = good() + "\nfuzzy_min_score: 95\nfuzzy_min_lead: 5.5\nhonorifics: [Ate]\n"
    assert load_config(write(tmp_path, text), ENV).ignored_keys == ("fuzzy_min_score", "fuzzy_min_lead", "honorifics")


def test_boot_logs_one_line_for_each_ignored_key(tmp_path, capsys, monkeypatch):
    from t6_notion_reports.cli import _load
    assert _load(write(tmp_path, good() + "\nhonorifics: [Ate]\nfuzzy_min_score: 95\n")) is not None
    assert capsys.readouterr().err.splitlines() == ["config key ignored: fuzzy_min_score", "config key ignored: honorifics"]


def test_lead_warning_days_defaults_to_30_and_is_read_from_config(tmp_path):
    assert load_config(write(tmp_path, BRIEF_YAML), ENV).brief.lead_warning_days == 30
    assert load_config(write(tmp_path, BRIEF_YAML + "\nlead_warning_days: 14\n"), ENV).brief.lead_warning_days == 14


@pytest.mark.parametrize("bad", ["hunter2", "0", "-3", "true", "2.5"])
def test_bad_lead_warning_days_fails_boot_naming_key_not_value(tmp_path, bad):
    with pytest.raises(ConfigError) as err:
        load_config(write(tmp_path, BRIEF_YAML + f"\nlead_warning_days: {bad}\n"), ENV)
    assert "lead_warning_days" in str(err.value) and "hunter2" not in str(err.value)


def test_concern_chronic_weeks_is_accepted_ignored_and_reported(tmp_path):
    cfg = load_config(write(tmp_path, BRIEF_YAML + "\nconcern_chronic_weeks: 6\n"), ENV)
    assert "concern_chronic_weeks" in cfg.ignored_keys and not hasattr(cfg.brief, "concern_chronic_weeks")


def test_pm_notes_weeks_defaults_to_52_and_is_read_from_config(tmp_path):
    assert load_config(write(tmp_path, BRIEF_YAML), ENV).brief.pm_notes_weeks == 52
    assert load_config(write(tmp_path, BRIEF_YAML + "\npm_notes_weeks: 8\n"), ENV).brief.pm_notes_weeks == 8


def test_leave_clash_days_defaults_to_14_and_is_read_from_config(tmp_path):
    assert load_config(write(tmp_path, BRIEF_YAML), ENV).brief.leave_clash_days == 14
    assert load_config(write(tmp_path, BRIEF_YAML + "\nleave_clash_days: 7\n"), ENV).brief.leave_clash_days == 7


@pytest.mark.parametrize("bad", ["hunter2", "0", "-3", "true", "2.5"])
def test_bad_leave_clash_days_fails_boot_naming_key_not_value(tmp_path, bad):
    with pytest.raises(ConfigError) as err:
        load_config(write(tmp_path, BRIEF_YAML + f"\nleave_clash_days: {bad}\n"), ENV)
    assert "leave_clash_days" in str(err.value) and "hunter2" not in str(err.value)


def test_llm_enabled_defaults_to_true_and_must_be_a_boolean(tmp_path):
    assert load_config(write(tmp_path, good()), ENV).llm_enabled is True
    off = write(tmp_path, good() + "\nllm_enabled: false")
    assert load_config(off, ENV).llm_enabled is False
    with pytest.raises(ConfigError, match="llm_enabled"):
        load_config(write(tmp_path, good(llm_enabled="hunter2")), ENV)


def test_shift_llm_is_off_when_llm_calls_are_off_or_the_api_is_not_set(tmp_path, monkeypatch):
    shift = good() + "\nllm_enabled: %s"
    for flag, url, expected in (("true", "http://api.invalid", True), ("false", "http://api.invalid", False), ("true", "", False)):
        monkeypatch.setenv("T6_API_URL", url)
        cfg = load_config(write(tmp_path, shift % flag), ENV)
        cfg = type(cfg)(**{**cfg.__dict__, "shift": SimpleNamespace(llm_model="m")})
        assert (cli._shift_llm(cfg) is not None) is expected
