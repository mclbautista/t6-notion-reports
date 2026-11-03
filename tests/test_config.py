import pytest

from t6_psr_brief import cli
from t6_psr_brief.config import ConfigError, load_config

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
