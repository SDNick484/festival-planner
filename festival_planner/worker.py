"""The single background process: `python -m festival_planner.worker` (spec 7).

It runs next to the web app, from the same image, as exactly one process (so mail is never polled
twice). Each tick it:

1. polls the capture label once (`mail.imap.poll_once`), if IMAP is configured;
2. runs the daily jobs that have not completed today (today is the event-timezone date);
3. waits `email_poll_seconds`, or longer after failed polls: the wait doubles per consecutive
   failure, up to 30 minutes, and drops back to normal after the next successful poll.

SIGTERM (and SIGINT) set a stop flag: the wait ends at once, the current step finishes, and the
process exits with status 0. Log lines carry no message bodies and no credentials.

Daily jobs
----------
A daily job is a function ``job(con, cfg, today) -> object`` (`today` is a `datetime.date`).
Register one with a single line in the "Daily jobs" block at the end of this module::

    register_daily("festival_planner.service.emails:purge_bodies")

A "module:function" path is imported when the worker starts, so registering needs no import
line; passing the function itself (or decorating it with ``@daily_job``) works too. Each job is
named after its function (or ``name=...``), and names are unique.

- The worker calls each job on every tick until the job completes for the day, then not again
  until the next date. Completion is stored in the `meta` table as ``daily_job:<name>``.
- Returning ``False`` means "not due yet": the job is called again on the next tick that day
  (a job that must wait for an hour of the day, such as the digest, returns False until then).
  Any other return value, including None, marks the job done for today.
- An exception is logged (job name and exception type only) and the job is retried next tick.
- Jobs must be idempotent within a day: a crash between the job and its completion mark means
  it runs again.

Later tasks each have a reserved line in that block (the retention purge, the weekly digest), so
branches that add jobs change different lines and merge cleanly.
"""

from __future__ import annotations

import importlib
import logging
import signal
import sqlite3
import sys
import threading
from collections.abc import Callable
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

from festival_planner import clock, db
from festival_planner.config import Config, ConfigError, load_config
from festival_planner.mail import imap
from festival_planner.service import meta

log = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 30 * 60
DAILY_KEY_PREFIX = "daily_job:"

DailyJob = Callable[[sqlite3.Connection, Config, date], Any]

# name -> the job, or its "module:function" path (resolved when a Worker starts).
DAILY_JOBS: dict[str, DailyJob | str] = {}


def register_daily(
    target: DailyJob | str,
    *,
    name: str | None = None,
    registry: dict[str, DailyJob | str] | None = None,
) -> DailyJob | str:
    """Register a daily job (a function or a "module:function" path). Returns `target`."""
    registry = DAILY_JOBS if registry is None else registry
    if name is None:
        name = target.rpartition(":")[2] if isinstance(target, str) else target.__name__
    if not name or name in registry:
        raise ValueError(f"daily job name must be unique and non-empty: {name!r}")
    registry[name] = target
    return target


def daily_job(fn: DailyJob) -> DailyJob:
    """Decorator form of `register_daily` for a job defined in an already-imported module."""
    register_daily(fn)
    return fn


def resolve_jobs(registry: dict[str, DailyJob | str]) -> dict[str, DailyJob]:
    """Import every "module:function" entry. A bad path fails at startup, not at midnight."""
    jobs: dict[str, DailyJob] = {}
    for name, target in registry.items():
        if isinstance(target, str):
            module, _, attr = target.partition(":")
            target = getattr(importlib.import_module(module), attr)
        if not callable(target):
            raise TypeError(f"daily job {name!r} is not callable")
        jobs[name] = target
    return jobs


def next_delay(poll_seconds: int, failures: int) -> int:
    """Seconds to wait: the poll interval, doubled per consecutive failure, capped at 30 min.

    The cap never shortens the configured interval itself.
    """
    if failures <= 0:
        return poll_seconds
    backoff = poll_seconds * 2 ** min(failures, 32)
    return max(poll_seconds, min(backoff, MAX_BACKOFF_SECONDS))


class Worker:
    def __init__(
        self,
        cfg: Config,
        *,
        poll: Callable[[sqlite3.Connection, Config], Any] = imap.poll_once,
        jobs: dict[str, DailyJob | str] | None = None,
        wait: Callable[[float], Any] | None = None,
    ):
        self.cfg = cfg
        self.stop = threading.Event()
        self.failures = 0
        self.polling = bool(cfg.imap_host and cfg.imap_user and cfg.imap_password)
        self.jobs = resolve_jobs(DAILY_JOBS if jobs is None else jobs)
        self._poll = poll
        self._wait = wait or self.stop.wait
        self._db_path = Path(cfg.data_dir) / db.DB_FILENAME
        with closing(db.connect(self._db_path)) as con:
            db.migrate(con)  # the web app may not have started yet

    def request_stop(self, signum: int | None = None, frame: Any = None) -> None:
        self.stop.set()

    def run(self) -> None:
        if not self.polling:
            log.info("email capture is off (no imap_host, imap_user or IMAP_PASSWORD)")
        while not self.stop.is_set():
            delay = self.tick()
            if self.stop.is_set():
                break
            self._wait(delay)

    def tick(self) -> int:
        """One poll plus the due daily jobs. Returns the seconds to wait before the next tick."""
        with closing(db.connect(self._db_path)) as con:
            if self.polling:
                self._poll_step(con)
            if not self.stop.is_set():
                self._run_daily(con)
        return next_delay(self.cfg.email_poll_seconds, self.failures)

    def _poll_step(self, con: sqlite3.Connection) -> None:
        try:
            self._poll(con, self.cfg)
        except Exception as exc:
            self.failures += 1
            if isinstance(exc, imap.PollError | sqlite3.Error):
                reason = imap.redact(str(exc), self.cfg)  # PollError is redacted already
            else:
                reason = type(exc).__name__  # unknown text could quote a message
            log.warning(
                "email poll failed (%s); next try in %ds",
                reason,
                next_delay(self.cfg.email_poll_seconds, self.failures),
            )
        else:
            self.failures = 0

    def _run_daily(self, con: sqlite3.Connection) -> None:
        today = clock.today(self.cfg)
        stamp = today.isoformat()
        for name, job in self.jobs.items():
            if self.stop.is_set():
                return
            key = DAILY_KEY_PREFIX + name
            if meta.get(con, key) == stamp:
                continue
            try:
                result = job(con, self.cfg, today)
            except Exception as exc:
                if con.in_transaction:
                    con.rollback()
                log.warning(
                    "daily job %s failed (%s); retrying next tick", name, type(exc).__name__
                )
                continue
            if result is not False:
                meta.set(con, key, stamp)
                log.info("daily job %s done for %s", name, stamp)


def main(
    *,
    config: Config | None = None,
    wait: Callable[[float], Any] | None = None,
    poll: Callable[[sqlite3.Connection, Config], Any] = imap.poll_once,
    jobs: dict[str, DailyJob | str] | None = None,
) -> int:
    """Run until SIGTERM or SIGINT. The keyword arguments are for tests."""
    cfg = config if config is not None else load_config()
    worker = Worker(cfg, poll=poll, jobs=jobs, wait=wait)
    handled = (signal.SIGTERM, signal.SIGINT)
    previous = {sig: signal.signal(sig, worker.request_stop) for sig in handled}
    try:
        log.info("worker started: %d daily job(s)", len(worker.jobs))
        worker.run()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    log.info("worker stopped")
    return 0


# --- Daily jobs: one register_daily(...) line each ------------------------------------------
# Each reserved line below is replaced by its task's registration.
register_daily("festival_planner.service.emails:purge_bodies")

# Task 21 (weekly digest): register_daily("festival_planner.mail.digest:<daily entry point>")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        sys.exit(main())
    except ConfigError as exc:
        log.error("invalid configuration: %s", exc)
        sys.exit(2)
