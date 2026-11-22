"""The single event row: read and update. It is created by the first-run bootstrap."""

from __future__ import annotations

import sqlite3
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from festival_planner.service import Actor, NotFound
from festival_planner.service._common import (
    MAX_NAME,
    Checker,
    apply_update,
    decode,
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


def update(con: sqlite3.Connection, actor: Actor, changes: dict) -> dict:
    values = _validate(changes, partial=True)
    with transaction(con):
        return apply_update(con, actor, TABLE, _fetch(con), values)
