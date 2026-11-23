"""/api/tasks, including POST /api/tasks/<id>/notes."""

from __future__ import annotations

from flask import g, jsonify

from festival_planner import views
from festival_planner.api._common import (
    ID,
    INT64_MAX,
    Query,
    Router,
    config,
    json_body,
    no_content,
    object_body,
    organizer_emails,
    page_args,
)
from festival_planner.api.inbox import summarize
from festival_planner.db import get_db
from festival_planner.service import emails, tasks


def _one(row: dict) -> dict:
    """A task as the API returns it: the row plus `effective_due` (own due date, else its
    milestone's date, else null), so clients can sort without re-deriving it."""
    return views.with_effective_due(get_db(), config(), [row])[0]


def register(router: Router) -> None:
    @router.route("/tasks", "GET")
    def list_tasks():
        query = Query({"workstream", "status", "owner", "due_before", "q", "limit", "offset"})
        filters = {
            "workstream_id": query.integer("workstream", lo=1, hi=INT64_MAX),
            "status": query.text("status"),
            "owner": query.text("owner"),
            "due_before": query.text("due_before"),
            "q": query.text("q"),
            **page_args(query),
        }
        query.done()
        return jsonify(
            views.with_effective_due(get_db(), config(), tasks.list(get_db(), **filters))
        )

    @router.route("/tasks", "POST")
    def create_task():
        row = tasks.create(get_db(), g.actor, json_body(), organizers=organizer_emails())
        return jsonify(_one(row)), 201

    @router.route(f"/tasks/{ID}", "GET")
    def get_task(id: int):
        row = _one(tasks.get(get_db(), id))
        row["emails"] = [summarize(e) for e in emails.list_for_task(get_db(), id)]
        return jsonify(row)

    @router.route(f"/tasks/{ID}", "PATCH")
    def update_task(id: int):
        row = tasks.update(get_db(), g.actor, id, json_body(), organizers=organizer_emails())
        return jsonify(_one(row))

    @router.route(f"/tasks/{ID}", "DELETE")
    def delete_task(id: int):
        tasks.delete(get_db(), g.actor, id)
        return no_content()

    @router.route(f"/tasks/{ID}/notes", "POST")
    def add_task_note(id: int):
        body = object_body({"note"}, required={"note"})
        activity_id = tasks.log_note(get_db(), g.actor, "task", id, body["note"])
        return jsonify(id=activity_id), 201
