"""Packaging checks: the Dockerfile, the compose example and the deploy guide.

These parse the files as text; the CI `docker` job builds and runs the image for real.
"""

import re
import subprocess
from pathlib import Path

import pytest

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


# --- deploy/alpine/check.sh: an install the service user cannot run fails loudly ----------------
# Each case builds a small fake install root (a .venv with a `gunicorn` script whose shebang names
# its interpreter) and runs check.sh against it. As root (the Alpine CI job) the checks run as
# `nobody`, so permissions still mean something; otherwise as the current user.


def _check_user() -> str:
    import getpass
    import os

    return "nobody" if os.geteuid() == 0 else getpass.getuser()


@pytest.fixture
def base(tmp_path):
    """Where a fake install is built. As root, pytest's own temp folder (/tmp/pytest-of-root, mode
    700) is closed to `nobody`, so use a fresh folder directly under /tmp instead."""
    import os
    import shutil
    import tempfile

    if os.geteuid() != 0:
        yield tmp_path
        return
    path = Path(tempfile.mkdtemp(prefix="fp-check-", dir="/tmp"))
    path.chmod(0o755)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _fake_install(base: Path, *, shebang_root: Path | None = None, data_dir: str | None = None):
    """A minimal install root under base/opt/fp, plus a config.toml. Returns (root, config)."""
    import os
    import sys

    base.chmod(0o755)
    root = base / "opt" / "fp"
    bin_dir = root / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to(sys.executable)
    interp = (shebang_root or root) / ".venv" / "bin" / "python"
    gunicorn = bin_dir / "gunicorn"
    gunicorn.write_text(f"#!{interp}\nprint('gunicorn (version 99)')\n", encoding="utf-8")
    gunicorn.chmod(0o755)
    data = base / "var" / "lib" / "fp"
    data.mkdir(parents=True)
    if os.geteuid() == 0:
        import pwd

        nobody = pwd.getpwnam("nobody")
        os.chown(data, nobody.pw_uid, nobody.pw_gid)
    for d in (base / "opt", base / "var", base / "var" / "lib", root, root / ".venv", bin_dir):
        d.chmod(0o755)
    data.chmod(0o700)
    # The service user must not be able to change its own code.
    root.chmod(0o555)
    config = base / "config.toml"
    config.write_text(f'[event]\nname = "x"\n\ndata_dir = "{data_dir or data}"\n', encoding="utf-8")
    config.chmod(0o644)
    return root, config


def _run_check(root: Path, config: Path | None = None) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, "FP_USER": _check_user()}
    args = ["sh", str(ROOT / ALPINE / "check.sh"), str(root)]
    if config is not None:
        args.append(str(config))
    return subprocess.run(args, capture_output=True, text=True, env=env, check=False)  # noqa: S603


def test_alpine_check_passes_a_good_install(base):
    root, config = _fake_install(base)
    result = _run_check(root, config)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_alpine_check_flags_a_venv_built_at_another_path(base):
    root, config = _fake_install(base, shebang_root=Path("/root/src/festival-planner"))
    result = _run_check(root, config)
    assert result.returncode == 1
    assert "built at another path" in result.stderr
    assert "/root/src/festival-planner/.venv/bin/python" in result.stderr


def test_alpine_check_flags_a_root_the_service_user_cannot_enter(base):
    root, config = _fake_install(base)
    (base / "opt").chmod(0o700 if _check_user() == "nobody" else 0o000)
    try:
        result = _run_check(root, config)
    finally:
        (base / "opt").chmod(0o755)
    assert result.returncode == 1
    assert "cannot enter" in result.stderr and str(base / "opt") in result.stderr


def test_alpine_check_flags_code_the_service_user_can_change(base):
    import os

    root, config = _fake_install(base)
    if os.geteuid() == 0:
        import pwd

        nobody = pwd.getpwnam("nobody")
        os.chown(root, nobody.pw_uid, nobody.pw_gid)
    root.chmod(0o755)
    result = _run_check(root, config)
    assert result.returncode == 1
    assert "can write to its own code" in result.stderr


def test_alpine_check_flags_a_relative_data_dir(base):
    root, config = _fake_install(base, data_dir="data")
    result = _run_check(root, config)
    assert result.returncode == 1
    assert "data_dir" in result.stderr and "absolute" in result.stderr


def test_alpine_check_flags_a_data_dir_the_service_user_cannot_write(base):
    root, config = _fake_install(base)
    (base / "var" / "lib" / "fp").chmod(0o500 if _check_user() != "nobody" else 0o700)
    if _check_user() == "nobody":
        import os

        os.chown(base / "var" / "lib" / "fp", 0, 0)
    result = _run_check(root, config)
    assert result.returncode == 1
    assert "cannot write" in result.stderr and "data_dir" in result.stderr


def test_alpine_check_without_a_config_checks_the_code_only(base):
    root, _ = _fake_install(base)
    result = _run_check(root, base / "missing.toml")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no settings file" in result.stdout


def test_alpine_install_rebuilds_a_venv_built_at_another_path(tmp_path):
    """Run just the stale-venv step of install.sh against a moved checkout."""
    script = (ROOT / ALPINE / "install.sh").read_text(encoding="utf-8")
    block = re.search(r"(# stale-venv-begin\n.*?# stale-venv-end\n)", script, flags=re.S)
    assert block
    root, _ = _fake_install(tmp_path, shebang_root=Path("/root/src/festival-planner"))
    root.chmod(0o755)
    harness = f'say() {{ :; }}\nroot="{root}"\n{block.group(1)}'
    _sh("-eu", "-c", harness)
    assert not (root / ".venv").exists()
    # A venv built in place is kept.
    (tmp_path / "second").mkdir()
    root2, _ = _fake_install(tmp_path / "second")
    root2.chmod(0o755)
    _sh("-eu", "-c", harness.replace(str(root), str(root2)))
    assert (root2 / ".venv" / "bin" / "gunicorn").exists()


def test_alpine_install_and_init_scripts_run_the_check():
    install = (ROOT / ALPINE / "install.sh").read_text(encoding="utf-8")
    assert re.search(r'sh "\$here/check\.sh" "\$root"', install)
    for name in ("festival-planner-web.initd", "festival-planner-worker.initd"):
        script = (ROOT / ALPINE / name).read_text(encoding="utf-8")
        start_pre = script.split("start_pre() {", 1)[1]
        assert 'deploy/alpine/check.sh" "$FP_ROOT"' in start_pre, name


def test_deploy_doc_tunnel_target_matches_each_path():
    doc = _text("docs/deploy.md")
    section4 = doc.split("## 4.", 1)[1].split("\n## 5.", 1)[0]
    # Section 4 no longer gives one target for both paths: Docker and Alpine each get theirs.
    assert "http://web:8000" in section4 or "Docker" in section4
    assert "127.0.0.1:8000" in section4 and "FP_WEB_BIND" in section4
    assert "deploy/alpine/check.sh" in doc
