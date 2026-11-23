"""Read-only derived views: effective due dates, attention lists, progress, countdown, digest.

Nothing here writes. Rows come from the service layer's read functions, and the date math is done
in Python after one query per list. "Today" is `clock.today(cfg)` (event timezone). Dates in the
returned dicts are ISO strings, so every result is JSON-serializable as is.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from festival_planner import clock
from festival_planner.config import Config
from festival_planner.service import (
    NotFound,
    budget,
    emails,
    event,
    milestones,
    tasks,
    workstreams,
)
from festival_planner.service._common import MAX_LIST_LIMIT

WEEK_DAYS = 7
REASON_ORDER = ("overdue", "blocked", "waiting")


# --- inputs --------------------------------------------------------------------------------


def _all(fetch: Callable[..., list[dict]], con: sqlite3.Connection, **kwargs: Any) -> list[dict]:
    """Every row a paged service `list` function has, not just the first page."""
    out: list[dict] = []
    while True:
        batch = fetch(con, limit=MAX_LIST_LIMIT, offset=len(out), **kwargs)
        out += batch
        if len(batch) < MAX_LIST_LIMIT:
            return out


def _event_row(con: sqlite3.Connection, cfg: Config) -> dict:
    """The event row (it is what "move the event date" edits); the config's values before setup."""
    try:
        return event.get(con)
    except NotFound:
        return {
            "name": cfg.event_name,
            "event_date": cfg.event_date.isoformat(),
            "timezone": cfg.timezone.key,
            "goal_note": "",
            "net_goal_cents": None,
        }


def _event_date(con: sqlite3.Connection, cfg: Config) -> date:
    return date.fromisoformat(_event_row(con, cfg)["event_date"])


def _milestone_days(con: sqlite3.Connection) -> dict[int, int]:
    return {m["id"]: m["days_before_event"] for m in _all(milestones.list, con)}


def _as_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


# --- effective due date --------------------------------------------------------------------


def effective_due(task: dict, event_date: date, milestones: dict[int, int]) -> date | None:
    """The task's own due date, else its milestone's date (event date minus days before), else None.

    `milestones` maps milestone id to days before the event. A milestone that is not in the map
    (unknown or deleted) gives None. Because the milestone date is derived on every call, moving
    the event date moves every milestone-only task with it.
    """
    if task.get("due_date"):
        return _as_date(task["due_date"])
    days = milestones.get(task["milestone_id"]) if task.get("milestone_id") is not None else None
    if days is None:
        return None
    return event_date - timedelta(days=days)


def with_effective_due(con: sqlite3.Connection, cfg: Config, rows: list[dict]) -> list[dict]:
    """The task dicts with `effective_due` (an ISO date or None) added, for the API and the UI.
    The event date and milestones are read once for the whole list."""
    ev_date = _event_date(con, cfg)
    days = _milestone_days(con)
    return [_item(t, effective_due(t, ev_date, days)) for t in rows]


def _open_with_due(con: sqlite3.Connection, cfg: Config) -> list[tuple[dict, date | None]]:
    ev_date = _event_date(con, cfg)
    days = _milestone_days(con)
    return [(t, effective_due(t, ev_date, days)) for t in _all(tasks.list, con, include_done=False)]


def _item(task: dict, due: date | None, **extra: Any) -> dict:
    return {**task, "effective_due": due.isoformat() if due else None, **extra}


# --- activity-derived ages -----------------------------------------------------------------


def _status_since(con: sqlite3.Connection, status: str) -> dict[int, datetime]:
    """Per task id, when the most recent activity set `status` (UTC). One read-only query."""
    rows = con.execute(
        "SELECT entity_id, max(at) FROM activity"
        " WHERE entity_type = 'task' AND json_extract(diff, '$.status[1]') = ?"
        " GROUP BY entity_id",
        (status,),
    ).fetchall()
    return {r[0]: _parse_ts(r[1]) for r in rows}


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _age_days(since: datetime, today: date, cfg: Config) -> int:
    """Whole calendar days from `since` to today, both as dates in the event timezone."""
    return max(0, (today - since.astimezone(cfg.timezone).date()).days)


def _since(task: dict, status_since: dict[int, datetime]) -> datetime:
    """When the task entered its status; falls back to its last change if no activity says."""
    return status_since.get(task["id"]) or _parse_ts(task["updated_at"])


# --- lists ---------------------------------------------------------------------------------


def overdue(con: sqlite3.Connection, cfg: Config) -> list[dict]:
    """Not-done tasks whose effective due date is before today, most overdue first.

    Each item is the task plus `effective_due` and `age_days` (days past due).
    """
    today = clock.today(cfg)
    items = [
        _item(t, due, age_days=(today - due).days)
        for t, due in _open_with_due(con, cfg)
        if due is not None and due < today
    ]
    return sorted(items, key=lambda i: (-i["age_days"], i["id"]))


def due_this_week(con: sqlite3.Connection, cfg: Config) -> list[dict]:
    """Not-done tasks due in the next 7 days, today inclusive, soonest first."""
    today = clock.today(cfg)
    last = today + timedelta(days=WEEK_DAYS - 1)
    items = [
        _item(t, due)
        for t, due in _open_with_due(con, cfg)
        if due is not None and today <= due <= last
    ]
    return sorted(items, key=lambda i: (i["effective_due"], i["id"]))


def needs_attention(con: sqlite3.Connection, cfg: Config) -> list[dict]:
    """Overdue, then blocked, then waiting longer than `waiting_stale_days`.

    Each task appears once, under the first reason that applies. Items carry `reason` and
    `age_days`: days past due for overdue, days since the status was set for blocked and waiting.
    Each group is sorted by age, oldest first. "Waiting since" is the latest activity that set
    status to waiting.
    """
    today = clock.today(cfg)
    blocked_since = _status_since(con, "blocked")
    waiting_since = _status_since(con, "waiting")
    groups: dict[str, list[dict]] = {reason: [] for reason in REASON_ORDER}
    for task, due in _open_with_due(con, cfg):
        if due is not None and due < today:
            groups["overdue"].append(
                _item(task, due, reason="overdue", age_days=(today - due).days)
            )
        elif task["status"] == "blocked":
            age = _age_days(_since(task, blocked_since), today, cfg)
            groups["blocked"].append(_item(task, due, reason="blocked", age_days=age))
        elif task["status"] == "waiting":
            age = _age_days(_since(task, waiting_since), today, cfg)
            if age > cfg.waiting_stale_days:
                groups["waiting"].append(_item(task, due, reason="waiting", age_days=age))
    return [
        item
        for reason in REASON_ORDER
        for item in sorted(groups[reason], key=lambda i: (-i["age_days"], i["id"]))
    ]


def workstream_progress(con: sqlite3.Connection) -> list[dict]:
    """Per active workstream: done and total live tasks and a whole-number percentage.

    A workstream with no tasks is 0%, not an error. Archived workstreams are left out.
    """
    counts: dict[int, list[int]] = {}
    for t in _all(tasks.list, con):
        done_total = counts.setdefault(t["workstream_id"], [0, 0])
        done_total[0] += t["status"] == "done"
        done_total[1] += 1
    out = []
    for ws in _all(workstreams.list, con, include_archived=False):
        done, total = counts.get(ws["id"], (0, 0))
        pct = (done * 100 + total // 2) // total if total else 0
        out.append({"id": ws["id"], "name": ws["name"], "done": done, "total": total, "pct": pct})
    return out


# --- countdown and digest ------------------------------------------------------------------


def countdown(cfg_or_event_date: Config | date, today: date) -> dict:
    """Days until the event and a label: "N days", "Tomorrow", "Today!" or "Event day passed".

    After the event `days` is 0, never negative.
    """
    event_date = (
        cfg_or_event_date if isinstance(cfg_or_event_date, date) else cfg_or_event_date.event_date
    )
    days = (event_date - today).days
    if days < 0:
        return {"days": 0, "label": "Event day passed"}
    if days == 0:
        return {"days": 0, "label": "Today!"}
    if days == 1:
        return {"days": 1, "label": "Tomorrow"}
    return {"days": days, "label": f"{days} days"}


def build_digest(con: sqlite3.Connection, cfg: Config) -> dict:
    """Everything the home screen, `get_digest` and the weekly email show.

    `budget` (the budget summary plus the currency) is there only once the budget has items.
    """
    ev = _event_row(con, cfg)
    digest = {
        "event": ev,
        "countdown": countdown(date.fromisoformat(ev["event_date"]), clock.today(cfg)),
        "needs_attention": needs_attention(con, cfg),
        "due_this_week": due_this_week(con, cfg),
        "inbox_count": emails.inbox_count(con),
        "workstreams": workstream_progress(con),
    }
    if budget.count(con):
        digest["budget"] = {**budget.summary(con), "currency": cfg.currency}
    return digest
