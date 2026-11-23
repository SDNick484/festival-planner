from __future__ import annotations

from contextlib import closing
from typing import TYPE_CHECKING

from flask import Flask, Response, jsonify

from festival_planner import auth, db, seed
from festival_planner.api import register_api
from festival_planner.mcp import PathTokenMiddleware, register_mcp
from festival_planner.service import emails
from festival_planner.ui import register_ui

if TYPE_CHECKING:
    from festival_planner.config import Config

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def create_app(config: Config | None = None) -> Flask:
    """Build the Flask app. The instance config is available as `app.config["FP"]`.

    Callers that run the app (the WSGI entry point, the worker) pass `load_config()`; the factory
    does not read files itself, so tests can build an app without a config.toml. `FP` is None when
    no config was given.
    """
    app = Flask(__name__)
    app.config["FP"] = config
    db.init_app(app)
    if config is not None:
        with closing(db.connect(app.config["DATABASE"])) as con:
            seed.bootstrap(con, config)
    auth.init_app(app)

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.get("/healthz")
    def healthz():
        # last_email_poll: UTC time of the worker's last successful poll (null if never).
        last_poll = None
        if app.config["DATABASE"] is not None:
            last_poll = emails.last_email_poll(db.get_db())
        return jsonify(ok=True, last_email_poll=last_poll)

    register_api(app)
    register_ui(app)
    # Machine clients (design §4.3): the MCP endpoint and the token-authenticated REST mirror.
    # The middleware strips a /mcp/<secret> path token before Flask or gunicorn logs the path.
    register_mcp(app)
    register_api(app, "/mcp/api", "token")
    app.wsgi_app = PathTokenMiddleware(app.wsgi_app)  # type: ignore[method-assign]

    return app
