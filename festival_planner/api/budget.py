"""/api/budget: the summary with every item, and item CRUD (Task 22).

The net goal is not a route here: it is `net_goal_cents` on `PUT /api/event` (the service layer
logs it as an event change). Mounted for web and token callers alike (`MODULES`): the MCP tools
`get_budget` and `upsert_budget_item` already give tokens the same read and write reach.
"""

from __future__ import annotations

from flask import g, jsonify

from festival_planner.api._common import ID, Query, Router, config, json_body, no_content
from festival_planner.db import get_db
from festival_planner.service import budget


def register(router: Router) -> None:
    @router.route("/budget", "GET")
    def get_budget():
        Query(()).done()
        return jsonify(budget.overview(get_db(), config().currency))

    @router.route("/budget/items", "POST")
    def create_budget_item():
        return jsonify(budget.create(get_db(), g.actor, json_body())), 201

    @router.route(f"/budget/items/{ID}", "GET")
    def get_budget_item(id: int):
        return jsonify(budget.get(get_db(), id))

    @router.route(f"/budget/items/{ID}", "PATCH")
    def update_budget_item(id: int):
        return jsonify(budget.update(get_db(), g.actor, id, json_body()))

    @router.route(f"/budget/items/{ID}", "DELETE")
    def delete_budget_item(id: int):
        budget.delete(get_db(), g.actor, id)
        return no_content()
