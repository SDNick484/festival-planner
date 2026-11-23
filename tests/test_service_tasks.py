import threading

import pytest

from festival_planner import db
from festival_planner.service import Actor, NotFound, ValidationError, activity, tasks, workstreams

ORGANIZERS = ("organizer-a@example.com", "organizer-b@example.com")
WEB = Actor("web", "organizer-a@example.com")
CLAUDE = Actor("claude", "claude")


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def con(db_path):
    con = db.connect(db_path)
    db.migrate(con)
    yield con
    con.close()


@pytest.fixture
def ws(con):
    return workstreams.create(con, WEB, {"name": "Vendors"})["id"]


def new_task(con, ws, **data):
    return tasks.create(con, WEB, {"workstream_id": ws, "title": "Book tent", **data})


def rows(con, entity_id, action=None):
    return [
        a
        for a in activity.list_activity(con, entity_type="task", entity_id=entity_id)
        if action is None or a["action"] == action
    ]


def test_create_task_logs_activity(con, ws):
    task = tasks.create(
        con,
        WEB,
        {"workstream_id": ws, "title": " Book tent ", "owner": "organizer-b@example.com"},
        organizers=ORGANIZERS,
    )
    assert task["title"] == "Book tent"
    assert task["owner"] == "organizer-b@example.com"
    assert task["status"] == "todo"
    log = rows(con, task["id"])
    assert len(log) == 1
    assert log[0]["action"] == "create"
    assert log[0]["source"] == "web"
    assert log[0]["actor"] == "organizer-a@example.com"
    assert log[0]["entity_type"] == "task"
    assert log[0]["diff"]["title"] == [None, "Book tent"]


def test_update_records_field_diff(con, ws):
    task = new_task(con, ws)
    updated = tasks.update(con, WEB, task["id"], {"status": "waiting"})
    assert updated["status"] == "waiting"
    log = rows(con, task["id"], "update")
    assert len(log) == 1
    assert log[0]["diff"] == {"status": ["todo", "waiting"]}


def test_noop_update_logs_nothing(con, ws):
    task = new_task(con, ws, notes="hello")
    after = tasks.update(con, WEB, task["id"], {"title": "  Book tent ", "notes": "hello"})
    assert after == tasks.get(con, task["id"])
    assert after["updated_at"] == task["updated_at"]
    assert rows(con, task["id"], "update") == []
    tasks.update(con, WEB, task["id"], {})
    assert rows(con, task["id"], "update") == []


def test_blocked_without_reason_rejected(con, ws):
    task = new_task(con, ws)
    for changes in ({"status": "blocked"}, {"status": "blocked", "blocked_reason": "   "}):
        with pytest.raises(ValidationError) as exc:
            tasks.update(con, WEB, task["id"], changes)
        assert "blocked_reason" in exc.value.errors
    with pytest.raises(ValidationError) as exc:
        new_task(con, ws, status="blocked")
    assert "blocked_reason" in exc.value.errors
    assert tasks.get(con, task["id"])["status"] == "todo"
    assert rows(con, task["id"], "update") == []

    blocked = tasks.update(
        con, WEB, task["id"], {"status": "blocked", "blocked_reason": "permit pending"}
    )
    assert blocked["blocked_reason"] == "permit pending"
    # Leaving blocked clears the reason, and the diff says so.
    doing = tasks.update(con, WEB, task["id"], {"status": "doing"})
    assert doing["blocked_reason"] is None
    assert rows(con, task["id"], "update")[0]["diff"] == {
        "status": ["blocked", "doing"],
        "blocked_reason": ["permit pending", None],
    }


def test_done_sets_and_clears_done_at(con, ws):
    task = new_task(con, ws)
    assert task["done_at"] is None
    done = tasks.update(con, WEB, task["id"], {"status": "done"})
    assert done["done_at"]
    again = tasks.update(con, WEB, task["id"], {"status": "done", "title": "Book big tent"})
    assert again["done_at"] == done["done_at"]  # staying done keeps the original time
    reopened = tasks.update(con, WEB, task["id"], {"status": "doing"})
    assert reopened["done_at"] is None
    created_done = new_task(con, ws, status="done")
    assert created_done["done_at"]
    with pytest.raises(ValidationError) as exc:
        tasks.update(con, WEB, task["id"], {"done_at": "2099-01-01T00:00:00Z"})
    assert "done_at" in exc.value.errors


def test_link_scheme_validation(con, ws):
    for bad in (
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html,hi",
        "ftp://files.example.com/x",
        "https://",
        "//example.com/no-scheme",
    ):
        with pytest.raises(ValidationError) as exc:
            new_task(con, ws, links=[{"label": "x", "url": bad}])
        assert "links" in exc.value.errors, bad
    with pytest.raises(ValidationError):
        new_task(con, ws, links=[{"label": "x" * 101, "url": "https://example.com"}])
    with pytest.raises(ValidationError):
        new_task(con, ws, links=[{"label": "x", "url": "https://example.com"}] * 21)
    with pytest.raises(ValidationError):
        new_task(con, ws, links=[{"label": "x", "url": "https://example.com", "extra": 1}])
    ok = new_task(
        con,
        ws,
        links=[
            {"label": "Quote", "url": "https://example.com/quote"},
            {"label": "", "url": "http://example.org"},
        ],
    )
    assert ok["links"][0] == {"label": "Quote", "url": "https://example.com/quote"}


def test_unknown_field_rejected(con, ws):
    with pytest.raises(ValidationError) as exc:
        new_task(con, ws, colour="red")
    assert "colour" in exc.value.errors
    task = new_task(con, ws)
    for changes in ({"id": 99}, {"created_at": "x"}, {"deleted_at": None}, {"bogus": 1}):
        with pytest.raises(ValidationError) as exc:
            tasks.update(con, WEB, task["id"], changes)
        assert set(changes) <= set(exc.value.errors)
    # Wrong types are validation errors too, never a crash.
    for changes in (
        {"title": 5},
        {"status": "open"},
        {"due_date": "10/10/2099"},
        {"due_date": "2099-02-30"},
        {"workstream_id": "1"},
        {"workstream_id": True},
        {"notes": "x" * 20_001},
        {"title": "x" * 201},
        {"title": "   "},
        {"links": "https://example.com"},
    ):
        with pytest.raises(ValidationError):
            tasks.update(con, WEB, task["id"], changes)


def test_owner_must_be_configured_organizer(con, ws):
    task = new_task(con, ws)
    with pytest.raises(ValidationError) as exc:
        tasks.update(con, WEB, task["id"], {"owner": "stranger@example.net"}, organizers=ORGANIZERS)
    assert "owner" in exc.value.errors
    with pytest.raises(ValidationError):  # no organizers given: nobody can be assigned
        tasks.update(con, WEB, task["id"], {"owner": "organizer-b@example.com"})
    owned = tasks.update(
        con, WEB, task["id"], {"owner": " Organizer-B@Example.com "}, organizers=ORGANIZERS
    )
    assert owned["owner"] == "organizer-b@example.com"
    assert tasks.update(con, WEB, task["id"], {"owner": None})["owner"] is None


def test_references_must_exist(con, ws):
    with pytest.raises(ValidationError) as exc:
        tasks.create(con, WEB, {"workstream_id": 999, "title": "x"})
    assert "workstream_id" in exc.value.errors
    with pytest.raises(ValidationError) as exc:
        tasks.create(con, WEB, {"title": "x"})
    assert "workstream_id" in exc.value.errors
    with pytest.raises(ValidationError) as exc:
        new_task(con, ws, milestone_id=999)
    assert "milestone_id" in exc.value.errors
    with pytest.raises(NotFound):
        tasks.update(con, WEB, 999, {"title": "x"})
    with pytest.raises(NotFound):
        tasks.get(con, 999)


def test_soft_delete_hides_from_list_but_activity_remains(con, ws):
    keep = new_task(con, ws, title="Keep")
    gone = new_task(con, ws, title="Gone")
    tasks.delete(con, WEB, gone["id"])
    assert [t["id"] for t in tasks.list(con)] == [keep["id"]]
    with pytest.raises(NotFound):
        tasks.get(con, gone["id"])
    with pytest.raises(NotFound):
        tasks.delete(con, WEB, gone["id"])
    with pytest.raises(NotFound):
        tasks.update(con, WEB, gone["id"], {"title": "Back"})
    assert [a["action"] for a in rows(con, gone["id"])] == ["delete", "create"]
    assert con.execute("SELECT deleted_at FROM task WHERE id = ?", (gone["id"],)).fetchone()[0]


def test_list_filters(con, ws):
    other = workstreams.create(con, WEB, {"name": "Marketing"})["id"]
    a = new_task(con, ws, title="Order 100% cotton shirts", due_date="2099-09-01")
    b = new_task(con, other, title="Poster", due_date="2099-09-20", status="waiting")
    c = new_task(con, ws, title="Thank sponsors", status="done")
    ids = lambda ts: [t["id"] for t in ts]  # noqa: E731
    assert ids(tasks.list(con, workstream_id=ws)) == [a["id"], c["id"]]
    assert ids(tasks.list(con, status="waiting")) == [b["id"]]
    assert ids(tasks.list(con, include_done=False)) == [a["id"], b["id"]]
    assert ids(tasks.list(con, q="100%")) == [a["id"]]
    assert ids(tasks.list(con, q="POSTER")) == [b["id"]]
    from datetime import date

    assert ids(tasks.list(con, due_before=date(2099, 9, 20))) == [a["id"]]
    assert ids(tasks.list(con, limit=1, offset=1)) == [b["id"]]
    tasks.update(con, WEB, a["id"], {"owner": ORGANIZERS[1]}, organizers=ORGANIZERS)
    assert ids(tasks.list(con, owner=ORGANIZERS[1])) == [a["id"]]
    for bad in ({"status": "open"}, {"owner": 5}, {"q": 5}, {"limit": 0}, {"offset": -1}):
        with pytest.raises(ValidationError):
            tasks.list(con, **bad)


def test_log_note(con, ws):
    task = new_task(con, ws)
    activity_id = tasks.log_note(con, CLAUDE, "task", task["id"], "  Called vendor, confirmed  ")
    entry = rows(con, task["id"])[0]
    assert entry["id"] == activity_id
    assert entry["action"] == "note"
    assert entry["note"] == "Called vendor, confirmed"
    assert entry["source"] == "claude" and entry["actor"] == "claude"
    assert entry["diff"] is None
    tasks.log_note(con, WEB, "workstream", ws, "Kickoff done")
    assert activity.list_activity(con, entity_type="workstream", entity_id=ws)[0]["note"] == (
        "Kickoff done"
    )
    with pytest.raises(ValidationError):
        tasks.log_note(con, WEB, "task", task["id"], "   ")
    with pytest.raises(ValidationError):
        tasks.log_note(con, WEB, "contact", 1, "nope")
    with pytest.raises(NotFound):
        tasks.log_note(con, WEB, "task", 999, "nope")


def test_failed_write_leaves_no_activity(con, ws, monkeypatch):
    task = new_task(con, ws)

    def boom(*args, **kwargs):
        raise RuntimeError("activity insert failed")

    monkeypatch.setattr(activity, "record", boom)
    with pytest.raises(RuntimeError):
        tasks.update(con, WEB, task["id"], {"title": "Changed"})
    assert not con.in_transaction
    assert tasks.get(con, task["id"])["title"] == "Book tent"


def test_concurrent_updates_both_logged(db_path, con, ws):
    """Review Focus 5: two writers on two connections at once; both succeed and are logged."""
    task_id = new_task(con, ws)["id"]
    rounds = 25
    barrier = threading.Barrier(2)
    results: dict[str, list] = {}
    errors: list[BaseException] = []

    def writer(name, actor):
        try:
            own = db.connect(db_path)
            try:
                barrier.wait(timeout=10)
                results[name] = [
                    tasks.update(own, actor, task_id, {"title": f"{name}{i}"})
                    for i in range(rounds)
                ]
            finally:
                own.close()
        except BaseException as exc:  # surfaced below; "database is locked" would land here
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=("web", WEB)),
        threading.Thread(target=writer, args=("claude", CLAUDE)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    assert len(results["web"]) == len(results["claude"]) == rounds

    final = tasks.get(con, task_id)["title"]
    assert final in {f"web{rounds - 1}", f"claude{rounds - 1}"}
    log = sorted(rows(con, task_id, "update"), key=lambda a: a["id"])
    assert len(log) == 2 * rounds
    assert {a["source"] for a in log} == {"web", "claude"}
    # Each diff's "before" is the previous write's "after": the read happened under the write lock.
    previous = "Book tent"
    for entry in log:
        before, after = entry["diff"]["title"]
        assert before == previous
        previous = after
    assert previous == final
