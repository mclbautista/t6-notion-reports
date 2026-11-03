"""Boot configuration: non-secret values from config.yaml, secrets from the environment.

A bad value fails boot with a message that names the key and never the value.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping

import yaml

SECRET_ENV = "NOTION_TOKEN"
KEYS = {"projects_data_source", "contacts_data_source", "database_path"}
_NOTION_ID = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")


class ConfigError(Exception):
    """Raised with a message naming the offending key, never its value."""


@dataclass(frozen=True)
class Config:
    projects_data_source: str
    contacts_data_source: str
    database_path: str
    notion_token: str


def load_config(path: str, env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    try:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except yaml.YAMLError:
        # PyYAML errors quote the offending line, which may hold a value.
        raise ConfigError(f"config file is not valid YAML: {path}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"config file must be a mapping of keys: {path}")

    for key in sorted(set(raw) - KEYS, key=str):
        raise ConfigError(f"unknown config key: {key}")

    return Config(
        projects_data_source=_notion_id(raw, "projects_data_source"),
        contacts_data_source=_notion_id(raw, "contacts_data_source"),
        database_path=_string(raw, "database_path", default="data/psr.db"),
        notion_token=_secret(env, SECRET_ENV),
    )


def _string(raw: dict, key: str, default: str | None = None) -> str:
    if key not in raw:
        if default is None:
            raise ConfigError(f"missing config key: {key}")
        return default
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"invalid config key: {key} (expected a non-empty string)")
    return value.strip()


def _notion_id(raw: dict, key: str) -> str:
    value = _string(raw, key)
    if not _NOTION_ID.match(value):
        raise ConfigError(f"invalid config key: {key} (expected a Notion data source id)")
    return value


def _secret(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"missing environment variable: {name}")
    return value
