"""Email capture and triage (spec 4.2): capture, suggest, file, file_new_task, dismiss, reads,
and the poller's bookkeeping (per-label IMAP state and `last_email_poll`).

Activity:
- capture logs `Actor("email", <captured_by organizer or "external">)`, action `capture`, on the
  email item. A subject tag `[T-<id>]` naming a live task also files it, which logs `file_email`
  on the task with the same actor.
- filing (from any channel) logs `file_email` on the task, with the email's `forward_note` as
  the activity note, so the organizer's one-liner shows up in the task's history.
- dismissing logs `dismiss` on the email item.
- the retention purge (`purge_bodies`) logs one `system` activity per run that clears bodies, with
  the count in the note. A run that clears nothing changes nothing, so it logs nothing.

The IMAP state and `meta` writes are bookkeeping, not model changes, so they log no activity.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from festival_planner.config import Config
from festival_planner.mail.parse import ParsedEmail
from festival_planner.service import (
    Actor,
    NotFound,
    ValidationError,
    activity,
    event,
    meta,
    tasks,
)
from festival_planner.service._common import MAX_NAME, is_live, now_iso, transaction

TABLE = "email_item"
STATES = ("inbox", "filed", "dismissed")
JSON_COLUMNS = ("to_addrs", "cc_addrs", "attachment_names")
LAST_POLL_KEY = "last_email_poll"
NO_SUBJECT_TITLE = "(no subject)"
PURGE_ACTOR = Actor("system", "system")


def _norm(address: str | None) -> str:
    return (address or "").strip().lower()


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for key in JSON_COLUMNS:
        out[key] = json.loads(out[key])
    return out


def _fetch(con: sqlite3.Connection, email_id: Any) -> dict[str, Any]:
    if isinstance(email_id, bool) or not isinstance(email_id, int):
        raise NotFound(f"email {email_id!r} not found")
    row = con.execute("SELECT * FROM email_item WHERE id = ?", (email_id,)).fetchone()
    if row is None:
        raise NotFound(f"email {email_id} not found")
    return _decode(row)


def organizer_for(cfg: Config, address: str | None) -> str | None:
    """The organizer (primary email) who owns `address`, as login email or alias, else None."""
    address = _norm(address)
    for organizer in cfg.organizers:
        if address == _norm(organizer.email) or address in {_norm(a) for a in organizer.aliases}:
            return organizer.email
    return None


# --- capture -------------------------------------------------------------------------------


def capture(con: sqlite3.Connection, cfg: Config, parsed: ParsedEmail) -> dict | None:
    """Store a parsed message, or return None if its Message-ID is already stored."""
    sender = _norm(parsed.from_addr)
    captured_by = organizer_for(cfg, sender)
    # An organizer's forward: the original sender, when recognizable, is the sender (spec 4.2).
    # Anyone else's forward keeps its real sender, so we still know who sent it to us.
    from_addr = _norm(parsed.forwarded_from) if captured_by and parsed.forwarded_from else sender
    actor = Actor("email", captured_by or "external")

    with transaction(con):
        if con.execute(
            "SELECT 1 FROM email_item WHERE message_id = ?", (parsed.message_id,)
        ).fetchone():
            return None
        tag = parsed.task_tag
        task_id = tag if tag is not None and is_live(con, "task", tag) else None
        state = "filed" if task_id is not None else "inbox"
        suggested = None if task_id is not None else suggest(con, parsed)
        ts = now_iso()
        cur = con.execute(
            "INSERT INTO email_item (message_id, thread_key, from_addr, to_addrs, cc_addrs,"
            " subject, sent_at, body_text, attachment_names, captured_by, forward_note, state,"
            " task_id, suggested_workstream_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                parsed.message_id,
                parsed.thread_key or parsed.message_id,
                from_addr,
                json.dumps([_norm(a) for a in parsed.to_addrs]),
                json.dumps([_norm(a) for a in parsed.cc_addrs]),
                parsed.subject,
                parsed.sent_at,
                parsed.body_text,
                json.dumps(list(parsed.attachment_names), ensure_ascii=False),
                captured_by,
                parsed.forward_note,
                state,
                task_id,
                suggested,
                ts,
                ts,
            ),
        )
        email_id: int = cur.lastrowid  # type: ignore[assignment]
        diff: dict[str, list] = {"subject": [None, parsed.subject], "state": [None, state]}
        if task_id is not None:
            diff["task_id"] = [None, task_id]
        activity.record(con, actor, TABLE, email_id, "capture", diff)
        if task_id is not None:
            _log_filing(con, actor, email_id, task_id, parsed.forward_note)
        return _fetch(con, email_id)


def suggest(con: sqlite3.Connection, parsed: ParsedEmail) -> int | None:
    """The workstream to suggest for a message, or None (spec 4.2, filing step 2).

    1. A contact (in a live workstream) with any participant's address. Participants are tried
       in order: forwarded_from, from, to, cc; ties between contacts go to the oldest contact.
    2. Else the workstream of the task holding the latest filed email in the same thread.
    """
    participants = [
        _norm(a)
        for a in (parsed.forwarded_from, parsed.from_addr, *parsed.to_addrs, *parsed.cc_addrs)
        if _norm(a)
    ]
    rows = con.execute(
        "SELECT c.workstream_id, c.emails FROM contact c"
        " JOIN workstream w ON w.id = c.workstream_id"
        " WHERE c.deleted_at IS NULL AND w.deleted_at IS NULL ORDER BY c.id"
    ).fetchall()
    by_address: dict[str, int] = {}
    for workstream_id, addresses in rows:
        for address in json.loads(addresses):
            by_address.setdefault(_norm(address), workstream_id)
    for address in participants:
        if address in by_address:
            return by_address[address]

    row = con.execute(
        "SELECT t.workstream_id FROM email_item e"
        " JOIN task t ON t.id = e.task_id"
        " JOIN workstream w ON w.id = t.workstream_id"
        " WHERE e.thread_key = ? AND e.state = 'filed'"
        " AND t.deleted_at IS NULL AND w.deleted_at IS NULL"
        " ORDER BY e.updated_at DESC, e.id DESC LIMIT 1",
        (parsed.thread_key,),
    ).fetchone()
    return None if row is None else row[0]


# --- triage --------------------------------------------------------------------------------


def _log_filing(
    con: sqlite3.Connection, actor: Actor, email_id: int, task_id: int, note: str | None
) -> None:
    activity.record(con, actor, "task", task_id, "file_email", {TABLE: [None, email_id]}, note)


def _file(con: sqlite3.Connection, actor: Actor, before: dict, task_id: int) -> dict:
    if before["state"] == "filed" and before["task_id"] == task_id:
        return before
    con.execute(
        "UPDATE email_item SET state = 'filed', task_id = ?, updated_at = ? WHERE id = ?",
        (task_id, now_iso(), before["id"]),
    )
    _log_filing(con, actor, before["id"], task_id, before["forward_note"])
    return _fetch(con, before["id"])


def file(con: sqlite3.Connection, actor: Actor, email_id: int, task_id: int) -> dict:  # noqa: A001
    """File an email to an existing live task (from the inbox, or re-file from anywhere)."""
    with transaction(con):
        before = _fetch(con, email_id)
        if not is_live(con, "task", task_id):
            raise ValidationError({"task_id": "no such task"})
        return _file(con, actor, before, task_id)


def file_new_task(
    con: sqlite3.Connection,
    actor: Actor,
    email_id: int,
    workstream_id: int,
    title: str | None = None,
) -> dict:
    """Create a task (titled with the subject unless `title` is given) and file the email to it.

    Both happen in one transaction: if the task is invalid, nothing is written.
    """
    with transaction(con):
        before = _fetch(con, email_id)
        if title is None:
            title = before["subject"].strip()[:MAX_NAME].strip() or NO_SUBJECT_TITLE
        task = tasks.create(con, actor, {"workstream_id": workstream_id, "title": title})
        return _file(con, actor, before, task["id"])


def dismiss(con: sqlite3.Connection, actor: Actor, email_id: int) -> dict:
    with transaction(con):
        before = _fetch(con, email_id)
        diff = {"state": [before["state"], "dismissed"], "task_id": [before["task_id"], None]}
        diff = {k: v for k, v in diff.items() if v[0] != v[1]}
        if not diff:
            return before
        con.execute(
            "UPDATE email_item SET state = 'dismissed', task_id = NULL, updated_at = ?"
            " WHERE id = ?",
            (now_iso(), email_id),
        )
        activity.record(con, actor, TABLE, email_id, "dismiss", diff)
        return _fetch(con, email_id)


# --- reads ---------------------------------------------------------------------------------


def get(con: sqlite3.Connection, email_id: int) -> dict:
    return _fetch(con, email_id)


def list_inbox(con: sqlite3.Connection) -> list[dict]:
    """Untriaged emails, newest capture first."""
    rows = con.execute("SELECT * FROM email_item WHERE state = 'inbox' ORDER BY id DESC")
    return [_decode(r) for r in rows.fetchall()]


def inbox_count(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COUNT(*) FROM email_item WHERE state = 'inbox'").fetchone()[0]


# --- poller bookkeeping --------------------------------------------------------------------


def get_imap_state(con: sqlite3.Connection, label: str) -> tuple[int | None, int]:
    """(uidvalidity, last_uid) for a label; (None, 0) before its first poll."""
    row = con.execute(
        "SELECT uidvalidity, last_uid FROM imap_state WHERE label = ?", (label,)
    ).fetchone()
    if row is None:
        return None, 0
    return row[0], row[1] or 0


def save_imap_state(
    con: sqlite3.Connection, label: str, uidvalidity: int | None, last_uid: int
) -> None:
    with transaction(con):
        con.execute(
            "INSERT INTO imap_state (label, uidvalidity, last_uid) VALUES (?, ?, ?)"
            " ON CONFLICT (label) DO UPDATE"
            " SET uidvalidity = excluded.uidvalidity, last_uid = excluded.last_uid",
            (label, uidvalidity, last_uid),
        )


def record_poll(con: sqlite3.Connection, at: str) -> None:
    """Remember the time (UTC ISO-8601) of the last successful poll, for /healthz."""
    meta.set(con, LAST_POLL_KEY, at)


def last_email_poll(con: sqlite3.Connection) -> str | None:
    return meta.get(con, LAST_POLL_KEY)


# --- retention purge -----------------------------------------------------------------------


def _event_date(con: sqlite3.Connection, cfg: Config) -> date:
    """The event date from the event row; the config's value before the event is set up."""
    try:
        return date.fromisoformat(event.get(con)["event_date"])
    except NotFound:
        return cfg.event_date


def purge_bodies(con: sqlite3.Connection, cfg: Config, today: date) -> int:
    """Clear the body of every stored email once `event_date + email_retention_days` has passed.

    Only `body_text` (set to NULL) and `body_purged_at` (set to now) change; the metadata stays.
    Idempotent: rows already purged are not touched, so a later run on the same day returns 0.
    Returns the number of bodies cleared. Logs one system activity when that number is above 0,
    with the count in its note; `entity_id` is 0 because one run covers many items.
    """
    cutoff = _event_date(con, cfg) + timedelta(days=cfg.email_retention_days)
    if today <= cutoff:
        return 0
    with transaction(con):
        cur = con.execute(
            "UPDATE email_item SET body_text = NULL, body_purged_at = ?"
            " WHERE body_text IS NOT NULL",
            (now_iso(),),
        )
        count = cur.rowcount
        if count:
            activity.record(con, PURGE_ACTOR, TABLE, 0, "purge", None, str(count))
    return count
