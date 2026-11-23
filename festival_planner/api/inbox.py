"""/api/inbox and /api/emails: triage of captured email (design §6.4).

Email text is untrusted. These handlers only pass it through as JSON strings; the list never
carries a full body (a preview of the first 200 characters instead), and the full body is one
request away at GET /api/emails/<id>.
"""

from __future__ import annotations

from flask import g, jsonify

from festival_planner.api._common import ID, INT64_MAX, Router, object_body
from festival_planner.db import get_db
from festival_planner.service import ValidationError, emails

PREVIEW_CHARS = 200


def summarize(row: dict) -> dict:
    """An email without its body: `preview` is the first 200 characters of `body_text` (empty
    when the body is gone), and `preview_truncated` says whether there is more."""
    out = {k: v for k, v in row.items() if k != "body_text"}
    body = row.get("body_text") or ""
    out["preview"] = body[:PREVIEW_CHARS]
    out["preview_truncated"] = len(body) > PREVIEW_CHARS
    return out


def _id(data: dict, name: str) -> int:
    """A positive int64 from the body. The service validates meaning (does it exist?); this only
    guarantees the value fits SQLite, so no client input can reach the driver as a 500."""
    value = data[name]
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= INT64_MAX:
        raise ValidationError({name: "must be a positive integer id"})
    return value


def register(router: Router) -> None:
    @router.route("/inbox", "GET")
    def list_inbox():
        return jsonify([summarize(e) for e in emails.list_inbox(get_db())])

    @router.route(f"/emails/{ID}", "GET")
    def get_email(id: int):
        return jsonify(emails.get(get_db(), id))

    @router.route(f"/inbox/{ID}/file", "POST")
    def file_email(id: int):
        data = object_body({"task_id"}, required={"task_id"})
        row = emails.file(get_db(), g.actor, id, _id(data, "task_id"))
        return jsonify(summarize(row))

    @router.route(f"/inbox/{ID}/new-task", "POST")
    def new_task_from_email(id: int):
        data = object_body({"workstream_id", "title"}, required={"workstream_id"})
        row = emails.file_new_task(
            get_db(), g.actor, id, _id(data, "workstream_id"), data.get("title")
        )
        return jsonify(summarize(row))

    @router.route(f"/inbox/{ID}/dismiss", "POST")
    def dismiss_email(id: int):
        return jsonify(summarize(emails.dismiss(get_db(), g.actor, id)))
