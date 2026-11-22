import dataclasses
import re
import sqlite3
from pathlib import Path

import pytest

from festival_planner import db
from festival_planner.config import load_config
from festival_planner.web import create_app

ROOT = Path(__file__).resolve().parent.parent


def latest_version() -> int:
    versions = [
        int(m.group(1))
        for p in db.MIGRATIONS_DIR.glob("*.sql")
        if (m := re.match(r"(\d{4})_", p.name))
    ]
    return max(versions)


@pytest.fixture
def con(tmp_path):
    con = db.connect(tmp_path / "test.db")
    db.migrate(con)
    yield con
    con.close()


def add_workstream(con) -> int:
    return con.execute("INSERT INTO workstream (name) VALUES ('Vendors')").lastrowid


def test_migrate_fresh_db_reaches_latest_version(tmp_path):
    con = db.connect(tmp_path / "fresh.db")
    assert db.migrate(con) == latest_version() >= 1
    rows = con.execute("SELECT version, applied_at FROM schema_migrations").fetchall()
    assert [r["version"] for r in rows] == list(range(1, latest_version() + 1))
    assert all(r["applied_at"] for r in rows)
    tables = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"event", "workstream", "task", "milestone", "contact", "activity"} <= tables
    assert "organizer" not in tables
    con.close()


def test_migrate_is_idempotent(con):
    before = con.execute("SELECT * FROM schema_migrations").fetchall()
    assert db.migrate(con) == latest_version()
    after = con.execute("SELECT * FROM schema_migrations").fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]


def test_wal_and_fk_enabled(tmp_path):
    con = db.connect(tmp_path / "pragmas.db")
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert con.row_factory is sqlite3.Row
    db.migrate(con)
    with pytest.raises(sqlite3.IntegrityError):  # foreign key to a missing workstream
        con.execute("INSERT INTO task (workstream_id, title) VALUES (999, 'x')")
    con.close()


def test_blocked_requires_reason(con):
    ws = add_workstream(con)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO task (workstream_id, title, status) VALUES (?, 'Book tent', 'blocked')",
            (ws,),
        )
    con.execute(
        "INSERT INTO task (workstream_id, title, status, blocked_reason)"
        " VALUES (?, 'Book tent', 'blocked', 'waiting on permit')",
        (ws,),
    )


def test_task_status_is_restricted(con):
    ws = add_workstream(con)
    for status in ("todo", "doing", "waiting", "blocked", "done"):
        con.execute(
            "INSERT INTO task (workstream_id, title, status, blocked_reason)"
            " VALUES (?, 't', ?, 'r')",
            (ws, status),
        )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO task (workstream_id, title, status) VALUES (?, 't', 'open')", (ws,)
        )


def test_milestone_days_before_event_not_negative(con):
    con.execute("INSERT INTO milestone (name, days_before_event) VALUES ('T-0', 0)")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO milestone (name, days_before_event) VALUES ('bad', -1)")


def test_activity_is_append_only(con):
    con.execute(
        "INSERT INTO activity (source, actor, entity_type, entity_id, action)"
        " VALUES ('web', 'organizer-a@example.com', 'task', 1, 'create')"
    )
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("UPDATE activity SET note = 'edited'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("DELETE FROM activity")
    assert con.execute("SELECT count(*) FROM activity").fetchone()[0] == 1
    assert con.execute("SELECT at FROM activity").fetchone()["at"]  # default timestamp


def test_single_event_row(con):
    insert = (
        "INSERT INTO event (id, name, event_date, timezone)"
        " VALUES (?, 'Example', '2099-10-10', 'UTC')"
    )
    con.execute(insert, (1,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(insert, (2,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(insert, (1,))  # duplicate primary key
    assert con.execute("SELECT count(*) FROM event").fetchone()[0] == 1


def test_expected_indexes_exist(con):
    names = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    for table, col in [
        ("task", "workstream_id"),
        ("task", "due_date"),
        ("activity", "at"),
    ]:
        assert any(table in n and col in n for n in names), (table, col)
    assert any("activity" in n and "entity" in n for n in names)


def test_migrations_ship_with_the_package():
    assert db.MIGRATIONS_DIR == ROOT / "festival_planner" / "migrations"
    assert (db.MIGRATIONS_DIR / "0001_init.sql").is_file()


def test_failed_migration_rolls_back_and_is_not_recorded(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_a.sql").write_text("CREATE TABLE a (id INTEGER);")
    (migrations / "0002_b.sql").write_text(
        "CREATE TABLE b (id INTEGER);\nINSERT INTO nonexistent VALUES (1);"
    )
    con = db.connect(tmp_path / "x.db")
    with pytest.raises(sqlite3.Error):
        db.migrate(con, migrations)
    versions = [r[0] for r in con.execute("SELECT version FROM schema_migrations")]
    assert versions == [1]
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "a" in tables and "b" not in tables
    assert not con.in_transaction
    con.close()


def test_migrate_handles_triggers_and_rejects_bad_names(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_t.sql").write_text(
        "CREATE TABLE t (id INTEGER);\n"
        "CREATE TRIGGER t_no_delete BEFORE DELETE ON t BEGIN SELECT RAISE(ABORT, 'no'); END;\n"
    )
    con = db.connect(tmp_path / "x.db")
    assert db.migrate(con, migrations) == 1
    (migrations / "0003_gap.sql").write_text("SELECT 1;")
    with pytest.raises(db.MigrationError):  # 0002 is missing
        db.migrate(con, migrations)
    con.close()


def test_migrate_with_no_migrations_dir_files_returns_zero(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    con = db.connect(tmp_path / "x.db")
    assert db.migrate(con, empty) == 0
    con.close()


def make_config(tmp_path):
    cfg = load_config(ROOT / "config.example.toml", env={})
    return dataclasses.replace(cfg, data_dir=tmp_path / "data")


def test_create_app_migrates_at_startup_and_get_db_is_per_request(tmp_path):
    app = create_app(make_config(tmp_path))
    assert (tmp_path / "data" / db.DB_FILENAME).is_file()
    with app.app_context():
        con = db.get_db()
        assert db.get_db() is con
        version = con.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        assert version == latest_version()
    with pytest.raises(sqlite3.ProgrammingError):  # closed by the teardown hook
        con.execute("SELECT 1")
    with app.app_context():
        assert db.get_db() is not con


def test_create_app_without_config_skips_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app()
    assert not list(tmp_path.iterdir())
    with app.app_context(), pytest.raises(RuntimeError, match="database"):
        db.get_db()
