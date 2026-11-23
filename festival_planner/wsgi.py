"""WSGI entry point for the container: `gunicorn festival_planner.wsgi:app`.

`create_app()` does not read configuration itself, so this module loads it with `load_config()`.
A missing or invalid config raises `ConfigError` at import, which makes gunicorn exit at start
(with `--preload`, before any worker is forked) instead of serving a half-configured app.
"""

from festival_planner.config import load_config
from festival_planner.web import create_app

app = create_app(load_config())
