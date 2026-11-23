"""SQLite access: connection helper, numbered-SQL migrations and the per-request connection.

Only the service layer writes through these connections (Global Constraints). Migrations are
hand-written `migrations/NNNN_name.sql` files, applied in order at startup.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from flask import current_app, g

if TYPE_CHECKING:
    from flask import Flask

DB_FILENAME = "festival.db"
BUSY_TIMEOUT_MS = 5000
# Resolved from this file so the SQL ships with the code, wherever the checkout (or image) lives.
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_MIGRATION_RE = re.compile(r"(\d{4})_[A-Za-z0-9_]+\.sql")


class MigrationError(Exception):
    """The migrations directory is inconsistent (bad file name, duplicate or missing number)."""


def connect(path: Path) -> sqlite3.Connection:
    """Open `path` (creating its folder) in WAL mode with foreign keys and a 5 s busy timeout."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    con.row_factory = sqlite3.Row
    # Set the timeout before anything that can contend for a lock (switching to WAL does).
    con.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _discover(migrations_dir: Path) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        m = _MIGRATION_RE.fullmatch(path.name)
        if not m:
            raise MigrationError(f"migration file name must look like NNNN_name.sql: {path.name}")
        found.append((int(m.group(1)), path))
    for expected, (version, path) in enumerate(found, start=1):
        if version != expected:
            raise MigrationError(f"migration numbers must be 1, 2, 3, ...: got {path.name}")
    return found


def _statements(sql: str):
    """Split a script into complete statements (a trigger body keeps its inner semicolons)."""
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            yield buffer
            buffer = ""
    if buffer.strip() and not all(
        ln.strip().startswith("--") or not ln.strip() for ln in buffer.splitlines()
    ):
        raise MigrationError("migration ends with an unterminated statement")


def _current_version(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]


def migrate(con: sqlite3.Connection, migrations_dir: Path | None = None) -> int:
    """Apply pending migrations in order and return the current schema version.

    Each migration runs in its own transaction together with its `schema_migrations` row, so a
    failure leaves the database at the previous version. The version is re-read after taking the
    write lock, so two processes starting at once do not apply the same migration twice.
    """
    files = _discover(migrations_dir or MIGRATIONS_DIR)
    con.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations"
        " (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    con.commit()
    for version, path in files:
        if version <= _current_version(con):
            continue
        sql = path.read_text(encoding="utf-8")
        try:
            con.execute("BEGIN IMMEDIATE")
            if version <= _current_version(con):  # another process got there first
                con.rollback()
                continue
            for statement in _statements(sql):
                con.execute(statement)
            con.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
            con.commit()
        except BaseException:
            if con.in_transaction:
                con.rollback()
            raise
    return _current_version(con)


def get_db() -> sqlite3.Connection:
    """The current request's connection, opened on first use and closed at context teardown."""
    if "db" not in g:
        path = current_app.config.get("DATABASE")
        if path is None:
            raise RuntimeError("no database configured: create_app() was called without a config")
        g.db = connect(path)
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    con = g.pop("db", None)
    if con is not None:
        con.close()


def init_app(app: Flask) -> None:
    """Wire the database into `app`: migrate at startup and close connections at teardown.

    With no config (tests that only need routes) there is no data_dir, so nothing is created
    or migrated, and `get_db()` raises until `app.config["DATABASE"]` is set.
    """
    app.config.setdefault("DATABASE", None)
    config = app.config.get("FP")
    if config is not None:
        app.config["DATABASE"] = Path(config.data_dir) / DB_FILENAME
        con = connect(app.config["DATABASE"])
        try:
            migrate(con)
        finally:
            con.close()
    app.teardown_appcontext(close_db)
