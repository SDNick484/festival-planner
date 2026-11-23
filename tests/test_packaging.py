"""Packaging checks: the Dockerfile, the compose example and the deploy guide.

These parse the files as text; the CI `docker` job builds and runs the image for real.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _instructions(dockerfile: str) -> list[tuple[str, str]]:
    """(INSTRUCTION, arguments) pairs, with line continuations joined and comments dropped."""
    joined = re.sub(r"\\\n", " ", dockerfile)
    pairs = []
    for line in joined.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        keyword, _, rest = line.partition(" ")
        pairs.append((keyword.upper(), rest.strip()))
    return pairs


def test_dockerfile_nonroot_and_healthcheck():
    steps = _instructions(_text("Dockerfile"))
    by_keyword: dict[str, list[str]] = {}
    for keyword, rest in steps:
        by_keyword.setdefault(keyword, []).append(rest)

    assert by_keyword["FROM"][0].startswith("python:3.12-slim")
    # The last USER is the one the process runs as: it must be the unprivileged `app` user.
    assert by_keyword["USER"][-1] in {"app", "10001", "app:app", "10001:10001"}
    assert any("useradd" in run and "10001" in run and "app" in run for run in by_keyword["RUN"])
    assert any(
        "uv sync" in run and "--frozen" in run and "--no-dev" in run for run in by_keyword["RUN"]
    )
    assert "/data" in by_keyword["VOLUME"][0]
    assert by_keyword["EXPOSE"] == ["8000"]
    health = by_keyword["HEALTHCHECK"][0]
    assert "/healthz" in health and "8000" in health
    cmd = by_keyword["CMD"][0]
    assert "gunicorn" in cmd and "0.0.0.0:8000" in cmd
    # The container must load the config through load_config(), not call the bare factory.
    assert "festival_planner.wsgi:app" in cmd
    # tz database for ZoneInfo on a slim image.
    assert any("tzdata" in run for run in by_keyword["RUN"])


def test_wsgi_entry_point_loads_config():
    source = _text("festival_planner/wsgi.py")
    assert "load_config()" in source and "create_app(" in source


def test_compose_example_has_no_secrets():
    text = _text("compose.example.yml")
    assert "env_file" in text and ".env" in text
    active = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    body = "\n".join(active)
    assert "fp-data:/data" in body
    assert "config.toml" in body and ":ro" in body
    assert "8000" in body
    # No literal secret values anywhere, in live or commented lines.
    for secret_name in ("CF_AUD", "MCP_TOKENS", "IMAP_PASSWORD", "SMTP_PASSWORD", "DEV_AUTH_EMAIL"):
        assert not re.search(rf"{secret_name}\s*[=:]\s*\S", text), secret_name
    # Any environment value that is set must be a placeholder or an interpolation.
    for line in re.finditer(r"TUNNEL_TOKEN\s*[=:]\s*(\S+)", text):
        value = line.group(1).strip("\"'")
        assert value.startswith("<") or value.startswith("${"), value
    # The worker service is live (Task 15) and runs the worker module from the same image.
    assert re.search(r"^\s{2}worker:", body, flags=re.M)
    assert '"-m", "festival_planner.worker"' in body


def test_deploy_doc_mentions_mcp_bypass():
    doc = _text("docs/deploy.md")
    assert "/mcp/*" in doc and "/healthz" in doc
    assert "Bypass" in doc
    assert "CF_AUD" in doc
    assert "festival.example.com" in doc
    assert "backup.sh" in doc
    assert "deploy/local/" in doc


def test_backup_script_rotates_and_uses_sqlite_backup():
    script = _text("scripts/backup.sh")
    assert script.startswith("#!/bin/sh")
    assert "set -eu" in script
    assert ".backup" in script and "-mtime" in script and "14" in script


# --- Alpine (native, OpenRC) deployment ------------------------------------------------------

ALPINE = "deploy/alpine"


def _sh(*args: str) -> None:
    subprocess.run(["sh", *args], check=True)  # noqa: S603, S607 (our own scripts, no user input)


def _assignments(script: str) -> dict[str, str]:
    """Top-level `name="value"` lines of an OpenRC script, quotes removed."""
    found = {}
    for match in re.finditer(r'^([a-z_]+)=("?)(.*)\2$', script, flags=re.M):
        found[match.group(1)] = match.group(3)
    return found


def test_alpine_init_scripts_supervise_as_unprivileged_user():
    for name, entry in (
        ("web", "festival_planner.wsgi:app"),
        ("worker", "festival_planner.worker"),
    ):
        script = _text(f"{ALPINE}/festival-planner-{name}.initd")
        assert script.startswith("#!/sbin/openrc-run")
        settings = _assignments(script)
        assert settings["supervisor"] == "supervise-daemon"
        # Respawn on crash, with no cap on restarts.
        assert settings["respawn_max"] == "0" and int(settings["respawn_delay"]) > 0
        user = settings["command_user"].split(":")[0]
        assert user == "festival" and user not in {"root", "0"}
        assert entry in settings["command_args"]
        # The working directory is the install root, and the app runs from its virtualenv.
        assert settings["directory"] == "${FP_ROOT}"
        assert settings["command"].startswith("${FP_ROOT}/.venv/bin/")
        assert 'FP_ROOT="${FP_ROOT:-/opt/festival-planner}"' in script
        # The secrets file is shared by both services, exported to the process.
        assert "/etc/conf.d/festival-planner" in script and "set -a" in script
        assert re.search(r"^\s*need net\b", script, flags=re.M)


def test_alpine_web_script_matches_the_docker_command():
    args = _assignments(_text(f"{ALPINE}/festival-planner-web.initd"))["command_args"]
    assert "gunicorn" in _assignments(_text(f"{ALPINE}/festival-planner-web.initd"))["command"]
    for flag in ("-w 2", "--preload", "--no-control-socket"):
        assert flag in args
    # Loopback by default (cloudflared runs on the same host); FP_WEB_BIND overrides it.
    assert "-b ${FP_WEB_BIND:-127.0.0.1:8000}" in args
    assert "FP_WEB_BIND" in _text(f"{ALPINE}/festival-planner.confd")


def test_alpine_worker_script_runs_the_worker_after_web():
    script = _text(f"{ALPINE}/festival-planner-worker.initd")
    assert _assignments(script)["command_args"] == "-m festival_planner.worker"
    assert re.search(r"^\s*after festival-planner-web\b", script, flags=re.M)


def test_alpine_confd_example_has_placeholders_only():
    text = _text(f"{ALPINE}/festival-planner.confd")
    # The same variables as .env.example, none with a real-looking value.
    for name in ("CF_TEAM_DOMAIN", "CF_AUD", "MCP_TOKENS", "IMAP_PASSWORD", "SMTP_PASSWORD"):
        assert re.search(rf"^{name}='", text, flags=re.M), name
    values = {
        key: value.strip("'")
        for key, value in re.findall(r"^([A-Z_]+)=(\S+)$", text, flags=re.M)
        if key not in {"FP_CONFIG"}
    }
    for key in ("CF_AUD", "IMAP_PASSWORD", "SMTP_PASSWORD"):
        assert values[key].startswith("replace-with"), key
    assert values["CF_TEAM_DOMAIN"].endswith(".example.com")
    for entry in values["MCP_TOKENS"].split(","):
        assert entry.split(":")[2].startswith("replace-with"), entry
    # Local development switches are never active in the example.
    assert not re.search(r"^(FLASK_ENV|DEV_AUTH_EMAIL)=", text, flags=re.M)


def test_alpine_install_script_is_safe_and_idempotent():
    path = ROOT / ALPINE / "install.sh"
    script = path.read_text(encoding="utf-8")
    assert script.startswith("#!/bin/sh") and "set -eu" in script
    _sh("-n", str(path))
    assert "uv sync --frozen --no-dev" in script
    assert "apk add" in script and "tzdata" in script and "sqlite" in script
    # The pinned uv release fallback is checksum verified.
    assert re.search(r"UV_SHA256_X86_64=[0-9a-f]{64}\b", script) and "sha256sum -c" in script
    # An existing conf.d is kept, and a new one is private and root-owned.
    assert re.search(r'if \[ -e "\$confd" \]; then\n\s+say "keeping existing', script)
    assert "install -m 600 -o root -g root" in script
    # Tight modes on the data and backup directories.
    assert re.search(r'install -d -m 700 -o "\$fp_user" -g "\$fp_user" "\$data_dir"', script)
    assert re.search(r'install -d -m 700 -o "\$fp_user" -g "\$fp_user" "\$backup_dir"', script)
    # It never starts or enables a service: the next-steps text is indented much deeper than any
    # command in the script.
    assert not re.search(r"^ {0,12}(rc-service|rc-update)\b", script, flags=re.M)
    assert "--with-cron" in script and "scripts/backup.sh" in script


def test_alpine_install_script_keeps_an_existing_confd(tmp_path):
    """Run just the conf.d step of install.sh against a pre-existing file."""
    script = (ROOT / ALPINE / "install.sh").read_text(encoding="utf-8")
    block = re.search(r'(if \[ -e "\$confd" \]; then.*?\nfi\n)', script, flags=re.S)
    assert block
    confd = tmp_path / "festival-planner"
    confd.write_text("MY_EDIT='keep-me'\n", encoding="utf-8")
    here = ROOT / ALPINE
    # chown to root:root needs privileges; the test only checks that the content is kept.
    step = block.group(1).replace("chown root:root", "true")
    harness = f'say() {{ :; }}\nconfd="{confd}"\nhere="{here}"\n{step}'
    _sh("-eu", "-c", harness)
    assert confd.read_text(encoding="utf-8") == "MY_EDIT='keep-me'\n"
    confd.unlink()
    _sh("-eu", "-c", harness.replace("install -m 600 -o root -g root", "install -m 600"))
    assert "replace-with" in confd.read_text(encoding="utf-8")
    assert oct(confd.stat().st_mode & 0o777) == "0o600"


def test_alpine_files_are_not_gitignored():
    patterns = {
        line.strip()
        for line in _text(".gitignore").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert "deploy/local/" in patterns
    hiding = {"deploy", "deploy/", "deploy/alpine", "deploy/alpine/", "*.sh", "*.initd", "*.confd"}
    assert not patterns & hiding
    assert (ROOT / ALPINE / "install.sh").is_file()


def test_deploy_doc_has_alpine_and_docker_sections():
    doc = _text("docs/deploy.md")
    assert "Alpine (native, no Docker)" in doc
    assert "docker compose up -d" in doc and "docker build -t festival-planner:local ." in doc
    for needle in (
        "deploy/alpine/install.sh",
        "rc-update add festival-planner-web",
        "rc-service festival-planner-worker start",
        "/etc/conf.d/festival-planner",
        "uv sync --frozen --no-dev",
        "last_email_poll",
        "cloudflared",
        "scripts/backup.sh",
        "/var/log/festival-planner",
    ):
        assert needle in doc, needle
    assert "festival.example.com" in doc and "<host>" in doc


def test_ci_has_a_pinned_alpine_job():
    ci = _text(".github/workflows/ci.yml")
    job = ci.split("\n  alpine:\n", 1)[1]
    assert re.search(r"ALPINE_IMAGE: alpine@sha256:[0-9a-f]{64}\b", job)
    for needle in ("deploy/alpine/install.sh", "rc-service festival-planner-web start", "/healthz"):
        assert needle in job, needle
    assert "--ignore=tests/ui" in job
    # Every action is pinned by full commit SHA.
    for ref in re.findall(r"uses:\s*\S+@(\S+)", ci):
        assert re.fullmatch(r"[0-9a-f]{40}", ref), ref
