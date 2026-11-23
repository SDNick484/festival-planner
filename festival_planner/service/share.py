"""Volunteer read-only share links (Task 24).

A link is a random token (`secrets.token_urlsafe(32)`, 256 bits) that the organizer hands out as
`/s/<token>`. Only its SHA-256 is stored, and a request finds its link by looking that hash up, so
the database never holds anything that opens a link. The raw token is returned once, by `create`.

`shared_view` is the whole public surface: task title, status, effective due date and owner
display name, plus milestone names and dates, for the link's scope. It reads only those columns,
so notes, links, waiting/blocked reasons, contacts, email and activity cannot leak through it.
"""

from __future__ import annotations

import builtins
import hashlib
import secrets
import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import Any

from festival_planner import clock
from festival_planner.config import Config
from festival_planner.service import Actor, NotFound, ValidationError, activity, event
from festival_planner.service._common import MAX_NAME, Checker, is_live, transaction
from festival_planner.views import effective_due

TABLE = "share_link"
TOKEN_BYTES = 32
MAX_DAYS = 365
# Columns an organizer may see when listing links (never the hash).
PUBLIC_COLUMNS = ("id", "label", "workstream_id", "expires_at", "revoked_at", "created_at")


def token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8", "replace")).hexdigest()


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> str:
    return _iso(clock.now())


def _public(row: sqlite3.Row | dict[str, Any], now: str) -> dict[str, Any]:
    out = {k: row[k] for k in PUBLIC_COLUMNS}
    if out["revoked_at"] is not None:
        out["state"] = "revoked"
    elif out["expires_at"] <= now:
        out["state"] = "expired"
    else:
        out["state"] = "active"
    return out


def _fetch(con: sqlite3.Connection, link_id: Any) -> sqlite3.Row:
    if isinstance(link_id, bool) or not isinstance(link_id, int):
        raise NotFound(f"share link {link_id!r} not found")
    row = con.execute("SELECT * FROM share_link WHERE id = ?", (link_id,)).fetchone()
    if row is None:
        raise NotFound(f"share link {link_id} not found")
    return row


# --- writes --------------------------------------------------------------------------------


def create(
    con: sqlite3.Connection, actor: Actor, label: Any, workstream_id: Any, days: Any
) -> tuple[int, str]:
    """Create a link valid for `days` days (1..365) and return `(link_id, raw_token)`.

    `workstream_id` None shares every workstream. The raw token is not stored and cannot be
    recovered: show it to the organizer now.
    """
    data = {"label": label, "workstream_id": workstream_id, "days": days}
    c = Checker(data, tuple(data))
    c.text("label", max_len=MAX_NAME, required=True)
    c.integer("workstream_id", lo=1, hi=2**63 - 1, nullable=True, label="a workstream id")
    c.integer("days", lo=1, hi=MAX_DAYS, label="a whole number of days")
    values = c.done()
    raw = secrets.token_urlsafe(TOKEN_BYTES)
    with transaction(con):
        ws = values["workstream_id"]
        if ws is not None and not is_live(con, "workstream", ws):
            raise ValidationError({"workstream_id": "no such workstream"})
        ts = _now()
        expires = _iso(clock.now() + timedelta(days=values["days"]))
        cur = con.execute(
            "INSERT INTO share_link"
            " (token_hash, label, workstream_id, expires_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (token_hash(raw), values["label"], ws, expires, ts, ts),
        )
        link_id = cur.lastrowid
        # The hash is deliberately not in the diff: the log shows what was shared, not the key.
        diff = {"label": [None, values["label"]], "expires_at": [None, expires]}
        if ws is not None:
            diff["workstream_id"] = [None, ws]
        activity.record(con, actor, TABLE, link_id, "create", diff)  # type: ignore[arg-type]
    return link_id, raw  # type: ignore[return-value]


def revoke(con: sqlite3.Connection, actor: Actor, link_id: int) -> dict[str, Any]:
    """Revoke a link (it then 404s). Revoking a revoked link changes nothing."""
    with transaction(con):
        row = _fetch(con, link_id)
        if row["revoked_at"] is None:
            ts = _now()
            con.execute(
                "UPDATE share_link SET revoked_at = ?, updated_at = ? WHERE id = ?",
                (ts, ts, link_id),
            )
            activity.record(con, actor, TABLE, link_id, "revoke", {"revoked_at": [None, ts]})
            row = _fetch(con, link_id)
    return _public(row, _now())


# --- reads ---------------------------------------------------------------------------------


def get(con: sqlite3.Connection, link_id: int) -> dict[str, Any]:
    return _public(_fetch(con, link_id), _now())


def list(con: sqlite3.Connection) -> builtins.list[dict[str, Any]]:  # noqa: A001
    """Every link, newest first, with its `state` (active, expired or revoked)."""
    now = _now()
    rows = con.execute("SELECT * FROM share_link ORDER BY id DESC").fetchall()
    return [_public(r, now) for r in rows]


def lookup(con: sqlite3.Connection, raw_token: str) -> dict[str, Any] | None:
    """The active link for this raw token, or None if it is unknown, expired, revoked, or scoped
    to a workstream that has since been deleted. Callers must not say which."""
    if not isinstance(raw_token, str) or not raw_token:
        return None
    row = con.execute(
        "SELECT s.id, s.workstream_id FROM share_link s"
        " LEFT JOIN workstream w ON w.id = s.workstream_id"
        " WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ?"
        " AND (s.workstream_id IS NULL OR w.deleted_at IS NULL)",
        (token_hash(raw_token), _now()),
    ).fetchone()
    return None if row is None else {"id": row["id"], "workstream_id": row["workstream_id"]}


def _event_date(con: sqlite3.Connection, cfg: Config) -> date:
    try:
        return date.fromisoformat(event.get(con)["event_date"])
    except NotFound:
        return cfg.event_date


def shared_view(con: sqlite3.Connection, cfg: Config, link: dict[str, Any]) -> dict[str, Any]:
    """What a share link shows: `{"tasks": [...], "milestones": [...]}`, nothing more.

    Tasks: `title`, `status`, `effective_due` and `owner` (the organizer's display name, never an
    email). A workstream link shows that workstream's live tasks and the milestones they use; an
    all-workstreams link shows the tasks of every live, unarchived workstream and every milestone.
    Milestones: `name` and `date`. Sorted by date, undated last.
    """
    event_date = _event_date(con, cfg)
    stones = con.execute(
        "SELECT id, name, days_before_event FROM milestone WHERE deleted_at IS NULL"
    ).fetchall()
    days = {m["id"]: m["days_before_event"] for m in stones}
    if link["workstream_id"] is None:
        rows = con.execute(
            "SELECT t.title, t.status, t.due_date, t.milestone_id, t.owner FROM task t"
            " JOIN workstream w ON w.id = t.workstream_id"
            " WHERE t.deleted_at IS NULL AND w.deleted_at IS NULL AND w.archived = 0"
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT title, status, due_date, milestone_id, owner FROM task"
            " WHERE deleted_at IS NULL AND workstream_id = ?",
            (link["workstream_id"],),
        ).fetchall()
    names = {o.email: o.display_name for o in cfg.organizers}
    tasks = []
    for row in rows:
        due = effective_due(dict(row), event_date, days)
        tasks.append(
            {
                "title": row["title"],
                "status": row["status"],
                "effective_due": due.isoformat() if due else None,
                "owner": names.get(row["owner"]) if row["owner"] else None,
            }
        )
    tasks.sort(key=lambda t: (t["effective_due"] is None, t["effective_due"] or "", t["title"]))
    used = {r["milestone_id"] for r in rows if r["milestone_id"] is not None}
    milestones = [
        {
            "name": m["name"],
            "date": (event_date - timedelta(days=m["days_before_event"])).isoformat(),
        }
        for m in stones
        if link["workstream_id"] is None or m["id"] in used
    ]
    milestones.sort(key=lambda m: (m["date"], m["name"]))
    return {"tasks": tasks, "milestones": milestones}
