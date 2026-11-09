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
SHIFT_KEYS = {"shift_reports_enabled", "shift_destinations", "shift_chat_id", "shift_topic_id", "ops_chat_id"}
# shift_destinations report names -> job slot codes (the slot codes the job and scheduler use today)
SHIFT_REPORT_SLOTS = {"mid": "1130", "day": "1900", "night": "0500"}
BRIEF_KEYS = {"gateway_url", "psr_group_chat_id", "psr_group_topic_id", "cyril_contact_id", "digest_recipients", "coordinator_recipients", "test_banner", "llm_model", "lead_warning_days", "concern_chronic_weeks", "pm_notes_weeks", "leave_clash_days"}
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
    coordinator_recipients: tuple[str, ...] | None = None  # None: all coordinators; (): no DMs or group fallbacks


@dataclass(frozen=True)
class Destination:
    """One place a shift report goes: a DM (``dm=True``, ``chat`` is the user id, no topic) or a chat with a topic."""

    chat: int
    topic: int | None = None
    dm: bool = False


@dataclass(frozen=True)
class ShiftSettings:
    """JO shift report settings. Present only when ``shift_reports_enabled`` is true."""

    gateway_url: str
    destinations: Mapping[str, tuple[Destination, ...]]  # keyed by slot ("1130", "1900", "0500"); () skips that slot
    ops_chat_id: int | None  # where the Notion-outage alert goes; unset: the first group entry of the slot's list
    test_banner: bool
    shift_secret: str | None  # None when SHIFT_SECRET is unset; only ``shift-report --dry-print`` may go without
    openrouter_api_key: str | None = None  # unset: task summaries are the first three words, no LLM call
    llm_model: str = DEFAULT_MODEL


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

    coordinator_recipients = None
    if "coordinator_recipients" in raw:
        val = raw["coordinator_recipients"]
        if not isinstance(val, list):
            raise ConfigError("invalid config key: coordinator_recipients (expected a list)")
        for i, item in enumerate(val):
            if not isinstance(item, str) or not _NOTION_ID.fullmatch(item.strip()):
                raise ConfigError(f"invalid config key: coordinator_recipients (entry {i} is not a Notion id)")
        coordinator_recipients = tuple(item.strip() for item in val)

    return BriefSettings(
        gateway_url=url,
        group_chat_id=_integer(raw, "psr_group_chat_id"),
        group_topic_id=topic,
        cyril_contact_id=cyril_id,
        test_banner=banner,
        psr_secret=env.get(PSR_SECRET_ENV, "").strip() or None,
        digest_recipients=tuple(digest_recipients),
        coordinator_recipients=coordinator_recipients,
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
    for old in ("shift_chat_id", "shift_topic_id"):
        if old in raw:
            raise ConfigError(f"config key {old} was replaced by shift_destinations")
    ops = _integer(raw, "ops_chat_id") if "ops_chat_id" in raw else None
    if "jo_data_source" not in raw:
        raise ConfigError("missing config key: jo_data_source (needed when shift_reports_enabled is true)")
    destinations = _shift_destinations(raw)
    if not any(destinations.values()):
        raise ConfigError("invalid config key: shift_destinations (needs at least one destination when shift_reports_enabled is true)")
    return ShiftSettings(
        gateway_url=_gateway_url(raw),
        destinations=destinations,
        ops_chat_id=ops,
        test_banner=_banner(raw),
        shift_secret=env.get(SHIFT_SECRET_ENV, "").strip() or None,
        openrouter_api_key=env.get(OPENROUTER_KEY_ENV, "").strip() or None,
        llm_model=_string(raw, "llm_model", default=DEFAULT_MODEL),
    )


def _shift_destinations(raw: dict) -> dict[str, tuple[Destination, ...]]:
    """Parse ``shift_destinations`` into a tuple of destinations per slot. Messages never hold an id."""
    key = "shift_destinations"
    val = raw.get(key, {})
    if val is None:
        val = {}
    if not isinstance(val, dict):
        raise ConfigError(f"invalid config key: {key} (expected a mapping of report name to a list)")
    for name in val:
        if name not in SHIFT_REPORT_SLOTS:
            raise ConfigError(f"invalid config key: {key} (unknown report name; expected mid, day or night)")
    result: dict[str, tuple[Destination, ...]] = {}
    for name, slot in SHIFT_REPORT_SLOTS.items():
        entries = val.get(name)
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            raise ConfigError(f"invalid config key: {key}.{name} (expected a list)")
        seen: set[Destination] = set()
        parsed = []
        for number, entry in enumerate(entries, 1):
            where = f"invalid config key: {key}.{name} (entry {number}"
            if _is_int(entry):
                if entry <= 0:
                    raise ConfigError(f"{where} is a bare id that is not a user id; "
                                      "a group chat must use the {chat, topic} form)")
                dest = Destination(entry, None, dm=True)
            elif isinstance(entry, dict):
                if set(entry) - {"chat", "topic"}:
                    raise ConfigError(f"{where} has a key other than chat and topic)")
                if not _is_int(entry.get("chat")):
                    raise ConfigError(f"{where} needs an integer chat)")
                if "topic" in entry and not _is_int(entry["topic"]):
                    raise ConfigError(f"{where} has a topic that is not an integer)")
                dest = Destination(entry["chat"], entry.get("topic"))
            else:
                raise ConfigError(f"{where} is not a user id or a mapping with chat and optional topic)")
            if dest in seen:
                raise ConfigError(f"{where} repeats an earlier entry)")
            seen.add(dest)
            parsed.append(dest)
        result[slot] = tuple(parsed)
    return result


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


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
