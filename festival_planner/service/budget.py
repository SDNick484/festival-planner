"""The budget: revenue and cost items, and the summary against the event's net goal (Task 22).

Money is integer cents (hundredths of the configured currency unit), never a float. An item's
`state` is estimated, pledged or received for both kinds; for a cost the UI says estimated,
committed and paid. The net goal lives on the event row (`event.net_goal_cents`) and is changed
through `service.event.update`, so the activity log records it like any other event change.
"""

from __future__ import annotations

import builtins
import sqlite3
from typing import Any

from festival_planner.service import Actor, ValidationError
from festival_planner.service._common import (
    MAX_LIST_LIMIT,
    MAX_NAME,
    Checker,
    apply_update,
    decode,
    fetch_live,
    insert,
    is_live,
    page,
    soft_delete,
    transaction,
)

TABLE = "budget_item"
KINDS = ("revenue", "cost")
STATES = ("estimated", "pledged", "received")
FIELDS = ("workstream_id", "label", "kind", "amount_cents", "state", "notes")
DEFAULTS: dict[str, Any] = {"workstream_id": None, "state": "estimated", "notes": ""}
# 10 billion currency units: far beyond any festival, and small enough that sums of any realistic
# number of items stay exactly representable (JSON clients read integers as doubles).
MAX_CENTS = 10**12


def _validate(data: Any, partial: bool) -> dict[str, Any]:
    c = Checker(data, FIELDS, () if partial else ("label", "kind", "amount_cents"))
    c.integer("workstream_id", lo=1, hi=2**63 - 1, nullable=True, label="a workstream id")
    c.text("label", max_len=MAX_NAME, required=True)
    c.choice("kind", KINDS)
    c.integer(
        "amount_cents", lo=0, hi=MAX_CENTS, label="a whole number of cents (integer, not a float)"
    )
    c.choice("state", STATES)
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


def update(con: sqlite3.Connection, actor: Actor, item_id: int, changes: dict) -> dict:
    values = _validate(changes, partial=True)
    with transaction(con):
        before = fetch_live(con, TABLE, item_id)
        _check_refs(con, values)
        return apply_update(con, actor, TABLE, before, values)


def delete(con: sqlite3.Connection, actor: Actor, item_id: int) -> None:
    with transaction(con):
        soft_delete(con, actor, TABLE, fetch_live(con, TABLE, item_id))


def get(con: sqlite3.Connection, item_id: int) -> dict:
    return fetch_live(con, TABLE, item_id)


def list(  # noqa: A001 (the interface name)
    con: sqlite3.Connection,
    *,
    workstream_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> builtins.list[dict]:
    """Live items in creation order, optionally only those of one workstream."""
    limit, offset = page(limit, offset)
    sql = "SELECT * FROM budget_item WHERE deleted_at IS NULL"
    params: builtins.list[Any] = []
    if workstream_id is not None:
        sql += " AND workstream_id = ?"
        params.append(workstream_id)
    rows = con.execute(sql + " ORDER BY id LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
    return [decode(r) for r in rows]  # type: ignore[misc]


def count(con: sqlite3.Connection) -> int:
    return con.execute("SELECT count(*) FROM budget_item WHERE deleted_at IS NULL").fetchone()[0]


def summary(con: sqlite3.Connection) -> dict:
    """Totals per kind and state, the projected and actual net, and the goal.

    * `revenue` and `cost`: cents per state (all three states always present).
    * `net_projected`: every revenue item minus every cost item, whatever its state.
    * `net_actual`: revenue received minus cost paid (state `received`).
    * `goal`: `event.net_goal_cents`, or None when no goal is set.
    * `pct_of_goal`: `net_projected` as a percentage of the goal, one decimal place (it can be
      negative or above 100); None when there is no goal or the goal is 0.
    """
    totals = {kind: dict.fromkeys(STATES, 0) for kind in KINDS}
    rows = con.execute(
        "SELECT kind, state, amount_cents FROM budget_item WHERE deleted_at IS NULL"
    ).fetchall()
    for kind, state, cents in rows:  # summed here, not with SQL SUM(): no int64 overflow
        totals[kind][state] += cents
    revenue, cost = totals["revenue"], totals["cost"]
    net_projected = sum(revenue.values()) - sum(cost.values())
    row = con.execute("SELECT net_goal_cents FROM event WHERE id = 1").fetchone()
    goal = row[0] if row else None
    return {
        "revenue": revenue,
        "cost": cost,
        "net_projected": net_projected,
        "net_actual": revenue["received"] - cost["received"],
        "goal": goal,
        "pct_of_goal": round(net_projected * 100 / goal, 1) if goal else None,
    }


def overview(con: sqlite3.Connection, currency: str) -> dict:
    """What the Budget screen and the `get_budget` tool show: currency, summary and every item."""
    items: builtins.list[dict] = []
    while True:
        batch = list(con, limit=MAX_LIST_LIMIT, offset=len(items))
        items += batch
        if len(batch) < MAX_LIST_LIMIT:
            break
    return {"currency": currency, "summary": summary(con), "items": items}
