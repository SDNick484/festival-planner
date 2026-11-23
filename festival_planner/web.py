from __future__ import annotations

from typing import TYPE_CHECKING

from flask import Flask, Response, jsonify

from festival_planner import auth, db

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
    auth.init_app(app)

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True)

    return app
