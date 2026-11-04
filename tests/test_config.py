import pytest

from t6_notion_reports import cli
from t6_notion_reports.config import ConfigError, load_config

ID_A = "11111111-1111-1111-1111-111111111111"
ID_B = "22222222222222222222222222222222"
ENV = {"NOTION_TOKEN": "ntn_supersecret"}


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return str(p)


def good(**over):
    cfg = {"projects_data_source": ID_A, "contacts_data_source": ID_B}
    cfg.update(over)
    return "\n".join(f'{k}: "{v}"' for k, v in cfg.items() if v is not None)


def test_loads_valid_config_with_default_db(tmp_path):
    cfg = load_config(write(tmp_path, good()), ENV)
    assert (cfg.projects_data_source, cfg.database_path, cfg.notion_token) == (ID_A, "data/psr.db", "ntn_supersecret")


@pytest.mark.parametrize("over,key", [
    ({"projects_data_source": None}, "projects_data_source"),
    ({"contacts_data_source": "not-an-id-hunter2"}, "contacts_data_source"),
    ({"database_path": " "}, "database_path"),
    ({"surprise": "hunter2"}, "surprise"),
])
def test_bad_config_names_key_never_value(tmp_path, over, key):
    with pytest.raises(ConfigError) as err:
        load_config(write(tmp_path, good(**over)), ENV)
    assert key in str(err.value) and "hunter2" not in str(err.value)


def test_missing_secret_names_variable(tmp_path):
    with pytest.raises(ConfigError, match="NOTION_TOKEN"):
        load_config(write(tmp_path, good()), {})


def test_bad_yaml_and_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not valid YAML") as err:
        load_config(write(tmp_path, "a: [hunter2"), ENV)
    assert "hunter2" not in str(err.value)
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "nope.yaml"), ENV)


def test_serve_fails_boot_with_key_name_on_stderr(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "ntn_supersecret")
    path = write(tmp_path, good(projects_data_source="hunter2"))
    assert cli.main(["serve", "--config", path]) == 2
    err = capsys.readouterr().err
    assert "projects_data_source" in err and "hunter2" not in err


BRIEF_YAML = """
projects_data_source: "11111111-1111-1111-1111-111111111111"
contacts_data_source: "22222222222222222222222222222222"
gateway_url: "http://192.168.88.151:8780"
psr_group_chat_id: -1003261335368
psr_group_topic_id: 1
cyril_contact_id: "33333333-3333-3333-3333-333333333333"
"""


def test_no_briefing_keys_means_no_brief_settings(tmp_path):
    assert load_config(write(tmp_path, good()), ENV).brief is None


def test_loads_briefing_settings_and_secret(tmp_path):
    cfg = load_config(write(tmp_path, BRIEF_YAML + "test_banner: true\n"), {**ENV, "PSR_SECRET": " hunter2 "})
    b = cfg.brief
    assert (b.gateway_url, b.group_chat_id, b.group_topic_id, b.test_banner, b.psr_secret) == (
        "http://192.168.88.151:8780", -1003261335368, 1, True, "hunter2")
    assert load_config(write(tmp_path, BRIEF_YAML), ENV).brief.psr_secret is None


@pytest.mark.parametrize("line,key", [
    ('gateway_url: "http://192.168.88.151:8780"', "gateway_url"),
    ("psr_group_chat_id: -1003261335368", "psr_group_chat_id"),
    ('cyril_contact_id: "33333333-3333-3333-3333-333333333333"', "cyril_contact_id"),
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


def test_matching_defaults_match_the_watchdog(tmp_path):
    m = load_config(write(tmp_path, good()), ENV).matching
    assert (m.fuzzy_min_score, m.fuzzy_min_lead, m.honorifics) == (90, 8, ("ate", "kuya", "sir"))


def test_matching_keys_are_read_from_config(tmp_path):
    text = good() + "\nfuzzy_min_score: 95\nfuzzy_min_lead: 5.5\nhonorifics: [Ate, ' boss ']\n"
    m = load_config(write(tmp_path, text), ENV).matching
    assert (m.fuzzy_min_score, m.fuzzy_min_lead, m.honorifics) == (95, 5.5, ("ate", "boss"))


@pytest.mark.parametrize("line,key", [
    ("fuzzy_min_score: 101", "fuzzy_min_score"),
    ("fuzzy_min_score: -1", "fuzzy_min_score"),
    ("fuzzy_min_score: hunter2", "fuzzy_min_score"),
    ("fuzzy_min_score: true", "fuzzy_min_score"),
    ("fuzzy_min_lead: 101", "fuzzy_min_lead"),
    ("fuzzy_min_lead: hunter2", "fuzzy_min_lead"),
    ("honorifics: hunter2", "honorifics"),
    ("honorifics: []", "honorifics"),
    ("honorifics: [ate, 5]", "honorifics"),
    ("honorifics: [ate, ' ']", "honorifics"),
])
def test_bad_matching_value_fails_boot_naming_key(tmp_path, line, key):
    with pytest.raises(ConfigError) as err:
        load_config(write(tmp_path, good() + "\n" + line + "\n"), ENV)
    assert key in str(err.value) and "hunter2" not in str(err.value)


def test_lead_warning_days_defaults_to_30_and_is_read_from_config(tmp_path):
    assert load_config(write(tmp_path, BRIEF_YAML), ENV).brief.lead_warning_days == 30
    assert load_config(write(tmp_path, BRIEF_YAML + "\nlead_warning_days: 14\n"), ENV).brief.lead_warning_days == 14


@pytest.mark.parametrize("bad", ["hunter2", "0", "-3", "true", "2.5"])
def test_bad_lead_warning_days_fails_boot_naming_key_not_value(tmp_path, bad):
    with pytest.raises(ConfigError) as err:
        load_config(write(tmp_path, BRIEF_YAML + f"\nlead_warning_days: {bad}\n"), ENV)
    assert "lead_warning_days" in str(err.value) and "hunter2" not in str(err.value)
