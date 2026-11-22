"""Shared plumbing for the service modules: the write transaction, field validators and the
generic insert / update / soft-delete steps that every entity uses.

Values handled here are "public" values (lists for JSON columns, bools for flags); they are
encoded for SQLite only at the moment of writing, so activity diffs show what the API shows.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit

from festival_planner.service import Actor, NotFound, ValidationError, activity

MAX_NAME = 200
MAX_NOTES = 20_000
MAX_LINKS = 20
MAX_LINK_LABEL = 100
MAX_URL = 2_000
MAX_EMAILS = 20
MAX_LIST_LIMIT = 500

JSON_FIELDS = frozenset({"links", "emails"})
BOOL_FIELDS = frozenset({"archived"})
# Never part of a diff or of client input.
BOOKKEEPING = ("id", "created_at", "updated_at", "deleted_at")

_EMAIL_RE = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MISSING = object()


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def transaction(con: sqlite3.Connection) -> Iterator[None]:
    """One write transaction that takes the write lock up front (BEGIN IMMEDIATE).

    Every mutation reads the current row and then writes it plus an activity row. With a deferred
    BEGIN, two connections could both read, and the second one to write would fail with
    "database is locked" (SQLite cannot upgrade a stale WAL read snapshot, and the busy timeout
    does not apply to that case). Taking the lock first makes the second writer wait for
    busy_timeout instead, and its read then sees the first writer's commit, so each diff's
    "before" is the true previous value. If the caller already holds a transaction, the work
    joins it and the caller commits.
    """
    if con.in_transaction:
        yield
        return
    con.execute("BEGIN IMMEDIATE")
    with con:  # commits on success, rolls back on any exception
        yield


# --- rows ----------------------------------------------------------------------------------


def decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in JSON_FIELDS & out.keys():
        out[key] = json.loads(out[key])
    for key in BOOL_FIELDS & out.keys():
        out[key] = bool(out[key])
    return out


def _encode(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, list | dict):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def fetch_live(con: sqlite3.Connection, table: str, entity_id: Any) -> dict[str, Any]:
    """The row with this id that is not soft-deleted, or NotFound."""
    if isinstance(entity_id, bool) or not isinstance(entity_id, int):
        raise NotFound(f"{table} {entity_id!r} not found")
    row = con.execute(
        f"SELECT * FROM {table} WHERE id = ? AND deleted_at IS NULL",  # noqa: S608 (fixed names)
        (entity_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"{table} {entity_id} not found")
    return decode(row)  # type: ignore[return-value]


def is_live(con: sqlite3.Connection, table: str, entity_id: int) -> bool:
    try:
        fetch_live(con, table, entity_id)
    except NotFound:
        return False
    return True


def page(limit: Any, offset: Any) -> tuple[int, int]:
    errors: dict[str, str] = {}
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST_LIMIT:
        errors["limit"] = f"must be an integer from 1 to {MAX_LIST_LIMIT}"
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        errors["offset"] = "must be a non-negative integer"
    if errors:
        raise ValidationError(errors)
    return limit, offset


def like(q: str) -> str:
    """A LIKE pattern matching `q` literally (use with ESCAPE '\\')."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# --- generic writes ------------------------------------------------------------------------


def insert(
    con: sqlite3.Connection, actor: Actor, table: str, values: Mapping[str, Any]
) -> dict[str, Any]:
    """Insert a row and log `create` with a diff of every non-empty field. Call in `transaction`."""
    ts = now_iso()
    columns = {**values, "created_at": ts, "updated_at": ts}
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    cur = con.execute(
        f"INSERT INTO {table} ({names}) VALUES ({marks})",  # noqa: S608 (validated names)
        [_encode(v) for v in columns.values()],
    )
    entity_id = cur.lastrowid
    diff = {k: [None, v] for k, v in values.items() if v is not None and v != "" and v != []}
    activity.record(con, actor, table, entity_id, "create", diff)
    return fetch_live(con, table, entity_id)


def apply_update(
    con: sqlite3.Connection,
    actor: Actor,
    table: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    """Write the fields of `after` that differ from `before` and log them. A no-op writes nothing.

    Call inside `transaction`, with `before` read inside that same transaction.
    """
    diff = {k: [before[k], v] for k, v in after.items() if before[k] != v}
    if not diff:
        return dict(before)
    ts = now_iso()
    sets = ", ".join(f"{k} = ?" for k in diff)
    con.execute(
        f"UPDATE {table} SET {sets}, updated_at = ? WHERE id = ?",  # noqa: S608 (validated names)
        [*(_encode(v) for _, v in diff.values()), ts, before["id"]],
    )
    activity.record(con, actor, table, before["id"], "update", diff)
    return {**before, **{k: v for k, (_, v) in diff.items()}, "updated_at": ts}


def soft_delete(con: sqlite3.Connection, actor: Actor, table: str, before: Mapping[str, Any]):
    ts = now_iso()
    con.execute(
        f"UPDATE {table} SET deleted_at = ?, updated_at = ? WHERE id = ?",  # noqa: S608
        (ts, ts, before["id"]),
    )
    activity.record(con, actor, table, before["id"], "delete", {"deleted_at": [None, ts]})


# --- validation ----------------------------------------------------------------------------


class Checker:
    """Collects per-field errors while cleaning one input dict (create or partial update)."""

    def __init__(self, data: Any, allowed: Collection[str], required: Collection[str] = ()):
        self.errors: dict[str, str] = {}
        self.out: dict[str, Any] = {}
        if not isinstance(data, Mapping):
            raise ValidationError({"_": "must be an object"})
        self.data = data
        for key in data:
            if key not in allowed:
                self.errors[str(key)] = "unknown field"
        for key in required:
            if key not in data:
                self.errors[key] = "required"

    def _get(self, key: str) -> Any:
        return self.data.get(key, _MISSING)

    def fail(self, key: str, message: str) -> None:
        self.errors.setdefault(key, message)

    def done(self) -> dict[str, Any]:
        if self.errors:
            raise ValidationError(self.errors)
        return self.out

    def text(
        self, key: str, *, max_len: int, required: bool = False, nullable: bool = False
    ) -> None:
        """A trimmed string. `required`: 1+ characters. `nullable`: None or "" store NULL."""
        value = self._get(key)
        if value is _MISSING:
            return
        if value is None and nullable:
            self.out[key] = None
            return
        if not isinstance(value, str):
            self.fail(key, "must be a string")
            return
        value = value.strip()
        if required and not value:
            self.fail(key, "must not be empty")
        elif len(value) > max_len:
            self.fail(key, f"must be at most {max_len} characters")
        else:
            self.out[key] = None if (nullable and not value) else value

    def notes(self, key: str = "notes") -> None:
        """Free text kept verbatim (markdown), at most MAX_NOTES characters."""
        value = self._get(key)
        if value is _MISSING:
            return
        if not isinstance(value, str):
            self.fail(key, "must be a string")
        elif len(value) > MAX_NOTES:
            self.fail(key, f"must be at most {MAX_NOTES} characters")
        else:
            self.out[key] = value

    def integer(
        self, key: str, *, lo: int, hi: int, nullable: bool = False, label: str = "an integer"
    ) -> None:
        value = self._get(key)
        if value is _MISSING:
            return
        if value is None and nullable:
            self.out[key] = None
        elif isinstance(value, bool) or not isinstance(value, int):
            self.fail(key, f"must be {label}")
        elif not lo <= value <= hi:
            self.fail(key, f"must be from {lo} to {hi}")
        else:
            self.out[key] = value

    def boolean(self, key: str) -> None:
        value = self._get(key)
        if value is _MISSING:
            return
        if not isinstance(value, bool):
            self.fail(key, "must be true or false")
        else:
            self.out[key] = value

    def choice(self, key: str, choices: Collection[str]) -> None:
        value = self._get(key)
        if value is _MISSING:
            return
        if value not in choices:
            self.fail(key, f"must be one of: {', '.join(choices)}")
        else:
            self.out[key] = value

    def iso_date(self, key: str, *, nullable: bool = False) -> None:
        value = self._get(key)
        if value is _MISSING:
            return
        if value is None and nullable:
            self.out[key] = None
            return
        try:
            self.out[key] = parse_date(value).isoformat()
        except ValueError:
            self.fail(key, "must be a date as YYYY-MM-DD")

    def owner(self, key: str, organizers: Collection[str] | None) -> None:
        """None (unassigned) or one of the configured organizer emails, stored lowercased."""
        value = self._get(key)
        if value is _MISSING:
            return
        if value is None:
            self.out[key] = None
            return
        if not isinstance(value, str):
            self.fail(key, "must be an organizer email or null")
            return
        value = value.strip().lower()
        if not value:
            self.out[key] = None
        elif value not in {o.strip().lower() for o in organizers or ()}:
            self.fail(key, "must be one of the configured organizers")
        else:
            self.out[key] = value

    def links(self, key: str = "links") -> None:
        value = self._get(key)
        if value is _MISSING:
            return
        if not isinstance(value, list):
            self.fail(key, "must be a list of {label, url}")
            return
        if len(value) > MAX_LINKS:
            self.fail(key, f"must have at most {MAX_LINKS} entries")
            return
        cleaned = []
        for i, item in enumerate(value):
            problem = _link_problem(item)
            if problem:
                self.fail(key, f"entry {i + 1}: {problem}")
                return
            cleaned.append({"label": item["label"].strip(), "url": item["url"].strip()})
        self.out[key] = cleaned

    def emails(self, key: str = "emails") -> None:
        value = self._get(key)
        if value is _MISSING:
            return
        if not isinstance(value, list) or not all(isinstance(e, str) for e in value):
            self.fail(key, "must be a list of email addresses")
            return
        if len(value) > MAX_EMAILS:
            self.fail(key, f"must have at most {MAX_EMAILS} entries")
            return
        cleaned = [e.strip().lower() for e in value]
        bad = [e for e in cleaned if len(e) > MAX_NAME or not _EMAIL_RE.fullmatch(e)]
        if bad:
            self.fail(key, "contains an invalid email address")
        else:
            self.out[key] = cleaned


def parse_date(value: Any) -> date:
    """A `date` (not a datetime) or a strict YYYY-MM-DD string; ValueError otherwise."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and _DATE_RE.fullmatch(value.strip()):
        return date.fromisoformat(value.strip())
    raise ValueError("not an ISO date")


def _link_problem(item: Any) -> str | None:
    if not isinstance(item, Mapping) or set(item) != {"label", "url"}:
        return "must be an object with exactly label and url"
    label, url = item["label"], item["url"]
    if not isinstance(label, str) or len(label.strip()) > MAX_LINK_LABEL:
        return f"label must be a string of at most {MAX_LINK_LABEL} characters"
    if not isinstance(url, str):
        return "url must be a string"
    url = url.strip()
    if len(url) > MAX_URL or any(ch.isspace() or ord(ch) < 32 for ch in url):
        return "url is too long or contains whitespace"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "url is malformed"
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return "url must be an http or https address"
    return None
