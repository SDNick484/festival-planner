"""/api/workstreams."""

from __future__ import annotations

from flask import g, jsonify

from festival_planner.api._common import (
    ID,
    Query,
    Router,
    json_body,
    no_content,
    organizer_emails,
    page_args,
)
from festival_planner.db import get_db
from festival_planner.service import workstreams


def register(router: Router) -> None:
    @router.route("/workstreams", "GET")
    def list_workstreams():
        query = Query({"limit", "offset"})
        paging = page_args(query)
        query.done()
        return jsonify(workstreams.list(get_db(), **paging))

    @router.route("/workstreams", "POST")
    def create_workstream():
        row = workstreams.create(get_db(), g.actor, json_body(), organizers=organizer_emails())
        return jsonify(row), 201

    @router.route(f"/workstreams/{ID}", "GET")
    def get_workstream(id: int):
        return jsonify(workstreams.get(get_db(), id))

    @router.route(f"/workstreams/{ID}", "PATCH")
    def update_workstream(id: int):
        row = workstreams.update(get_db(), g.actor, id, json_body(), organizers=organizer_emails())
        return jsonify(row)

    @router.route(f"/workstreams/{ID}", "DELETE")
    def delete_workstream(id: int):
        workstreams.delete(get_db(), g.actor, id)
        return no_content()
