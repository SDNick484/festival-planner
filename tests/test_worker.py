"""The worker loop: backoff, daily jobs and SIGTERM, with injected waits (no real sleeping)."""

import logging
import signal
from datetime import UTC, datetime

import pytest
from access_keys import make_config

from festival_planner import clock, db, worker
from festival_planner.mail import imap
from festival_planner.service import meta

PASSWORD = "app-password-do-not-log-7Qx"  # noqa: S105 (a fake, asserted absent from logs)


@pytest.fixture
def cfg(tmp_path):
    return make_config(
        tmp_path,
        imap_host="imap.example.com",
        imap_user="planner@example.com",
        imap_password=PASSWORD,
        imap_label="Festival",
        email_poll_seconds=300,
    )


@pytest.fixture
def now(monkeypatch):
    state = {"at": datetime(2099, 10, 1, 12, 0, tzinfo=UTC)}
    monkeypatch.setattr(clock, "now", lambda: state["at"])
    return state


class Polls:
    """A fake poll_once: each call pops the next outcome (an exception or a PollResult)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, con, cfg):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else imap.PollResult(0, 0, 0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_backoff_delays():
    assert worker.next_delay(300, 0) == 300
    assert [worker.next_delay(300, n) for n in range(1, 6)] == [600, 1200, 1800, 1800, 1800]
    assert worker.next_delay(60, 3) == 480
    assert worker.next_delay(3600, 2) == 3600  # never shorter than the poll interval
    assert worker.next_delay(300, 10_000) == 1800  # no overflow


def test_tick_backs_off_and_recovers(cfg, now):
    polls = Polls(
        imap.PollError("down"),
        imap.PollError("down"),
        imap.PollError("down"),
        imap.PollError("down"),
        imap.PollResult(1, 1, 0),
    )
    w = worker.Worker(cfg, poll=polls, jobs={})
    assert [w.tick() for _ in range(6)] == [600, 1200, 1800, 1800, 300, 300]


def test_tick_survives_unexpected_poll_error(cfg, now, caplog):
    polls = Polls(RuntimeError(f"boom {PASSWORD}"))
    w = worker.Worker(cfg, poll=polls, jobs={})
    with caplog.at_level(logging.INFO, logger="festival_planner"):
        assert w.tick() == 600
    assert "RuntimeError" in caplog.text and PASSWORD not in caplog.text


def test_polling_off_without_imap_config(tmp_path, now):
    polls = Polls()
    ran = []
    w = worker.Worker(make_config(tmp_path), poll=polls, jobs={"j": lambda c, f, d: ran.append(d)})
    assert w.tick() == 300
    assert polls.calls == 0 and len(ran) == 1


def test_daily_jobs_run_once_per_day(cfg, now):
    ran: list = []
    retried: list = []
    broken: list = []

    def job(con, cfg, today):
        ran.append(today)

    def not_yet(con, cfg, today):  # e.g. a digest waiting for its hour
        retried.append(today)
        return False

    def flaky(con, cfg, today):
        broken.append(today)
        raise RuntimeError(f"smtp said {PASSWORD}")

    w = worker.Worker(cfg, poll=Polls(), jobs={"job": job, "not_yet": not_yet, "flaky": flaky})
    w.tick()
    w.tick()
    assert len(ran) == 1 and len(retried) == 2 and len(broken) == 2
    now["at"] = datetime(2099, 10, 2, 0, 5, tzinfo=UTC)
    w.tick()
    assert [d.isoformat() for d in ran] == ["2099-10-01", "2099-10-02"]
    con = db.connect(cfg.data_dir / db.DB_FILENAME)
    try:
        assert meta.get(con, "daily_job:job") == "2099-10-02"
        assert meta.get(con, "daily_job:flaky") is None
    finally:
        con.close()


def test_daily_job_registry():
    jobs = {}
    calls = []

    def first(con, cfg, today):
        calls.append("first")

    worker.register_daily(first, registry=jobs)
    worker.register_daily("festival_planner.service.meta:get", name="by_path", registry=jobs)
    assert list(jobs) == ["first", "by_path"]
    assert worker.resolve_jobs(jobs)["by_path"] is meta.get
    with pytest.raises(ValueError):
        worker.register_daily(first, registry=jobs)  # one name, one job
    assert worker.daily_job(first) is first and worker.DAILY_JOBS.pop("first") is first


def test_sigterm_during_wait_exits_cleanly(cfg, now):
    waits: list[float] = []
    polls = Polls()
    before = signal.getsignal(signal.SIGTERM)

    def wait(seconds):
        waits.append(seconds)
        signal.raise_signal(signal.SIGTERM)
        return False

    assert worker.main(config=cfg, wait=wait, poll=polls) == 0
    assert waits == [300] and polls.calls == 1
    assert signal.getsignal(signal.SIGTERM) is before


def test_sigterm_during_poll_skips_the_wait(cfg, now):
    waits: list[float] = []

    def poll(con, cfg):
        signal.raise_signal(signal.SIGTERM)
        return imap.PollResult(0, 0, 0)

    assert worker.main(config=cfg, wait=waits.append, poll=poll) == 0
    assert waits == []


def test_worker_migrates_database(cfg, now):
    def wait(seconds):
        signal.raise_signal(signal.SIGTERM)

    worker.main(config=cfg, wait=wait, poll=Polls())
    con = db.connect(cfg.data_dir / db.DB_FILENAME)
    try:
        assert meta.get(con, "missing") is None  # the meta table exists
    finally:
        con.close()
