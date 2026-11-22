"""Milestones: CRUD with activity log. A milestone's date is always derived from the event date."""

from __future__ import annotations

import builtins
import sqlite3
from typing import Any

from festival_planner.service import Actor, ValidationError
from festival_planner.service._common import (
    MAX_NAME,
    Checker,
    apply_update,
    decode,
    fetch_live,
    insert,
    page,
    soft_delete,
    transaction,
)

TABLE = "milestone"
FIELDS = ("name", "days_before_event", "notes")
MAX_DAYS = 3650


def _validate(data: Any, partial: bool) -> dict[str, Any]:
    c = Checker(data, FIELDS, () if partial else ("name", "days_before_event"))
    c.text("name", max_len=MAX_NAME, required=True)
    c.integer("days_before_event", lo=0, hi=MAX_DAYS)
    c.notes()
    return c.done()


def create(con: sqlite3.Connection, actor: Actor, data: dict) -> dict:
    values = _validate(data, partial=False)
    with transaction(con):
        return insert(con, actor, TABLE, {"notes": "", **values})


def update(con: sqlite3.Connection, actor: Actor, milestone_id: int, changes: dict) -> dict:
    values = _validate(changes, partial=True)
    with transaction(con):
        before = fetch_live(con, TABLE, milestone_id)
        return apply_update(con, actor, TABLE, before, values)


def delete(con: sqlite3.Connection, actor: Actor, milestone_id: int) -> None:
    """Soft delete. Refused while live tasks use the milestone, whose due dates depend on it."""
    with transaction(con):
        before = fetch_live(con, TABLE, milestone_id)
        count = con.execute(
            "SELECT count(*) FROM task WHERE milestone_id = ? AND deleted_at IS NULL",
            (milestone_id,),
        ).fetchone()[0]
        if count:
            raise ValidationError({"tasks": f"milestone is used by {count} task(s)"})
        soft_delete(con, actor, TABLE, before)


def get(con: sqlite3.Connection, milestone_id: int) -> dict:
    return fetch_live(con, TABLE, milestone_id)


def list(  # noqa: A001 (the interface name)
    con: sqlite3.Connection, *, limit: int = 100, offset: int = 0
) -> builtins.list[dict]:
    """Live milestones in calendar order (furthest from the event first), then id."""
    limit, offset = page(limit, offset)
    rows = con.execute(
        "SELECT * FROM milestone WHERE deleted_at IS NULL"
        " ORDER BY days_before_event DESC, id LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [decode(r) for r in rows]  # type: ignore[misc]
