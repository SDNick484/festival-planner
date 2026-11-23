"""The append-only activity log. Rows are written by the other service modules, inside the same
transaction as the change they describe."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from festival_planner.service import Actor, ValidationError

MAX_LIMIT = 500


def _ts(moment: datetime) -> str:
    """UTC ISO-8601 with milliseconds, the same shape as the column default. Naive means UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def record(
    con: sqlite3.Connection,
    actor: Actor,
    entity_type: str,
    entity_id: int,
    action: str,
    diff: dict | None = None,
    note: str | None = None,
) -> int:
    """Append one activity row and return its id. Does not commit: the caller's transaction does.

    `actor` must come from the authenticated identity (Global Constraints), never request data.
    """
    if not isinstance(actor, Actor) or not actor.source.strip() or not actor.actor.strip():
        raise ValueError("activity needs an authenticated source and actor")
    cur = con.execute(
        "INSERT INTO activity (at, source, actor, entity_type, entity_id, action, diff, note)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _ts(datetime.now(UTC)),
            actor.source,
            actor.actor,
            entity_type,
            entity_id,
            action,
            None if diff is None else json.dumps(diff, ensure_ascii=False, separators=(",", ":")),
            note,
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


def list_activity(
    con: sqlite3.Connection,
    *,
    since: datetime | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    source: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Activity rows, newest first (append order), with `diff` decoded. `since` is inclusive."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValidationError({"limit": f"must be an integer from 1 to {MAX_LIMIT}"})
    where: list[str] = []
    params: list[Any] = []
    if since is not None:
        where.append("at >= ?")
        params.append(_ts(since))
    if entity_type is not None:
        where.append("entity_type = ?")
        params.append(entity_type)
    if entity_id is not None:
        where.append("entity_id = ?")
        params.append(entity_id)
    if source is not None:
        where.append("source = ?")
        params.append(source)
    sql = "SELECT * FROM activity"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    rows = con.execute(sql, [*params, limit]).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["diff"] = None if item["diff"] is None else json.loads(item["diff"])
        out.append(item)
    return out
