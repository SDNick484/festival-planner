"""Workstreams: CRUD with activity log."""

from __future__ import annotations

import builtins
import re
import sqlite3
from collections.abc import Collection
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

TABLE = "workstream"
FIELDS = ("name", "owner", "color", "sort_order", "archived")
DEFAULTS: dict[str, Any] = {"owner": None, "color": None, "sort_order": 0, "archived": False}
MAX_SORT = 1_000_000
_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")


def _validate(
    data: Any, partial: bool, organizers: Collection[str] | None = None
) -> dict[str, Any]:
    c = Checker(data, FIELDS, () if partial else ("name",))
    c.text("name", max_len=MAX_NAME, required=True)
    c.owner("owner", organizers)
    c.text("color", max_len=7, nullable=True)
    color = c.out.get("color")
    if color is not None and not _COLOR_RE.fullmatch(color):
        c.fail("color", "must be a hex color such as #3366cc")
    c.integer("sort_order", lo=-MAX_SORT, hi=MAX_SORT)
    c.boolean("archived")
    values = c.done()
    if values.get("color"):
        values["color"] = values["color"].lower()
    return values


def create(
    con: sqlite3.Connection,
    actor: Actor,
    data: dict,
    *,
    organizers: Collection[str] | None = None,
) -> dict:
    """Create a workstream. `organizers` are the configured organizer emails (owner must be one)."""
    values = _validate(data, partial=False, organizers=organizers)
    with transaction(con):
        return insert(con, actor, TABLE, {**DEFAULTS, **values})


def update(
    con: sqlite3.Connection,
    actor: Actor,
    workstream_id: int,
    changes: dict,
    *,
    organizers: Collection[str] | None = None,
) -> dict:
    values = _validate(changes, partial=True, organizers=organizers)
    with transaction(con):
        before = fetch_live(con, TABLE, workstream_id)
        return apply_update(con, actor, TABLE, before, values)


def delete(con: sqlite3.Connection, actor: Actor, workstream_id: int) -> None:
    """Soft delete. Refused while live tasks remain: move or delete them first (or archive)."""
    with transaction(con):
        before = fetch_live(con, TABLE, workstream_id)
        count = con.execute(
            "SELECT count(*) FROM task WHERE workstream_id = ? AND deleted_at IS NULL",
            (workstream_id,),
        ).fetchone()[0]
        if count:
            raise ValidationError({"tasks": f"workstream still has {count} task(s)"})
        soft_delete(con, actor, TABLE, before)


def get(con: sqlite3.Connection, workstream_id: int) -> dict:
    return fetch_live(con, TABLE, workstream_id)


def list(  # noqa: A001 (the interface name)
    con: sqlite3.Connection,
    *,
    include_archived: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> builtins.list[dict]:
    """Live workstreams by sort_order, then id."""
    limit, offset = page(limit, offset)
    sql = "SELECT * FROM workstream WHERE deleted_at IS NULL"
    if not include_archived:
        sql += " AND archived = 0"
    rows = con.execute(
        sql + " ORDER BY sort_order, id LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    return [decode(r) for r in rows]  # type: ignore[misc]
