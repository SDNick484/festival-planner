"""/api/milestones."""

from __future__ import annotations

from flask import g, jsonify

from festival_planner.api._common import ID, Query, Router, json_body, no_content, page_args
from festival_planner.db import get_db
from festival_planner.service import milestones


def register(router: Router) -> None:
    @router.route("/milestones", "GET")
    def list_milestones():
        query = Query({"limit", "offset"})
        paging = page_args(query)
        query.done()
        return jsonify(milestones.list(get_db(), **paging))

    @router.route("/milestones", "POST")
    def create_milestone():
        return jsonify(milestones.create(get_db(), g.actor, json_body())), 201

    @router.route(f"/milestones/{ID}", "GET")
    def get_milestone(id: int):
        return jsonify(milestones.get(get_db(), id))

    @router.route(f"/milestones/{ID}", "PATCH")
    def update_milestone(id: int):
        return jsonify(milestones.update(get_db(), g.actor, id, json_body()))

    @router.route(f"/milestones/{ID}", "DELETE")
    def delete_milestone(id: int):
        milestones.delete(get_db(), g.actor, id)
        return no_content()
