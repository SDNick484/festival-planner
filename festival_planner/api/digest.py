"""GET /api/digest: the same payload as the MCP `get_digest` tool."""

from __future__ import annotations

from flask import jsonify

from festival_planner import views
from festival_planner.api._common import Query, Router, config
from festival_planner.db import get_db


def register(router: Router) -> None:
    @router.route("/digest", "GET")
    def get_digest():
        Query(()).done()
        return jsonify(views.build_digest(get_db(), config()))
