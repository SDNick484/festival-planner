"""Tasks: CRUD with status rules, plus `log_note` for tasks and workstreams."""

from __future__ import annotations

import builtins
import sqlite3
from collections.abc import Collection
from datetime import date
from typing import Any, Literal

from festival_planner.service import Actor, ValidationError, activity
from festival_planner.service._common import (
    MAX_NAME,
    MAX_NOTES,
    Checker,
    apply_update,
    decode,
    fetch_live,
    insert,
    is_live,
    like,
    now_iso,
    page,
    parse_date,
    soft_delete,
    transaction,
)

TABLE = "task"
STATUSES = ("todo", "doing", "waiting", "blocked", "done")
FIELDS = (
    "workstream_id",
    "title",
    "owner",
    "status",
    "due_date",
    "milestone_id",
    "waiting_on",
    "blocked_reason",
    "notes",
    "links",
)
DEFAULTS: dict[str, Any] = {
    "owner": None,
    "status": "todo",
    "due_date": None,
    "milestone_id": None,
    "waiting_on": None,
    "blocked_reason": None,
    "notes": "",
    "links": [],
    "done_at": None,
}
MAX_ID = 2**63 - 1


def _validate(
    data: Any, partial: bool, organizers: Collection[str] | None = None
) -> dict[str, Any]:
    """Clean `data` field by field; cross-field rules need the current row (see `_resolve`)."""
    c = Checker(data, FIELDS, () if partial else ("workstream_id", "title"))
    c.integer("workstream_id", lo=1, hi=MAX_ID, label="a workstream id")
    c.text("title", max_len=MAX_NAME, required=True)
    c.owner("owner", organizers)
    c.choice("status", STATUSES)
    c.iso_date("due_date", nullable=True)
    c.integer("milestone_id", lo=1, hi=MAX_ID, nullable=True, label="a milestone id")
    c.text("waiting_on", max_len=MAX_NAME, nullable=True)
    c.text("blocked_reason", max_len=500, nullable=True)
    c.notes()
    c.links()
    return c.done()


def _resolve(con: sqlite3.Connection, before: dict[str, Any], values: dict[str, Any]):
    """Apply the status rules and reference checks to the merged row. Returns the new fields."""
    after = {**before, **values}
    errors: dict[str, str] = {}
    if "workstream_id" in values and not is_live(con, "workstream", values["workstream_id"]):
        errors["workstream_id"] = "no such workstream"
    if values.get("milestone_id") is not None and not is_live(
        con, "milestone", values["milestone_id"]
    ):
        errors["milestone_id"] = "no such milestone"
    if after["status"] == "blocked":
        if not after["blocked_reason"]:
            errors["blocked_reason"] = "required when status is blocked"
    else:
        after["blocked_reason"] = None  # leaving (or not in) blocked clears the reason
    if after["status"] != "done":
        after["done_at"] = None
    elif before["status"] != "done" or not before["done_at"]:
        after["done_at"] = now_iso()
    if errors:
        raise ValidationError(errors)
    return {k: after[k] for k in (*FIELDS, "done_at")}


def create(
    con: sqlite3.Connection,
    actor: Actor,
    data: dict,
    *,
    organizers: Collection[str] | None = None,
) -> dict:
    """Create a task. `organizers` are the configured organizer emails (owner must be one)."""
    values = _validate(data, partial=False, organizers=organizers)
    with transaction(con):
        before = {**DEFAULTS, "workstream_id": None, "title": None, "status": None}
        row = _resolve(con, before, {**DEFAULTS, **values})
        return insert(con, actor, TABLE, row)


def update(
    con: sqlite3.Connection,
    actor: Actor,
    task_id: int,
    changes: dict,
    *,
    organizers: Collection[str] | None = None,
) -> dict:
    """Partial update. Unchanged values are not written and an empty diff logs nothing."""
    values = _validate(changes, partial=True, organizers=organizers)
    with transaction(con):
        before = fetch_live(con, TABLE, task_id)
        return apply_update(con, actor, TABLE, before, _resolve(con, before, values))


def delete(con: sqlite3.Connection, actor: Actor, task_id: int) -> None:
    """Soft delete: the row stays (so activity never points at nothing) but is hidden."""
    with transaction(con):
        soft_delete(con, actor, TABLE, fetch_live(con, TABLE, task_id))


def get(con: sqlite3.Connection, task_id: int) -> dict:
    return fetch_live(con, TABLE, task_id)


def list(  # noqa: A001 (the interface name)
    con: sqlite3.Connection,
    *,
    workstream_id: int | None = None,
    status: str | None = None,
    owner: str | None = None,
    due_before: date | None = None,
    q: str | None = None,
    include_done: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> builtins.list[dict]:
    """Live tasks, by explicit due date (undated last), then id.

    `due_before` is exclusive and looks at `due_date` only; milestone-derived dates are the
    views layer's job. `q` is a case-insensitive substring match on title and notes.
    """
    limit, offset = page(limit, offset)
    for key, value in (("owner", owner), ("q", q)):
        if value is not None and not isinstance(value, str):
            raise ValidationError({key: "must be a string"})
    where = ["deleted_at IS NULL"]
    params: builtins.list[Any] = []
    if workstream_id is not None:
        where.append("workstream_id = ?")
        params.append(workstream_id)
    if status is not None:
        if status not in STATUSES:
            raise ValidationError({"status": f"must be one of: {', '.join(STATUSES)}"})
        where.append("status = ?")
        params.append(status)
    if owner is not None:
        where.append("owner = ?")
        params.append(owner.strip().lower())
    if due_before is not None:
        try:
            cutoff = parse_date(due_before)
        except ValueError:
            raise ValidationError({"due_before": "must be a date"}) from None
        where.append("due_date < ?")
        params.append(cutoff.isoformat())
    if q:
        where.append("(title LIKE ? ESCAPE '\\' OR notes LIKE ? ESCAPE '\\')")
        params += [like(q), like(q)]
    if not include_done:
        where.append("status != 'done'")
    rows = con.execute(
        "SELECT * FROM task WHERE "  # noqa: S608 (fixed clauses, values are parameters)
        + " AND ".join(where)
        + " ORDER BY due_date IS NULL, due_date, id LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [decode(r) for r in rows]  # type: ignore[misc]


def log_note(
    con: sqlite3.Connection,
    actor: Actor,
    entity_type: Literal["task", "workstream"],
    entity_id: int,
    note: str,
) -> int:
    """Log a free-text note ("called vendor, confirmed") on a live task or workstream."""
    errors = {}
    if entity_type not in ("task", "workstream"):
        errors["entity_type"] = "must be task or workstream"
    if not isinstance(note, str) or not note.strip():
        errors["note"] = "must not be empty"
    elif len(note.strip()) > MAX_NOTES:
        errors["note"] = f"must be at most {MAX_NOTES} characters"
    if errors:
        raise ValidationError(errors)
    with transaction(con):
        fetch_live(con, entity_type, entity_id)
        return activity.record(con, actor, entity_type, entity_id, "note", note=note.strip())
