from __future__ import annotations

from typing import TYPE_CHECKING

from flask import Flask, jsonify

if TYPE_CHECKING:
    # Task 2 adds festival_planner.config.Config.
    from festival_planner.config import Config


def create_app(config: Config | None = None) -> Flask:
    """Build the Flask app. `config` is unused until Task 2 adds Config."""
    app = Flask(__name__)

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True)

    return app
