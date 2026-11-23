"""The single event row: read and update. It is created by the first-run bootstrap."""

from __future__ import annotations

import sqlite3
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from festival_planner.service import Actor, NotFound, ValidationError, activity
from festival_planner.service._common import (
    MAX_NAME,
    Checker,
    apply_update,
    decode,
    now_iso,
    transaction,
)

TABLE = "event"
FIELDS = ("name", "event_date", "timezone", "goal_note")


def _validate(data: Any, partial: bool) -> dict[str, Any]:
    c = Checker(data, FIELDS, () if partial else FIELDS[:3])
    c.text("name", max_len=MAX_NAME, required=True)
    c.iso_date("event_date")
    c.text("timezone", max_len=64, required=True)
    tz = c.out.get("timezone")
    if tz is not None:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            c.fail("timezone", "must be an IANA time zone such as Europe/Berlin")
    c.notes("goal_note")
    return c.done()


def _fetch(con: sqlite3.Connection) -> dict:
    row = decode(con.execute("SELECT * FROM event WHERE id = 1").fetchone())
    if row is None:
        raise NotFound("the event has not been set up yet")
    return row


def get(con: sqlite3.Connection) -> dict:
    return _fetch(con)


def create(con: sqlite3.Connection, actor: Actor, data: dict) -> dict:
    """Create the one event row (id 1). Refused when it already exists. Used by the bootstrap."""
    values = _validate(data, partial=False)
    with transaction(con):
        if con.execute("SELECT 1 FROM event WHERE id = 1").fetchone():
            raise ValidationError({"event": "already exists"})
        ts = now_iso()
        con.execute(
            "INSERT INTO event (id, name, event_date, timezone, goal_note, created_at, updated_at)"
            " VALUES (1, ?, ?, ?, ?, ?, ?)",
            (
                values["name"],
                values["event_date"],
                values["timezone"],
                values.get("goal_note", ""),
                ts,
                ts,
            ),
        )
        # The event table has no deleted_at column, so this is written here rather than through
        # the generic insert(), which reads back through a live-row filter.
        diff = {k: [None, v] for k, v in values.items() if v not in (None, "")}
        activity.record(con, actor, TABLE, 1, "create", diff)
        return _fetch(con)


def update(con: sqlite3.Connection, actor: Actor, changes: dict) -> dict:
    values = _validate(changes, partial=True)
    with transaction(con):
        return apply_update(con, actor, TABLE, _fetch(con), values)
