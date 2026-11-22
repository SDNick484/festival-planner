from __future__ import annotations

import textwrap
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from festival_planner.config import Config, ConfigError, Token, load_config
from festival_planner.web import create_app

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"
SECRET_A = "a" * 24
SECRET_B = "b" * 24
SECRET_C = "c" * 24


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent(body))
    return path


def minimal(organizers: str = "", event_extra: str = "", tz: str = "UTC") -> str:
    organizers = (
        organizers
        or """
        [[organizers]]
        email = "Organizer-A@Example.com"
        display_name = "Organizer A"
        """
    )
    return f"""
        [event]
        name = "Example Fall Festival"
        date = 2099-10-10
        timezone = "{tz}"
        {event_extra}
        {organizers}
        """


def test_loads_example_config():
    cfg = load_config(EXAMPLE, env={})
    assert cfg.event_name == "Example Fall Festival"
    assert len(cfg.organizers) == 2
    assert cfg.event_date == date(2099, 10, 10)
    assert cfg.timezone == ZoneInfo("UTC")
    assert len(cfg.seed_workstreams) == 7
    assert cfg.seed_workstreams[0] == "Rentals & Rides"
    assert cfg.seed_milestones == (
        ("T-60", 60),
        ("T-45", 45),
        ("T-30", 30),
        ("T-21", 21),
        ("T-14", 14),
        ("T-7", 7),
        ("T-1", 1),
    )
    assert cfg.organizers[1].aliases == ("organizer-b@example.org",)
    assert cfg.mcp_tokens == () and cfg.dev_auth_email is None


def test_config_is_frozen():
    cfg = load_config(EXAMPLE, env={})
    with pytest.raises(AttributeError):
        cfg.event_name = "Other"  # type: ignore[misc]


def test_defaults_when_sections_omitted(tmp_path):
    cfg = load_config(write_config(tmp_path, minimal()), env={})
    assert cfg.organizers[0].email == "organizer-a@example.com"  # lowercased
    assert cfg.waiting_stale_days == 5
    assert cfg.email_poll_seconds == 300 and cfg.email_retention_days == 30
    assert cfg.digest_weekday is None and cfg.digest_hour == 8 and cfg.digest_final_stretch is False
    assert cfg.imap_host is None and cfg.smtp_host is None and cfg.smtp_port is None
    assert cfg.seed_workstreams == () and cfg.seed_milestones == ()
    assert cfg.data_dir == Path("data")


def test_secrets_come_from_env_only(tmp_path):
    imap_value, smtp_value = "imap-value", "smtp-value"
    env = {
        "CF_TEAM_DOMAIN": "example-team.example.com",
        "CF_AUD": "aud-tag",
        "IMAP_PASSWORD": imap_value,
        "SMTP_PASSWORD": smtp_value,
    }
    cfg = load_config(write_config(tmp_path, minimal()), env=env)
    assert cfg.cf_team_domain == "example-team.example.com" and cfg.cf_aud == "aud-tag"
    assert (cfg.imap_password, cfg.smtp_password) == (imap_value, smtp_value)
    secret_in_toml = minimal(event_extra='imap_password = "x"')
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, secret_in_toml), env={})


def test_cf_values_must_be_set_together(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, minimal()), env={"CF_AUD": "aud-tag"})


def test_mcp_tokens_parse(tmp_path):
    env = {"MCP_TOKENS": f"claude:write:{SECRET_A},duet:read:{SECRET_B},claude:write:{SECRET_C}"}
    cfg = load_config(write_config(tmp_path, minimal()), env=env)
    assert len(cfg.mcp_tokens) == 3
    assert {t.name for t in cfg.mcp_tokens} == {"claude", "duet"}
    assert cfg.mcp_tokens[1] == Token("duet", "read", SECRET_B)


def test_token_repr_hides_secret():
    assert SECRET_A not in repr(Token("claude", "write", SECRET_A))


@pytest.mark.parametrize(
    "value",
    [
        f"x:admin:{SECRET_A}",  # scope
        f"claude:write:{SECRET_A},,duet:read:{SECRET_B}",  # empty entry
        f"claude:write{SECRET_A}",  # missing field
        f"web:write:{SECRET_A}",  # reserved source name
        f"Claude:write:{SECRET_A}",  # name charset
        f"claude:write:{SECRET_A},duet:read:{SECRET_A}",  # one secret, two identities
    ],
)
def test_malformed_tokens_rejected(tmp_path, value):
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, minimal()), env={"MCP_TOKENS": value})
    assert SECRET_A not in str(exc.value)


def test_bad_token_scope_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, minimal()), env={"MCP_TOKENS": "x:admin:zzz"})
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, minimal()), env={"MCP_TOKENS": f"x:admin:{SECRET_A}"})


def test_short_token_secret_rejected(tmp_path):
    secret = "s" * 23
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, minimal()), env={"MCP_TOKENS": f"claude:read:{secret}"})
    assert secret not in str(exc.value)


def test_dev_auth_refused_outside_development(tmp_path):
    path = write_config(tmp_path, minimal())
    env = {"DEV_AUTH_EMAIL": "organizer-a@example.com"}
    with pytest.raises(ConfigError):
        load_config(path, env=env)
    with pytest.raises(ConfigError):
        load_config(path, env={**env, "FLASK_ENV": "production"})
    cfg = load_config(path, env={**env, "FLASK_ENV": "development"})
    assert cfg.dev_auth_email == "organizer-a@example.com"


def test_dev_auth_must_be_an_organizer(tmp_path):
    env = {"DEV_AUTH_EMAIL": "stranger@example.com", "FLASK_ENV": "development"}
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, minimal()), env=env)


def test_alias_cannot_duplicate_other_organizer(tmp_path):
    body = """
        [[organizers]]
        email = "organizer-a@example.com"
        display_name = "Organizer A"
        aliases = []

        [[organizers]]
        email = "organizer-b@example.com"
        display_name = "Organizer B"
        aliases = ["Organizer-A@example.com"]
        """
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, minimal(organizers=body)), env={})


def test_alias_cannot_repeat_within_one_organizer(tmp_path):
    body = """
        [[organizers]]
        email = "organizer-a@example.com"
        display_name = "Organizer A"
        aliases = ["organizer-a@example.org", "organizer-a@example.org"]
        """
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, minimal(organizers=body)), env={})


def test_duplicate_organizer_rejected(tmp_path):
    body = """
        [[organizers]]
        email = "organizer-a@example.com"
        display_name = "Organizer A"

        [[organizers]]
        email = "ORGANIZER-A@example.com"
        display_name = "Organizer A again"
        """
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, minimal(organizers=body)), env={})


def test_invalid_timezone(tmp_path):
    for tz in ("Mars/Olympus_Mons", "../../etc/passwd", ""):
        with pytest.raises(ConfigError):
            load_config(write_config(tmp_path, minimal(tz=tz)), env={})


def test_event_date_must_be_a_date(tmp_path):
    body = minimal().replace("date = 2099-10-10", 'date = "2099-10-10"')
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, body), env={})


def test_unknown_keys_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, minimal(event_extra="timezon = 1")), env={})
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, minimal() + "\n[mystery]\nx = 1\n"), env={})


@pytest.mark.parametrize(
    "extra",
    [
        "[digest]\nweekday = 7",
        "[digest]\nhour = 24",
        "[digest]\nfinal_stretch = 1",
        "[needs_attention]\nwaiting_stale_days = 0",
        "[needs_attention]\nwaiting_stale_days = true",
        "[email]\npoll_seconds = 0",
        "[email]\nsmtp_port = 70000",
        "[email]\nimap_host = 'imap.example.com'",  # user required with host
        "[email]\nimap_host = 'https://imap.example.com'",
        "[seed]\nworkstreams = ['A', 'a']",
        "[seed]\nmilestones = [{ name = 'T-1' }]",
        "[seed]\nmilestones = [{ name = 'T-1', days_before = -1 }]",
    ],
)
def test_out_of_range_values_rejected(tmp_path, extra):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, minimal() + "\n" + extra), env={})


def test_missing_and_malformed_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.toml", env={})
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, "this is = = not toml"), env={})


def test_path_defaults_to_fp_config_env(tmp_path):
    path = write_config(tmp_path, minimal())
    assert load_config(env={"FP_CONFIG": str(path)}).event_name == "Example Fall Festival"


def test_create_app_stores_config():
    cfg = load_config(EXAMPLE, env={})
    app = create_app(cfg)
    assert isinstance(app.config["FP"], Config) and app.config["FP"] is cfg
