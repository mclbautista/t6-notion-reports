"""Boot configuration: non-secret values from config.yaml, secrets from the environment.

A bad value fails boot with a message that names the key and never the value.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping

import yaml

from .llm import DEFAULT_MODEL

SECRET_ENV = "NOTION_TOKEN"
PSR_SECRET_ENV = "PSR_SECRET"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
BRIEF_KEYS = {"gateway_url", "psr_group_chat_id", "psr_group_topic_id", "cyril_contact_id", "test_banner", "llm_model"}
BRIEF_REQUIRED = ("gateway_url", "psr_group_chat_id", "cyril_contact_id")
KEYS = {"projects_data_source", "contacts_data_source", "database_path", "meeting_notes_data_source", "jo_data_source"} | BRIEF_KEYS
_NOTION_ID = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")


class ConfigError(Exception):
    """Raised with a message naming the offending key, never its value."""


@dataclass(frozen=True)
class BriefSettings:
    """Sunday briefing settings. Present only when the briefing keys are in config.yaml."""

    gateway_url: str
    group_chat_id: int
    group_topic_id: int | None
    cyril_contact_id: str
    test_banner: bool
    psr_secret: str | None  # None when PSR_SECRET is unset; only ``brief --dry-print`` may go without
    llm_model: str = DEFAULT_MODEL
    openrouter_api_key: str | None = None  # None when OPENROUTER_API_KEY is unset; only ``--dry-print`` may go without


@dataclass(frozen=True)
class Config:
    projects_data_source: str
    contacts_data_source: str
    database_path: str
    notion_token: str
    brief: BriefSettings | None = None
    jo_data_source: str | None = None  # Job Orders data source; needed by jo-snapshot
    meeting_notes_data_source: str | None = None  # set to draft the minutes (draft-mom, Monday job)


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
        brief=_brief(raw, env),
        jo_data_source=_notion_id(raw, "jo_data_source") if "jo_data_source" in raw else None,
        meeting_notes_data_source=(
            _notion_id(raw, "meeting_notes_data_source") if "meeting_notes_data_source" in raw else None),
    )


def _brief(raw: dict, env: Mapping[str, str]) -> BriefSettings | None:
    if not BRIEF_KEYS & set(raw):
        return None
    for key in BRIEF_REQUIRED:
        if key not in raw:
            raise ConfigError(f"missing config key: {key}")
    url = _string(raw, "gateway_url")
    if not url.startswith(("http://", "https://")):
        raise ConfigError("invalid config key: gateway_url (expected an http(s) URL)")
    banner = raw.get("test_banner", False)
    if not isinstance(banner, bool):
        raise ConfigError("invalid config key: test_banner (expected true or false)")
    topic = _integer(raw, "psr_group_topic_id") if "psr_group_topic_id" in raw else None
    return BriefSettings(
        gateway_url=url,
        group_chat_id=_integer(raw, "psr_group_chat_id"),
        group_topic_id=topic,
        cyril_contact_id=_notion_id(raw, "cyril_contact_id"),
        test_banner=banner,
        psr_secret=env.get(PSR_SECRET_ENV, "").strip() or None,
        llm_model=_string(raw, "llm_model", default=DEFAULT_MODEL),
        openrouter_api_key=env.get(OPENROUTER_KEY_ENV, "").strip() or None,
    )


def _integer(raw: dict, key: str) -> int:
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"invalid config key: {key} (expected an integer)")
    return value


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
