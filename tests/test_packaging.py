"""Packaging checks: the Dockerfile, the compose example and the deploy guide.

These parse the files as text; the CI `docker` job builds and runs the image for real.
"""

import re
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
