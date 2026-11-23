"""/api/tasks, including POST /api/tasks/<id>/notes."""

from __future__ import annotations

from flask import g, jsonify

from festival_planner.api._common import (
    ID,
    INT64_MAX,
    Query,
    Router,
    json_body,
    no_content,
    object_body,
    organizer_emails,
    page_args,
)
from festival_planner.db import get_db
from festival_planner.service import tasks


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
        return jsonify(tasks.list(get_db(), **filters))

    @router.route("/tasks", "POST")
    def create_task():
        row = tasks.create(get_db(), g.actor, json_body(), organizers=organizer_emails())
        return jsonify(row), 201

    @router.route(f"/tasks/{ID}", "GET")
    def get_task(id: int):
        return jsonify(tasks.get(get_db(), id))

    @router.route(f"/tasks/{ID}", "PATCH")
    def update_task(id: int):
        row = tasks.update(get_db(), g.actor, id, json_body(), organizers=organizer_emails())
        return jsonify(row)

    @router.route(f"/tasks/{ID}", "DELETE")
    def delete_task(id: int):
        tasks.delete(get_db(), g.actor, id)
        return no_content()

    @router.route(f"/tasks/{ID}/notes", "POST")
    def add_task_note(id: int):
        body = object_body({"note"}, required={"note"})
        activity_id = tasks.log_note(get_db(), g.actor, "task", id, body["note"])
        return jsonify(id=activity_id), 201
