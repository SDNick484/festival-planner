"""Playwright fixtures: the real app on a live local server, signed in through DEV_AUTH_EMAIL.

The server runs in a thread with a temp data directory. `load_config` is used with an explicit
environment, so the development sign-in goes through the same checks as a real start (it needs
FLASK_ENV=development and must be an organizer). If Chromium is missing, run
`uv run playwright install chromium`.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from festival_planner.config import load_config
from festival_planner.web import create_app

ROOT = Path(__file__).resolve().parents[2]
ORGANIZER = "organizer-a@example.com"


@pytest.fixture(scope="session")
def live_server(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("data")
    example = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    assert 'data_dir = "data"' in example
    config_path = data_dir / "config.toml"
    config_path.write_text(
        example.replace('data_dir = "data"', f'data_dir = "{data_dir.as_posix()}"'),
        encoding="utf-8",
    )
    cfg = load_config(config_path, env={"FLASK_ENV": "development", "DEV_AUTH_EMAIL": ORGANIZER})
    app = create_app(cfg)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture(scope="session")
def base_url(live_server):
    return live_server
