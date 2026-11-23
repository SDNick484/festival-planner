"""Contacts: CRUD with activity log. Contacts are for organizing only, never exposed outside."""

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
    is_live,
    like,
    page,
    soft_delete,
    transaction,
)

TABLE = "contact"
FIELDS = ("workstream_id", "name", "organization", "role", "emails", "phone", "notes")
DEFAULTS: dict[str, Any] = {
    "workstream_id": None,
    "organization": None,
    "role": None,
    "emails": [],
    "phone": None,
    "notes": "",
}


def _validate(data: Any, partial: bool) -> dict[str, Any]:
    c = Checker(data, FIELDS, () if partial else ("name",))
    c.integer("workstream_id", lo=1, hi=2**63 - 1, nullable=True, label="a workstream id")
    c.text("name", max_len=MAX_NAME, required=True)
    c.text("organization", max_len=MAX_NAME, nullable=True)
    c.text("role", max_len=MAX_NAME, nullable=True)
    c.emails()
    c.text("phone", max_len=50, nullable=True)
    c.notes()
    return c.done()


def _check_refs(con: sqlite3.Connection, values: dict[str, Any]) -> None:
    ws = values.get("workstream_id")
    if ws is not None and not is_live(con, "workstream", ws):
        raise ValidationError({"workstream_id": "no such workstream"})


def create(con: sqlite3.Connection, actor: Actor, data: dict) -> dict:
    values = _validate(data, partial=False)
    with transaction(con):
        _check_refs(con, values)
        return insert(con, actor, TABLE, {**DEFAULTS, **values})


def update(con: sqlite3.Connection, actor: Actor, contact_id: int, changes: dict) -> dict:
    values = _validate(changes, partial=True)
    with transaction(con):
        before = fetch_live(con, TABLE, contact_id)
        _check_refs(con, values)
        return apply_update(con, actor, TABLE, before, values)


def delete(con: sqlite3.Connection, actor: Actor, contact_id: int) -> None:
    with transaction(con):
        soft_delete(con, actor, TABLE, fetch_live(con, TABLE, contact_id))


def get(con: sqlite3.Connection, contact_id: int) -> dict:
    return fetch_live(con, TABLE, contact_id)


def list(  # noqa: A001 (the interface name)
    con: sqlite3.Connection,
    *,
    workstream_id: int | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> builtins.list[dict]:
    """Live contacts by name. `q` matches name, organization or role (case-insensitive)."""
    limit, offset = page(limit, offset)
    if q is not None and not isinstance(q, str):
        raise ValidationError({"q": "must be a string"})
    where = ["deleted_at IS NULL"]
    params: builtins.list[Any] = []
    if workstream_id is not None:
        where.append("workstream_id = ?")
        params.append(workstream_id)
    if q:
        where.append(
            "(name LIKE ? ESCAPE '\\' OR organization LIKE ? ESCAPE '\\'"
            " OR role LIKE ? ESCAPE '\\')"
        )
        params += [like(q)] * 3
    rows = con.execute(
        "SELECT * FROM contact WHERE "  # noqa: S608 (fixed clauses, values are parameters)
        + " AND ".join(where)
        + " ORDER BY name COLLATE NOCASE, id LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [decode(r) for r in rows]  # type: ignore[misc]
