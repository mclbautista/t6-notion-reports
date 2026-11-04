"""Boot configuration: non-secret values from config.yaml, secrets from the environment.

A bad value fails boot with a message that names the key and never the value.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping

import yaml

from .concerns import DEFAULT_CHRONIC_WEEKS
from .consolidate import DEFAULT_PM_NOTES_WEEKS
from .flags import DEFAULT_LEAD_WARNING_DAYS
from .leaves import DEFAULT_CLASH_DAYS
from .llm import DEFAULT_MODEL
from .people_match import MatchSettings

SECRET_ENV = "NOTION_TOKEN"
PSR_SECRET_ENV = "PSR_SECRET"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
SHIFT_SECRET_ENV = "SHIFT_SECRET"
SHIFT_KEYS = {"shift_reports_enabled", "shift_chat_id", "shift_topic_id", "ops_chat_id"}
BRIEF_KEYS = {"gateway_url", "psr_group_chat_id", "psr_group_topic_id", "cyril_contact_id", "digest_recipients", "test_banner", "llm_model", "lead_warning_days", "concern_chronic_weeks", "pm_notes_weeks", "leave_clash_days"}
BRIEF_REQUIRED = ("gateway_url", "psr_group_chat_id")
MATCH_KEYS = {"fuzzy_min_score", "fuzzy_min_lead", "honorifics"}
KEYS = {"projects_data_source", "contacts_data_source", "database_path", "meeting_notes_data_source", "jo_data_source"} | BRIEF_KEYS | SHIFT_KEYS | MATCH_KEYS
_NOTION_ID = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")


class ConfigError(Exception):
    """Raised with a message naming the offending key, never its value."""


@dataclass(frozen=True)
class BriefSettings:
    """Saturday briefing settings. Present only when the briefing keys are in config.yaml."""

    gateway_url: str
    group_chat_id: int
    group_topic_id: int | None
    cyril_contact_id: str | None
    test_banner: bool
    psr_secret: str | None  # None when PSR_SECRET is unset; only ``brief --dry-print`` may go without
    digest_recipients: tuple[str, ...] = ()  # list of Contact page ids who receive the master digest
    llm_model: str = DEFAULT_MODEL
    openrouter_api_key: str | None = None  # None when OPENROUTER_API_KEY is unset; only ``--dry-print`` may go without
    lead_warning_days: int = DEFAULT_LEAD_WARNING_DAYS  # a Lead whose Playdate is less than this many days away is flagged
    concern_chronic_weeks: int = DEFAULT_CHRONIC_WEEKS  # a Concern open longer than this many weeks gets a chronic marker
    leave_clash_days: int = DEFAULT_CLASH_DAYS  # a team member's leave within this many days of a project date is a Concern
    pm_notes_weeks: int = DEFAULT_PM_NOTES_WEEKS  # PM Notes blocks older than this go to the LLM only if they hold a future date


@dataclass(frozen=True)
class ShiftSettings:
    """JO shift report settings. Present only when ``shift_reports_enabled`` is true."""

    gateway_url: str
    chat_id: int
    topic_id: int | None
    ops_chat_id: int | None  # where the Notion-outage alert goes; unset: it goes to the shift group
    test_banner: bool
    shift_secret: str | None  # None when SHIFT_SECRET is unset; only ``shift-report --dry-print`` may go without


@dataclass(frozen=True)
class Config:
    projects_data_source: str
    contacts_data_source: str
    database_path: str
    notion_token: str
    brief: BriefSettings | None = None
    jo_data_source: str | None = None  # Job Orders data source; needed by jo-snapshot
    meeting_notes_data_source: str | None = None  # set to draft the minutes (draft-mom, Monday job)
    shift: ShiftSettings | None = None  # None when shift reports are disabled
    matching: MatchSettings = MatchSettings()  # people matching (leave names); defaults match the watchdog


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
        shift=_shift(raw, env),
        matching=_matching(raw),
    )


def _matching(raw: dict) -> MatchSettings:
    d = MatchSettings()
    honorifics = d.honorifics
    if "honorifics" in raw:
        val = raw["honorifics"]
        if (not isinstance(val, list) or not val
                or any(not isinstance(h, str) or not h.strip() for h in val)):
            raise ConfigError("invalid config key: honorifics (expected a non-empty list of words)")
        honorifics = tuple(h.strip().casefold() for h in val)
    return MatchSettings(
        fuzzy_min_score=_percent(raw, "fuzzy_min_score", d.fuzzy_min_score),
        fuzzy_min_lead=_percent(raw, "fuzzy_min_lead", d.fuzzy_min_lead),
        honorifics=honorifics,
    )


def _percent(raw: dict, key: str, default: float) -> float:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise ConfigError(f"invalid config key: {key} (expected a number from 0 to 100)")
    return value


def _brief(raw: dict, env: Mapping[str, str]) -> BriefSettings | None:
    present = BRIEF_KEYS & set(raw)
    if raw.get("shift_reports_enabled", False) is not False:  # the shift reports share these two keys without the briefing
        present -= {"gateway_url", "test_banner"}
    if not present:
        return None
    for key in BRIEF_REQUIRED:
        if key not in raw:
            raise ConfigError(f"missing config key: {key}")
    url = _gateway_url(raw)
    banner = _banner(raw)
    topic = _integer(raw, "psr_group_topic_id") if "psr_group_topic_id" in raw else None
    cyril_id = _notion_id(raw, "cyril_contact_id") if "cyril_contact_id" in raw else None
    digest_recipients: list[str] = []
    if "digest_recipients" in raw:
        val = raw["digest_recipients"]
        if not isinstance(val, list) or not val:
            raise ConfigError("invalid config key: digest_recipients (expected a non-empty list)")
        for i, item in enumerate(val):
            if not isinstance(item, str) or not _NOTION_ID.match(item.strip()):
                raise ConfigError(f"invalid config key: digest_recipients (entry {i} is not a Notion id)")
            digest_recipients.append(item.strip())
    elif cyril_id is not None:
        digest_recipients.append(cyril_id)
    else:
        raise ConfigError("missing config key: cyril_contact_id or digest_recipients")

    return BriefSettings(
        gateway_url=url,
        group_chat_id=_integer(raw, "psr_group_chat_id"),
        group_topic_id=topic,
        cyril_contact_id=cyril_id,
        test_banner=banner,
        psr_secret=env.get(PSR_SECRET_ENV, "").strip() or None,
        digest_recipients=tuple(digest_recipients),
        llm_model=_string(raw, "llm_model", default=DEFAULT_MODEL),
        openrouter_api_key=env.get(OPENROUTER_KEY_ENV, "").strip() or None,
        lead_warning_days=_days(raw, "lead_warning_days", DEFAULT_LEAD_WARNING_DAYS),
        concern_chronic_weeks=_days(raw, "concern_chronic_weeks", DEFAULT_CHRONIC_WEEKS, "weeks"),
        pm_notes_weeks=_days(raw, "pm_notes_weeks", DEFAULT_PM_NOTES_WEEKS, "weeks"),
        leave_clash_days=_days(raw, "leave_clash_days", DEFAULT_CLASH_DAYS),
    )


def _days(raw: dict, key: str, default: int, unit: str = "days") -> int:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"invalid config key: {key} (expected a whole number of {unit}, 1 or more)")
    return value


def _gateway_url(raw: dict) -> str:
    url = _string(raw, "gateway_url")
    if not url.startswith(("http://", "https://")):
        raise ConfigError("invalid config key: gateway_url (expected an http(s) URL)")
    return url


def _banner(raw: dict) -> bool:
    banner = raw.get("test_banner", False)
    if not isinstance(banner, bool):
        raise ConfigError("invalid config key: test_banner (expected true or false)")
    return banner


def _shift(raw: dict, env: Mapping[str, str]) -> ShiftSettings | None:
    enabled = raw.get("shift_reports_enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("invalid config key: shift_reports_enabled (expected true or false)")
    if not enabled:  # nothing else about the shift reports is read, so existing configs behave as before
        return None
    chat = _integer(raw, "shift_chat_id") if "shift_chat_id" in raw else None
    topic = _integer(raw, "shift_topic_id") if "shift_topic_id" in raw else None
    ops = _integer(raw, "ops_chat_id") if "ops_chat_id" in raw else None
    for key in ("jo_data_source", "shift_chat_id"):
        if key not in raw:
            raise ConfigError(f"missing config key: {key} (needed when shift_reports_enabled is true)")
    return ShiftSettings(
        gateway_url=_gateway_url(raw),
        chat_id=chat,
        topic_id=topic,
        ops_chat_id=ops,
        test_banner=_banner(raw),
        shift_secret=env.get(SHIFT_SECRET_ENV, "").strip() or None,
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
