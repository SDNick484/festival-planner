"""The email body retention purge (spec 4.2, Task 17): bodies go after the window."""

from datetime import date

import pytest
from access_keys import make_config

from festival_planner import db, worker
from festival_planner.mail.parse import ParsedEmail
from festival_planner.service import activity, emails

# Event 2099-10-10 with the default 30-day retention: the window closes after 2099-11-09.
EVENT_DATE = date(2099, 10, 10)
LAST_DAY_IN_WINDOW = date(2099, 11, 9)
FIRST_DAY_PURGED = date(2099, 11, 10)


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path)


@pytest.fixture
def con(tmp_path):
    con = db.connect(tmp_path / "test.db")
    db.migrate(con)
    con.execute(
        "INSERT INTO event (id, name, event_date, timezone)"
        " VALUES (1, 'Example Fall Festival', '2099-10-10', 'UTC')"
    )
    con.commit()
    yield con
    con.close()


def capture(con, cfg, n: int = 1) -> dict:
    parsed = ParsedEmail(
        message_id=f"<purge-{n}@example.com>",
        thread_key=f"<purge-{n}@example.com>",
        from_addr="rentals@example.org",
        to_addrs=["capture@example.com"],
        cc_addrs=[],
        subject=f"Purge {n}",
        sent_at="2099-09-01T09:30:00Z",
        body_text=f"Body of message {n}.",
        forward_note=f"note {n}",
    )
    return emails.capture(con, cfg, parsed)


def purge_activity(con) -> list[dict]:
    return [r for r in activity.list_activity(con, limit=500) if r["action"] == "purge"]


def test_no_purge_before_window(con, cfg):
    item = capture(con, cfg)
    assert emails.purge_bodies(con, cfg, LAST_DAY_IN_WINDOW) == 0
    assert emails.purge_bodies(con, cfg, EVENT_DATE) == 0
    assert emails.get(con, item["id"])["body_text"] == "Body of message 1."
    assert purge_activity(con) == []


def test_purge_after_window_keeps_metadata(con, cfg):
    item = capture(con, cfg)
    assert emails.purge_bodies(con, cfg, FIRST_DAY_PURGED) == 1

    after = emails.get(con, item["id"])
    assert after["body_text"] is None
    assert after["body_purged_at"] is not None
    assert after["subject"] == "Purge 1"
    assert after["from_addr"] == "rentals@example.org"
    assert after["message_id"] == "<purge-1@example.com>"
    assert after["forward_note"] == "note 1"
    assert after["state"] == "inbox"

    (row,) = purge_activity(con)
    assert row["source"] == "system" and row["actor"] == "system"
    assert row["note"] == "1"
    assert row["entity_type"] == "email_item" and row["entity_id"] == 0


def test_purge_idempotent(con, cfg):
    capture(con, cfg, 1)
    capture(con, cfg, 2)
    assert emails.purge_bodies(con, cfg, FIRST_DAY_PURGED) == 2
    assert emails.purge_bodies(con, cfg, FIRST_DAY_PURGED) == 0
    assert emails.purge_bodies(con, cfg, date(2100, 1, 1)) == 0
    # Only the first run changed anything, so only it is logged.
    (row,) = purge_activity(con)
    assert row["note"] == "2"


def test_window_uses_event_row_date(con, cfg):
    # The event row, not the config, sets the date: moving the event moves the window.
    con.execute("UPDATE event SET event_date = '2099-12-01' WHERE id = 1")
    capture(con, cfg)
    assert emails.purge_bodies(con, cfg, FIRST_DAY_PURGED) == 0
    assert emails.purge_bodies(con, cfg, date(2100, 1, 1)) == 1


def test_registered_as_daily_job():
    jobs = worker.resolve_jobs(worker.DAILY_JOBS)
    assert jobs["purge_bodies"] is emails.purge_bodies
