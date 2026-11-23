"""GET|PUT /api/event."""

from __future__ import annotations

from flask import g, jsonify

from festival_planner.api._common import Router, json_body
from festival_planner.db import get_db
from festival_planner.service import event


def register(router: Router) -> None:
    @router.route("/event", "GET")
    def get_event():
        return jsonify(event.get(get_db()))

    @router.route("/event", "PUT")
    def update_event():
        # Partial: only the fields sent change (the event has no required-on-update fields).
        return jsonify(event.update(get_db(), g.actor, json_body()))
