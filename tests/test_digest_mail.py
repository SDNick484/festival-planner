"""The weekly digest email: rendering, recipients and once-a-day scheduling (no sockets)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from access_keys import ORGANIZER_A, ORGANIZER_B, make_config

from festival_planner import clock, db, views, worker
from festival_planner.mail import digest
from festival_planner.mail.parse import ParsedEmail
from festival_planner.service import Actor, emails, meta, tasks, workstreams

WEB = Actor("web", ORGANIZER_A)
BASE_URL = "https://festival.example.com"
EVENT_DATE = date(2099, 10, 10)
MONDAY = datetime(2099, 9, 7, 9, 0, tzinfo=UTC)  # 33 days before the event
assert MONDAY.weekday() == 0
PRIVATE_TEXT = "the-secret-body-text-3f9a"


class FakeSMTP:
    """Stands in for smtplib.SMTP_SSL: records everything, opens no socket."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, **kwargs):
        self.host, self.port, self.kwargs = host, port, kwargs
        self.logins: list[tuple[str, str]] = []
        self.sent: list = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.logins.append((user, password))

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture(autouse=True)
def fake_smtp():
    FakeSMTP.instances = []
    return FakeSMTP


def sent():
    return [m for smtp in FakeSMTP.instances for m in smtp.sent]


def make_cfg(tmp_path, **overrides):
    values = {
        "event_date": EVENT_DATE,
        "digest_weekday": 0,
        "digest_hour": 8,
        "smtp_host": "smtp.example.com",
        "smtp_port": 465,
        "smtp_password": "smtp-pass-for-tests",
        "imap_user": "planner@example.com",
        "base_url": BASE_URL,
    }
    values.update(overrides)
    return make_config(tmp_path, **values)


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture
def con(tmp_path, cfg):
    con = db.connect(tmp_path / "test.db")
    db.migrate(con)
    con.execute(
        "INSERT INTO event (id, name, event_date, timezone) VALUES (1, ?, ?, ?)",
        ("Example Fall Festival", cfg.event_date.isoformat(), "UTC"),
    )
    con.commit()
    yield con
    con.close()


@pytest.fixture
def at(monkeypatch):
    def _set(moment):
        monkeypatch.setattr(clock, "now", lambda: moment)

    return _set


def run(con, cfg, moment, at, **kwargs):
    """What the worker does for the job: call it with today's date in the event timezone."""
    at(moment)
    return digest.daily_digest(con, cfg, clock.today(cfg), smtp_factory=FakeSMTP, **kwargs)


def add_overdue_task(con, title="Book tent"):
    ws = workstreams.create(con, WEB, {"name": "Vendors"})["id"]
    return tasks.create(con, WEB, {"workstream_id": ws, "title": title, "due_date": "2099-09-01"})[
        "id"
    ]


def test_render_digest_subject(con, cfg, at):
    at(MONDAY)
    task_id = add_overdue_task(con)
    subject, body = digest.render_digest(views.build_digest(con, cfg), BASE_URL)
    assert subject == "Example Fall Festival: 33 days, 1 need attention"
    assert f"{BASE_URL}/#/task/{task_id}" in body
    assert "Book tent" in body
    for heading in ("Needs attention", "Due this week", "Inbox", "Progress"):
        assert heading in body
    assert "Vendors: 0/1 done (0%)" in body

    at(datetime(2099, 10, 9, 9, 0, tzinfo=UTC))
    subject, _ = digest.render_digest(views.build_digest(con, cfg), BASE_URL)
    assert subject == "Example Fall Festival: Tomorrow, 1 need attention"


def test_render_without_base_url_has_no_links(con, cfg, at):
    at(MONDAY)
    add_overdue_task(con)
    _, body = digest.render_digest(views.build_digest(con, cfg), "")
    assert "Book tent" in body and "#/task/" not in body


def test_render_excludes_email_bodies(con, cfg, at):
    at(MONDAY)
    emails.capture(
        con,
        cfg,
        ParsedEmail(
            message_id="<digest-1@example.com>",
            thread_key="t1",
            from_addr="vendor@example.com",
            to_addrs=["planner@example.com"],
            cc_addrs=[],
            subject="Subject-line-that-must-stay-out",
            sent_at=None,
            body_text=PRIVATE_TEXT,
            forward_note=PRIVATE_TEXT + "-note",
        ),
    )
    assert emails.inbox_count(con) == 1
    _, body = digest.render_digest(views.build_digest(con, cfg), BASE_URL)
    assert "1 email" in body and f"{BASE_URL}/#/inbox" in body
    assert PRIVATE_TEXT not in body and "Subject-line-that-must-stay-out" not in body
    # Even a digest dict that carries message text renders none of it.
    leaky = {**views.build_digest(con, cfg), "emails": [{"body_text": PRIVATE_TEXT}]}
    assert PRIVATE_TEXT not in digest.render_digest(leaky, BASE_URL)[1]


def test_render_flattens_multiline_titles(con, cfg, at):
    at(MONDAY)
    add_overdue_task(con, "Line one\nBcc: someone@example.net")
    subject, body = digest.render_digest(views.build_digest(con, cfg), BASE_URL)
    assert "\n" not in subject
    assert "Line one Bcc: someone@example.net" in body


def test_send_digest_goes_to_primary_emails_only(con, cfg, at):
    at(MONDAY)
    add_overdue_task(con)
    assert digest.send_digest(cfg, con, smtp_factory=FakeSMTP) is True
    (smtp,) = FakeSMTP.instances
    assert (smtp.host, smtp.port) == ("smtp.example.com", 465)
    assert smtp.logins == [("planner@example.com", "smtp-pass-for-tests")]
    (msg,) = smtp.sent
    assert msg["To"] == f"{ORGANIZER_A}, {ORGANIZER_B}"  # no aliases
    assert msg["From"] == "planner@example.com"
    assert msg["Subject"].startswith("Example Fall Festival: 33 days")
    assert msg.get_content_type() == "text/plain"
    assert meta.get(con, "last_digest_date") == "2099-09-07"


def test_send_digest_false_when_smtp_not_configured(tmp_path, con, at):
    at(MONDAY)
    cfg = make_cfg(tmp_path, smtp_host=None)
    assert digest.send_digest(cfg, con, smtp_factory=FakeSMTP) is False
    assert FakeSMTP.instances == [] and meta.get(con, "last_digest_date") is None


def test_send_failure_propagates_and_is_not_recorded(con, cfg, at):
    at(MONDAY)

    class Down(FakeSMTP):
        def send_message(self, msg):
            raise OSError("connection reset")

    with pytest.raises(OSError, match="connection reset"):
        digest.send_digest(cfg, con, smtp_factory=Down)
    assert meta.get(con, "last_digest_date") is None


def test_sends_once_per_day(con, cfg, at):
    assert run(con, cfg, MONDAY, at) is True
    assert len(sent()) == 1
    # A second call the same day, even if the worker's own marker was lost (a crash between the
    # send and the mark), does not send again.
    assert run(con, cfg, MONDAY + timedelta(hours=3), at) is True
    assert len(sent()) == 1
    # Through the worker: its marker and meta.last_digest_date agree, so ticks send nothing more.
    jobs = {"daily_digest": digest.daily_digest}
    w = worker.Worker(cfg, jobs=jobs, wait=lambda s: None)
    at(MONDAY + timedelta(hours=4))
    w.tick()
    w.tick()
    assert len(sent()) == 1
    # The next scheduled weekday sends again.
    assert run(con, cfg, MONDAY + timedelta(days=7), at) is True
    assert len(sent()) == 2
    assert meta.get(con, "last_digest_date") == "2099-09-14"


def test_waits_for_digest_hour(con, cfg, at):
    early = MONDAY.replace(hour=7)
    assert run(con, cfg, early, at) is False  # the worker retries on its next tick
    assert sent() == []
    assert run(con, cfg, early.replace(hour=8), at) is True
    assert len(sent()) == 1


def test_hour_is_in_the_event_timezone(tmp_path, at):
    from zoneinfo import ZoneInfo

    cfg = make_cfg(tmp_path, timezone=ZoneInfo("Pacific/Auckland"), digest_hour=8)  # UTC+12/13
    con = db.connect(tmp_path / "tz.db")
    db.migrate(con)
    # 20:00 UTC on Sunday is 08:00/09:00 on Monday in Auckland.
    assert run(con, cfg, datetime(2099, 9, 6, 20, 0, tzinfo=UTC), at) is True
    assert len(sent()) == 1
    con.close()


def test_other_weekdays_do_nothing(con, cfg, at):
    tuesday = MONDAY + timedelta(days=1)
    assert run(con, cfg, tuesday, at) is None  # not False: nothing to retry today
    assert sent() == [] and meta.get(con, "last_digest_date") is None


def test_disabled_when_weekday_none(tmp_path, con, at):
    cfg = make_cfg(tmp_path, digest_weekday=None, digest_final_stretch=True)
    for day in range(7):
        assert run(con, cfg, MONDAY + timedelta(days=day), at) is None
    assert sent() == []


def set_event_date(con, day):
    con.execute("UPDATE event SET event_date = ?", (day.isoformat(),))
    con.commit()


def test_final_stretch_extra_day(tmp_path, con, at):
    cfg = make_cfg(tmp_path, digest_final_stretch=True)
    thursday = datetime(2099, 9, 24, 9, 0, tzinfo=UTC)  # (digest_weekday + 3) % 7
    assert thursday.weekday() == 3

    set_event_date(con, date(2099, 10, 9))  # 15 days out: not yet in the final stretch
    assert run(con, cfg, thursday, at) is None
    assert sent() == []

    set_event_date(con, date(2099, 10, 8))  # exactly 14 days out: the extra day sends
    assert run(con, cfg, thursday, at) is True
    assert len(sent()) == 1

    # The switch off: a Thursday inside the window sends nothing.
    off = make_cfg(tmp_path, digest_final_stretch=False)
    assert run(con, off, thursday + timedelta(days=7), at) is None
    assert len(sent()) == 1

    # The switch on: the regular weekday still sends, and so does the extra day.
    assert run(con, cfg, datetime(2099, 9, 28, 9, 0, tzinfo=UTC), at) is True  # Monday
    assert run(con, cfg, thursday + timedelta(days=7), at) is True  # Thursday, 7 days out
    assert len(sent()) == 3


def test_final_stretch_ends_with_the_event(tmp_path, con, at):
    cfg = make_cfg(tmp_path, digest_final_stretch=True)
    set_event_date(con, date(2099, 10, 8))
    after = datetime(2099, 10, 12, 9, 0, tzinfo=UTC)  # the Monday after the event
    assert after.weekday() == 0
    assert run(con, cfg, after, at) is None
    assert sent() == []


def test_unconfigured_smtp_is_skipped_quietly(tmp_path, con, at):
    cfg = make_cfg(tmp_path, smtp_host=None)
    assert run(con, cfg, MONDAY, at) is None  # done for today: nothing to retry until a restart
    assert sent() == []


def test_daily_digest_is_registered_with_the_worker():
    jobs = worker.resolve_jobs(worker.DAILY_JOBS)
    assert jobs["daily_digest"] is digest.daily_digest
