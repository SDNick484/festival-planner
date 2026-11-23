import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from festival_planner import db
from festival_planner.service import Actor, activity

WEB = Actor("web", "organizer-a@example.com")
CLAUDE = Actor("claude", "claude")


@pytest.fixture
def con(tmp_path):
    con = db.connect(tmp_path / "test.db")
    db.migrate(con)
    yield con
    con.close()


def test_actor_is_frozen():
    with pytest.raises(AttributeError):
        WEB.source = "email"  # type: ignore[misc]


def test_record_and_list(con):
    with con:
        first = activity.record(con, WEB, "task", 1, "create", {"title": [None, "Book tent"]})
        second = activity.record(con, CLAUDE, "task", 1, "note", note="Called vendor")
        activity.record(con, WEB, "workstream", 2, "update", {"name": ["A", "B"]})
    assert second > first
    entries = activity.list_activity(con, entity_type="task", entity_id=1)
    assert [e["id"] for e in entries] == [second, first]  # newest first
    assert entries[1]["diff"] == {"title": [None, "Book tent"]}
    assert entries[0]["diff"] is None and entries[0]["note"] == "Called vendor"
    assert entries[0]["at"].endswith("Z")
    assert [e["id"] for e in activity.list_activity(con, source="claude")] == [second]
    assert len(activity.list_activity(con)) == 3
    assert len(activity.list_activity(con, limit=2)) == 2


def test_list_since(con):
    with con:
        activity.record(con, WEB, "task", 1, "create")
    assert len(activity.list_activity(con, since=datetime.now(UTC) - timedelta(minutes=1))) == 1
    assert activity.list_activity(con, since=datetime.now(UTC) + timedelta(minutes=1)) == []


def test_record_rejects_empty_identity(con):
    with pytest.raises(ValueError):
        activity.record(con, Actor("", "x"), "task", 1, "create")
    with pytest.raises(ValueError):
        activity.record(con, Actor("web", " "), "task", 1, "create")


def test_activity_stays_append_only(con):
    with con:
        activity.record(con, WEB, "task", 1, "create")
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("DELETE FROM activity")
