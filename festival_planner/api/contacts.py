"""/api/contacts."""

from __future__ import annotations

from flask import g, jsonify

from festival_planner.api._common import (
    ID,
    INT64_MAX,
    Query,
    Router,
    json_body,
    no_content,
    page_args,
)
from festival_planner.db import get_db
from festival_planner.service import contacts


def register(router: Router) -> None:
    @router.route("/contacts", "GET")
    def list_contacts():
        query = Query({"workstream", "q", "limit", "offset"})
        filters = {
            "workstream_id": query.integer("workstream", lo=1, hi=INT64_MAX),
            "q": query.text("q"),
            **page_args(query),
        }
        query.done()
        return jsonify(contacts.list(get_db(), **filters))

    @router.route("/contacts", "POST")
    def create_contact():
        return jsonify(contacts.create(get_db(), g.actor, json_body())), 201

    @router.route(f"/contacts/{ID}", "GET")
    def get_contact(id: int):
        return jsonify(contacts.get(get_db(), id))

    @router.route(f"/contacts/{ID}", "PATCH")
    def update_contact(id: int):
        return jsonify(contacts.update(get_db(), g.actor, id, json_body()))

    @router.route(f"/contacts/{ID}", "DELETE")
    def delete_contact(id: int):
        contacts.delete(get_db(), g.actor, id)
        return no_content()
