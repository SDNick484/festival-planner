from __future__ import annotations

from typing import TYPE_CHECKING

from flask import Flask, jsonify

if TYPE_CHECKING:
    from festival_planner.config import Config


def create_app(config: Config | None = None) -> Flask:
    """Build the Flask app. The instance config is available as `app.config["FP"]`.

    Callers that run the app (the WSGI entry point, the worker) pass `load_config()`; the factory
    does not read files itself, so tests can build an app without a config.toml. `FP` is None when
    no config was given.
    """
    app = Flask(__name__)
    app.config["FP"] = config

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True)

    return app
