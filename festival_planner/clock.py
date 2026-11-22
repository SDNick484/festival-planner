"""The single source of "now" and "today".

"Today" is always the calendar date in the event timezone (`Config.timezone`), never server local
time or UTC, so a task due today is not overdue at 23:30 local time even after UTC has rolled over.
Tests patch `clock.now`; everything that needs today's date calls `clock.today(cfg)`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from festival_planner.config import Config


def now() -> datetime:
    """The current moment as an aware UTC datetime. Patch this in tests."""
    return datetime.now(UTC)


def today(cfg: Config) -> date:
    """Today's date in the event timezone. A naive datetime from `now` is taken as UTC."""
    moment = now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(cfg.timezone).date()
