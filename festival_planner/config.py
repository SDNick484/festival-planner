"""Instance configuration: non-secret settings from TOML, secrets from the environment.

`load_config()` is strict: unknown keys, wrong types, out-of-range numbers, bad time zones,
ambiguous addresses and malformed tokens all raise `ConfigError`. Error messages never contain
secret values (token secrets, passwords).
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_CONFIG_PATH = "config.toml"
MIN_TOKEN_SECRET_LENGTH = 24
TOKEN_SCOPES = ("read", "write")
# Activity sources that are not token names (Global Constraints): a token may not impersonate them.
RESERVED_TOKEN_NAMES = frozenset({"web", "email", "system"})

_TOKEN_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_EMAIL_RE = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+")
_HOST_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?")


class ConfigError(Exception):
    """The configuration is missing, malformed or inconsistent."""


@dataclass(frozen=True)
class Organizer:
    email: str
    display_name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Token:
    name: str
    scope: Literal["read", "write"]
    secret: str

    def __repr__(self) -> str:
        # Keep the secret out of logs, tracebacks and pytest output.
        return f"Token(name={self.name!r}, scope={self.scope!r}, secret=<redacted>)"


@dataclass(frozen=True)
class Config:
    event_name: str
    event_date: date
    timezone: ZoneInfo
    organizers: tuple[Organizer, ...]
    seed_workstreams: tuple[str, ...]
    seed_milestones: tuple[tuple[str, int], ...]
    data_dir: Path
    cf_team_domain: str | None = None
    cf_aud: str | None = None
    dev_auth_email: str | None = None
    mcp_tokens: tuple[Token, ...] = ()
    waiting_stale_days: int = 5
    imap_host: str | None = None
    imap_user: str | None = None
    imap_password: str | None = None
    imap_label: str | None = None
    email_poll_seconds: int = 300
    email_retention_days: int = 30
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_password: str | None = None
    digest_weekday: int | None = None
    digest_hour: int = 8
    digest_final_stretch: bool = False

    def __repr__(self) -> str:
        return "Config(<redacted>)"


# --- helpers -------------------------------------------------------------------------------


def _table(data: Mapping[str, Any], key: str, allowed: set[str], *, required: bool = False):
    """Return data[key] as a dict after checking its keys; {} when absent and optional."""
    if key not in data:
        if required:
            raise ConfigError(f"missing [{key}] section")
        return {}
    value = data[key]
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table")
    _no_unknown(value, allowed, f"[{key}]")
    return value


def _no_unknown(table: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(unknown)}")


def _str(table: Mapping[str, Any], key: str, where: str, *, required: bool = False) -> str | None:
    if key not in table:
        if required:
            raise ConfigError(f"{where}.{key} is required")
        return None
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.{key} must be a non-empty string")
    return value.strip()


def _int(
    table: Mapping[str, Any], key: str, where: str, default: int | None, lo: int, hi: int | None
) -> int | None:
    if key not in table:
        return default
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}.{key} must be an integer")
    if value < lo or (hi is not None and value > hi):
        bound = f">= {lo}" if hi is None else f"between {lo} and {hi}"
        raise ConfigError(f"{where}.{key} must be {bound}")
    return value


def _bool(table: Mapping[str, Any], key: str, where: str, default: bool) -> bool:
    if key not in table:
        return default
    if not isinstance(table[key], bool):
        raise ConfigError(f"{where}.{key} must be true or false")
    return table[key]


def _host(value: str | None, where: str) -> str | None:
    if value is not None and not _HOST_RE.fullmatch(value):
        raise ConfigError(f"{where} must be a bare host name (no scheme, port or path)")
    return value


def _email(value: object, where: str) -> str:
    if not isinstance(value, str) or not _EMAIL_RE.fullmatch(value.strip()):
        raise ConfigError(f"{where} must be an email address")
    return value.strip().lower()


def _env(env: Mapping[str, str], key: str) -> str | None:
    """An env value with surrounding whitespace removed; empty counts as unset."""
    value = env.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


# --- sections ------------------------------------------------------------------------------


def _parse_organizers(data: Mapping[str, Any]) -> tuple[Organizer, ...]:
    raw = data.get("organizers")
    if not isinstance(raw, list) or not raw or not all(isinstance(o, dict) for o in raw):
        raise ConfigError("at least one [[organizers]] entry is required")
    seen: dict[str, str] = {}  # address -> where it was first used
    organizers = []
    for i, item in enumerate(raw, start=1):
        where = f"organizers[{i}]"
        _no_unknown(item, {"email", "display_name", "aliases"}, where)
        email = _email(item.get("email"), f"{where}.email")
        display_name = _str(item, "display_name", where, required=True)
        raw_aliases = item.get("aliases", [])
        if not isinstance(raw_aliases, list):
            raise ConfigError(f"{where}.aliases must be a list")
        aliases = tuple(_email(a, f"{where}.aliases") for a in raw_aliases)
        for address in (email, *aliases):
            if address in seen:
                # Covers a duplicate organizer, an alias of another organizer, and a repeat
                # within one organizer: a sender address must identify exactly one person.
                raise ConfigError(
                    f"address used more than once: {address} ({seen[address]}, {where})"
                )
            seen[address] = where
        organizers.append(Organizer(email, cast(str, display_name), aliases))
    return tuple(organizers)


def _parse_seed(data: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[tuple[str, int], ...]]:
    seed = _table(data, "seed", {"workstreams", "milestones"})
    workstreams = seed.get("workstreams", [])
    if not isinstance(workstreams, list) or not all(
        isinstance(w, str) and w.strip() for w in workstreams
    ):
        raise ConfigError("seed.workstreams must be a list of non-empty strings")
    names = tuple(w.strip() for w in workstreams)
    if len({n.casefold() for n in names}) != len(names):
        raise ConfigError("seed.workstreams contains a duplicate name")

    raw = seed.get("milestones", [])
    if not isinstance(raw, list) or not all(isinstance(m, dict) for m in raw):
        raise ConfigError("seed.milestones must be an array of tables")
    milestones = []
    for i, item in enumerate(raw, start=1):
        where = f"seed.milestones[{i}]"
        _no_unknown(item, {"name", "days_before"}, where)
        name = _str(item, "name", where, required=True)
        days = _int(item, "days_before", where, None, 0, None)
        if days is None:
            raise ConfigError(f"{where}.days_before is required")
        milestones.append((cast(str, name), days))
    if len({n.casefold() for n, _ in milestones}) != len(milestones):
        raise ConfigError("seed.milestones contains a duplicate name")
    return names, tuple(milestones)


def parse_mcp_tokens(raw: str | None) -> tuple[Token, ...]:
    """Parse ``name:scope:secret,...``. Several entries may share a name (rotation)."""
    if raw is None or not raw.strip():
        return ()
    tokens: list[Token] = []
    for i, entry in enumerate(raw.split(","), start=1):
        where = f"MCP_TOKENS entry {i}"
        parts = entry.strip().split(":", 2)
        if len(parts) != 3:
            raise ConfigError(f"{where} must look like name:scope:secret")
        name, scope, secret = parts
        if not _TOKEN_NAME_RE.fullmatch(name):
            raise ConfigError(f"{where}: name must be lowercase letters, digits, - or _ (max 32)")
        if name in RESERVED_TOKEN_NAMES:
            raise ConfigError(f"{where}: name {name!r} is reserved")
        if scope not in TOKEN_SCOPES:
            raise ConfigError(f"{where}: scope must be read or write")
        if len(secret) < MIN_TOKEN_SECRET_LENGTH:
            raise ConfigError(
                f"{where}: secret must be at least {MIN_TOKEN_SECRET_LENGTH} characters"
            )
        if re.search(r"\s|/", secret):
            raise ConfigError(f"{where}: secret must not contain whitespace or '/'")
        tokens.append(Token(name, cast(Literal["read", "write"], scope), secret))
    if len({t.secret for t in tokens}) != len(tokens):
        # One secret must identify one (name, scope), or the activity source would be ambiguous.
        raise ConfigError("MCP_TOKENS contains a secret used more than once")
    return tuple(tokens)


# --- entry point ---------------------------------------------------------------------------


def load_config(path: Path | None = None, env: Mapping[str, str] = os.environ) -> Config:
    """Load and validate the configuration. Raises `ConfigError` on any problem."""
    if path is None:
        path = Path(_env(env, "FP_CONFIG") or DEFAULT_CONFIG_PATH)
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from None

    _no_unknown(
        data, {"event", "organizers", "seed", "needs_attention", "email", "digest"}, "config"
    )

    event = _table(data, "event", {"name", "date", "timezone", "data_dir"}, required=True)
    event_name = _str(event, "name", "event", required=True)
    event_date = event.get("date")
    if type(event_date) is not date:  # a TOML local date, not a datetime or a string
        raise ConfigError("event.date must be a TOML date such as 2099-10-10")
    tz_name = _str(event, "timezone", "event", required=True)
    try:
        timezone = ZoneInfo(cast(str, tz_name))
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ConfigError(f"event.timezone is not a known IANA time zone: {tz_name}") from None
    data_dir = Path(_str(event, "data_dir", "event") or "data")

    organizers = _parse_organizers(data)
    seed_workstreams, seed_milestones = _parse_seed(data)

    attention = _table(data, "needs_attention", {"waiting_stale_days"})
    waiting_stale_days = _int(attention, "waiting_stale_days", "needs_attention", 5, 1, None)

    mail = _table(
        data,
        "email",
        {
            "imap_host",
            "imap_user",
            "imap_label",
            "poll_seconds",
            "retention_days",
            "smtp_host",
            "smtp_port",
        },
    )
    imap_host = _host(_str(mail, "imap_host", "email"), "email.imap_host")
    imap_user = _str(mail, "imap_user", "email")
    if imap_host and not imap_user:
        raise ConfigError("email.imap_user is required when email.imap_host is set")
    smtp_host = _host(_str(mail, "smtp_host", "email"), "email.smtp_host")

    digest = _table(data, "digest", {"weekday", "hour", "final_stretch"})

    # Secrets and the development switch come from the environment only.
    cf_team_domain = _env(env, "CF_TEAM_DOMAIN")
    cf_aud = _env(env, "CF_AUD")
    if cf_team_domain is not None:
        _host(cf_team_domain, "CF_TEAM_DOMAIN")
    if (cf_team_domain is None) != (cf_aud is None):
        raise ConfigError("CF_TEAM_DOMAIN and CF_AUD must be set together")

    dev_auth_email = _env(env, "DEV_AUTH_EMAIL")
    if dev_auth_email is not None:
        if _env(env, "FLASK_ENV") != "development":
            raise ConfigError("DEV_AUTH_EMAIL is only allowed when FLASK_ENV=development")
        dev_auth_email = _email(dev_auth_email, "DEV_AUTH_EMAIL")
        if dev_auth_email not in {o.email for o in organizers}:
            raise ConfigError("DEV_AUTH_EMAIL must be one of the configured organizers")

    return Config(
        event_name=cast(str, event_name),
        event_date=event_date,
        timezone=timezone,
        organizers=organizers,
        seed_workstreams=seed_workstreams,
        seed_milestones=seed_milestones,
        data_dir=data_dir,
        cf_team_domain=cf_team_domain,
        cf_aud=cf_aud,
        dev_auth_email=dev_auth_email,
        mcp_tokens=parse_mcp_tokens(_env(env, "MCP_TOKENS")),
        waiting_stale_days=cast(int, waiting_stale_days),
        imap_host=imap_host,
        imap_user=imap_user,
        imap_password=_env(env, "IMAP_PASSWORD"),
        imap_label=_str(mail, "imap_label", "email"),
        email_poll_seconds=cast(int, _int(mail, "poll_seconds", "email", 300, 1, None)),
        email_retention_days=cast(int, _int(mail, "retention_days", "email", 30, 1, None)),
        smtp_host=smtp_host,
        smtp_port=_int(mail, "smtp_port", "email", None, 1, 65535),
        smtp_password=_env(env, "SMTP_PASSWORD"),
        digest_weekday=_int(digest, "weekday", "digest", None, 0, 6),
        digest_hour=cast(int, _int(digest, "hour", "digest", 8, 0, 23)),
        digest_final_stretch=_bool(digest, "final_stretch", "digest", False),
    )
