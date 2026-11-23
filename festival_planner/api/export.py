"""GET /api/export/<kind>.csv: organizer CSV downloads (Task 25).

Web auth only (`WEB_ONLY_MODULES`), so machine tokens get a 404 on `/mcp/api/export/*`.

* tasks, contacts: the live rows (soft-deleted rows are excluded), read through the service list
  functions one page at a time.
* activity: the whole append-only log, oldest first. A row's `diff` is exported as the names of
  the fields it changed, never their values, because values can carry email text (a captured
  email's subject, a task titled from an email subject, a forward note). Rows about email items
  are left out, and a `file_email` row's note (the forward note) is blanked.
* budget: Task 22 adds it. Until then it is an unknown kind and answers 404.

CSV injection: a text cell that starts with `= + - @`, a tab or a carriage return gets a single
quote in front, so a spreadsheet shows it as text rather than running it as a formula.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Iterator
from typing import Any

from flask import Response, abort

from festival_planner import views
from festival_planner.api._common import Router, config
from festival_planner.db import get_db
from festival_planner.service import contacts, tasks

PAGE = 500  # the service lists' own maximum page size
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
EMAIL_ENTITY = "email_item"

TASK_COLUMNS = (
    "id",
    "workstream_id",
    "title",
    "owner",
    "status",
    "due_date",
    "effective_due",
    "milestone_id",
    "waiting_on",
    "blocked_reason",
    "notes",
    "links",
    "done_at",
    "created_at",
    "updated_at",
)
CONTACT_COLUMNS = (
    "id",
    "workstream_id",
    "name",
    "organization",
    "role",
    "emails",
    "phone",
    "notes",
    "created_at",
    "updated_at",
)
ACTIVITY_COLUMNS = (
    "id",
    "at",
    "source",
    "actor",
    "entity_type",
    "entity_id",
    "action",
    "changed_fields",
    "note",
)


def escape_cell(value: Any) -> Any:
    """Prefix a text cell that a spreadsheet would read as a formula. Other values pass through."""
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def _pages(list_fn: Callable[..., list[dict]], con) -> Iterator[list[dict]]:
    offset = 0
    while True:
        batch = list_fn(con, limit=PAGE, offset=offset)
        yield batch
        if len(batch) < PAGE:
            return
        offset += PAGE


def _task_rows() -> Iterator[dict]:
    con, cfg = get_db(), config()
    for batch in _pages(tasks.list, con):
        for row in views.with_effective_due(con, cfg, batch):
            row["links"] = json.dumps(row["links"], ensure_ascii=False)
            yield row


def _contact_rows() -> Iterator[dict]:
    for batch in _pages(contacts.list, get_db()):
        for row in batch:
            row["emails"] = ", ".join(row["emails"])
            yield row


def _activity_rows() -> Iterator[dict]:
    # Keyset paging on id: the activity log has no offset in the service layer.
    con = get_db()
    last = 0
    while True:
        rows = [
            dict(r)
            for r in con.execute(
                "SELECT * FROM activity WHERE id > ? ORDER BY id LIMIT ?", (last, PAGE)
            ).fetchall()
        ]
        for row in rows:
            last = row["id"]
            if row["entity_type"] == EMAIL_ENTITY:
                continue
            diff = json.loads(row["diff"]) if row["diff"] else {}
            out = {k: row[k] for k in ACTIVITY_COLUMNS if k != "changed_fields"}
            out["changed_fields"] = ", ".join(diff)
            if row["action"] == "file_email":
                out["note"] = None  # the forward note is email text
            yield out
        if len(rows) < PAGE:
            return


SOURCES: dict[str, tuple[tuple[str, ...], Callable[[], Iterator[dict]]]] = {
    "tasks": (TASK_COLUMNS, _task_rows),
    "contacts": (CONTACT_COLUMNS, _contact_rows),
    "activity": (ACTIVITY_COLUMNS, _activity_rows),
}


def csv_text(columns: tuple[str, ...], rows: Iterator[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(escape_cell(row[c]) for c in columns)
    return buf.getvalue()


def register(router: Router) -> None:
    @router.route("/export/<kind>.csv", "GET")
    def export_csv(kind: str):
        if kind not in SOURCES:
            abort(404)
        columns, rows = SOURCES[kind]
        resp = Response(csv_text(columns, rows()), content_type="text/csv; charset=utf-8")
        resp.headers["Content-Disposition"] = f'attachment; filename="{kind}.csv"'
        resp.headers["Cache-Control"] = "no-store"
        return resp
