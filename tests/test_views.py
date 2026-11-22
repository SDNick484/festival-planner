from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from access_keys import make_config

from festival_planner import clock, db, views
from festival_planner.service import Actor, event, milestones, tasks, workstreams

WEB = Actor("web", "organizer-a@example.com")
EVENT_DATE = date(2099, 10, 10)
NOON_UTC = datetime(2099, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path, event_date=EVENT_DATE)


@pytest.fixture
def con(tmp_path):
    con = db.connect(tmp_path / "test.db")
    db.migrate(con)
    # There is no event.create: the first-run bootstrap writes this row. Insert it directly.
    con.execute(
        "INSERT INTO event (id, name, event_date, timezone) VALUES (1, ?, ?, ?)",
        ("Example Fall Festival", EVENT_DATE.isoformat(), "UTC"),
    )
    con.commit()
    yield con
    con.close()


@pytest.fixture
def ws(con):
    return workstreams.create(con, WEB, {"name": "Vendors"})["id"]


@pytest.fixture
def at(monkeypatch):
    """Set the clock: at(datetime) pins clock.now."""

    def _set(moment):
        monkeypatch.setattr(clock, "now", lambda: moment)

    return _set


def new_task(con, ws, title="Book tent", **data):
    return tasks.create(con, WEB, {"workstream_id": ws, "title": title, **data})


def test_effective_due_prefers_explicit_date():
    task = {"due_date": "2099-09-01", "milestone_id": 1}
    assert views.effective_due(task, EVENT_DATE, {1: 14}) == date(2099, 9, 1)


def test_effective_due_from_milestone():
    task = {"due_date": None, "milestone_id": 1}
    assert views.effective_due(task, EVENT_DATE, {1: 14}) == EVENT_DATE - timedelta(days=14)
    assert views.effective_due({"due_date": None, "milestone_id": None}, EVENT_DATE, {}) is None
    assert views.effective_due({"due_date": None, "milestone_id": 9}, EVENT_DATE, {1: 14}) is None


def test_moving_event_date_moves_milestone_tasks(con, cfg, ws, at):
    ms = milestones.create(con, WEB, {"name": "Permits in", "days_before_event": 14})["id"]
    new_task(con, ws, milestone_id=ms)
    at(NOON_UTC)  # 9 days before the event: the T-14 task is 5 days overdue
    assert [t["age_days"] for t in views.overdue(con, cfg)] == [5]
    assert views.due_this_week(con, cfg) == []

    event.update(con, WEB, {"event_date": "2099-10-30"})  # T-14 is now 2099-10-16
    assert views.overdue(con, cfg) == []
    assert views.due_this_week(con, cfg) == []
    at(datetime(2099, 10, 12, 12, 0, tzinfo=UTC))
    [due] = views.due_this_week(con, cfg)
    assert due["effective_due"] == "2099-10-16"


def test_overdue_uses_event_timezone(con, tmp_path, ws, at):
    la = make_config(tmp_path, event_date=EVENT_DATE, timezone=ZoneInfo("America/Los_Angeles"))
    new_task(con, ws, due_date="2099-10-09")
    # 23:30 on 9 Oct in Los Angeles (UTC-7) is 06:30 on 10 Oct UTC: UTC has already rolled over.
    at(datetime(2099, 10, 10, 6, 30, tzinfo=UTC))
    assert clock.today(la) == date(2099, 10, 9)
    assert views.overdue(con, la) == []
    assert [t["title"] for t in views.due_this_week(con, la)] == ["Book tent"]
    # An hour later it is 00:30 on 10 Oct local, and the task is a day overdue.
    at(datetime(2099, 10, 10, 7, 30, tzinfo=UTC))
    [item] = views.overdue(con, la)
    assert item["age_days"] == 1
    # The same instant is already the next day in UTC: today is never taken from UTC.
    assert clock.today(make_config(tmp_path, timezone=ZoneInfo("UTC"))) == date(2099, 10, 10)


def test_due_this_week_window(con, cfg, ws, at):
    at(NOON_UTC)
    for day in ("2099-09-30", "2099-10-01", "2099-10-07", "2099-10-08"):
        new_task(con, ws, title=day, due_date=day)
    new_task(con, ws, title="done", due_date="2099-10-02", status="done")
    assert [t["title"] for t in views.due_this_week(con, cfg)] == ["2099-10-01", "2099-10-07"]


def test_waiting_stale_after_n_days(con, cfg, ws, at):
    at(NOON_UTC)
    assert cfg.waiting_stale_days == 5
    raw_task(con, ws, "four", "waiting", NOON_UTC - timedelta(days=4))
    raw_task(con, ws, "five", "waiting", NOON_UTC - timedelta(days=5))
    six = raw_task(con, ws, "six", "waiting", NOON_UTC - timedelta(days=6))
    [item] = views.needs_attention(con, cfg)
    assert item["id"] == six
    assert item["reason"] == "waiting"
    assert item["age_days"] == 6


def test_waiting_age_restarts_when_set_to_waiting_again(con, cfg, ws, at):
    task = new_task(con, ws, status="waiting")
    tasks.update(con, WEB, task["id"], {"status": "doing"})
    tasks.update(con, WEB, task["id"], {"status": "waiting"})
    at(datetime.now(UTC) + timedelta(days=6))
    assert views.needs_attention(con, cfg)[0]["age_days"] == 6
    # A note is not a status change.
    tasks.log_note(con, WEB, "task", task["id"], "pinged them")
    assert views.needs_attention(con, cfg)[0]["age_days"] == 6


def raw_task(con, ws, title, status, since, **data):
    """Insert a task and the activity row that set its status at `since` (UTC).

    The log is append-only and stamped with the real clock, so a task that has been waiting for
    days is built by inserting its history directly.
    """
    cur = con.execute(
        "INSERT INTO task (workstream_id, title, status, blocked_reason, due_date)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            ws,
            title,
            status,
            "no permit" if status == "blocked" else None,
            data.get("due_date"),
        ),
    )
    con.execute(
        "INSERT INTO activity (at, source, actor, entity_type, entity_id, action, diff)"
        " VALUES (?, 'web', 'organizer-a@example.com', 'task', ?, 'update', ?)",
        (
            since.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            cur.lastrowid,
            f'{{"status":["todo","{status}"]}}',
        ),
    )
    con.commit()
    return cur.lastrowid


def test_needs_attention_order(con, cfg, ws, at):
    at(NOON_UTC)
    days = lambda n: NOON_UTC - timedelta(days=n)  # noqa: E731
    raw_task(con, ws, "wait 6", "waiting", days(6))
    raw_task(con, ws, "wait 9", "waiting", days(9))
    raw_task(con, ws, "block 1", "blocked", days(1))
    raw_task(con, ws, "block 4", "blocked", days(4))
    new_task(con, ws, title="late 2", due_date="2099-09-29")  # 2 days overdue
    new_task(con, ws, title="late 9", due_date="2099-09-22")  # 9 days overdue
    new_task(con, ws, title="done late", due_date="2099-09-01", status="done")
    items = views.needs_attention(con, cfg)
    assert [(i["title"], i["reason"], i["age_days"]) for i in items] == [
        ("late 9", "overdue", 9),
        ("late 2", "overdue", 2),
        ("block 4", "blocked", 4),
        ("block 1", "blocked", 1),
        ("wait 9", "waiting", 9),
        ("wait 6", "waiting", 6),
    ]


def test_overdue_blocked_task_is_listed_once_as_overdue(con, cfg, ws, at):
    at(NOON_UTC)
    new_task(con, ws, due_date="2099-09-29", status="blocked", blocked_reason="no permit")
    assert [(i["reason"], i["age_days"]) for i in views.needs_attention(con, cfg)] == [
        ("overdue", 2)
    ]


def test_progress_empty_workstream(con, ws):
    workstreams.create(con, WEB, {"name": "Archived", "archived": True})
    [row] = views.workstream_progress(con)
    assert row == {"id": ws, "name": "Vendors", "done": 0, "total": 0, "pct": 0}


def test_progress_counts_live_tasks(con, ws):
    new_task(con, ws, status="done")
    new_task(con, ws, title="b", status="done")
    gone = new_task(con, ws, title="c")
    new_task(con, ws, title="d")
    tasks.delete(con, WEB, gone["id"])
    [row] = views.workstream_progress(con)
    assert (row["done"], row["total"], row["pct"]) == (2, 3, 67)


def test_countdown_labels():
    assert views.countdown(EVENT_DATE, date(2099, 9, 30)) == {"days": 10, "label": "10 days"}
    assert views.countdown(EVENT_DATE, date(2099, 10, 9)) == {"days": 1, "label": "Tomorrow"}
    assert views.countdown(EVENT_DATE, EVENT_DATE) == {"days": 0, "label": "Today!"}


def test_countdown_accepts_config(cfg):
    assert views.countdown(cfg, date(2099, 10, 8))["label"] == "2 days"


def test_countdown_after_event():
    result = views.countdown(EVENT_DATE, EVENT_DATE + timedelta(days=3))
    assert result["label"] == "Event day passed"
    assert result["days"] == 0


def test_digest_shape(con, cfg, ws, at):
    at(NOON_UTC)
    new_task(con, ws, due_date="2099-10-03")
    digest = views.build_digest(con, cfg)
    assert set(digest) == {
        "event",
        "countdown",
        "needs_attention",
        "due_this_week",
        "inbox_count",
        "workstreams",
    }
    assert digest["event"]["name"] == "Example Fall Festival"
    assert digest["countdown"] == {"days": 9, "label": "9 days"}
    assert digest["inbox_count"] == 0
    assert len(digest["due_this_week"]) == 1
    assert digest["workstreams"][0]["total"] == 1


def test_digest_is_json_serializable(con, cfg, ws, at):
    import json

    at(NOON_UTC)
    new_task(con, ws, due_date="2099-09-01")
    json.dumps(views.build_digest(con, cfg))


def test_views_do_not_write(con, cfg, ws, at):
    at(NOON_UTC)
    new_task(con, ws, due_date="2099-09-01")
    before = con.total_changes
    views.build_digest(con, cfg)
    assert con.total_changes == before
