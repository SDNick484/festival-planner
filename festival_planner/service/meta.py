"""The `meta` key/value table: small bookkeeping values that are not part of the model.

Examples: `last_email_poll` (shown by /healthz) and the worker's `daily_job:<name>` run dates.
These are not user-visible changes, so they write no activity row. They still live in the service
layer, which stays the only place that executes write SQL (Global Constraints).
"""

from __future__ import annotations

import sqlite3

from festival_planner.service._common import transaction


def get(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def set(con: sqlite3.Connection, key: str, value: str | None) -> None:  # noqa: A001
    with transaction(con):
        con.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
