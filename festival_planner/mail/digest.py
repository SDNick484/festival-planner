"""The weekly digest email (spec 4.4): a plain-text summary to both organizers over SMTP.

`render_digest` turns `views.build_digest` into (subject, body); `send_digest` sends it;
`daily_digest` is the worker's daily job that decides whether today is a digest day.

Scheduling and "at most once a day"
-----------------------------------
The worker already marks a job done for the day (`meta` key ``daily_job:daily_digest``) once it
returns anything but False, and `daily_digest` returns False while the hour has not come. That
marker alone gives once a day, except when the process dies between the send and the mark. So
`send_digest` also records ``meta.last_digest_date`` right after a successful send, and the job
checks it first: a restart in that window does not send twice. Both markers are kept on purpose;
`last_digest_date` is the one that is about the email, the worker's is about the job.

The digest carries counts and task titles only. Email subjects, bodies and forward notes never
appear in it (the inbox is a count with a link).
"""

from __future__ import annotations

import logging
import smtplib
import sqlite3
from collections.abc import Callable
from datetime import date
from email.message import EmailMessage
from typing import Any

from festival_planner import clock, views
from festival_planner.config import Config
from festival_planner.service import meta

log = logging.getLogger(__name__)

LAST_DIGEST_KEY = "last_digest_date"
FINAL_STRETCH_DAYS = 14
EXTRA_DAY_OFFSET = 3  # the final-stretch extra mail goes out this many days after the weekday
DEFAULT_SMTP_PORT = 465  # SMTP over SSL, which is what the default factory speaks
SMTP_TIMEOUT_SECONDS = 30
MAX_TITLE = 80

SmtpFactory = Callable[..., Any]


def _line(text: object, limit: int = MAX_TITLE) -> str:
    """One line of text: all whitespace (newlines included) collapsed, and cut to `limit`."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _reason(item: dict) -> str:
    reason, age = item["reason"], item["age_days"]
    return reason if reason == "blocked" and not age else f"{reason} {age}d"


def _link(base_url: str, path: str) -> str:
    return f"  {base_url.rstrip('/')}/#/{path}" if base_url else ""


def render_digest(digest: dict, base_url: str) -> tuple[str, str]:
    """`(subject, plain_text_body)` for a `views.build_digest` result.

    Task lines link to ``<base_url>/#/task/<id>``; with an empty `base_url` there are no links.
    """
    attention, due = digest["needs_attention"], digest["due_this_week"]
    name = _line(digest["event"]["name"], 120)
    subject = f"{name}: {digest['countdown']['label']}, {len(attention)} need attention"

    out = [f"{name}: {digest['countdown']['label']}", ""]

    out.append(f"Needs attention ({len(attention)})")
    for item in attention:
        out.append(f"- {_line(item['title'])} [{_reason(item)}]")
        if base_url:
            out.append(_link(base_url, f"task/{item['id']}"))
    if not attention:
        out.append("- Nothing right now.")

    out += ["", f"Due this week ({len(due)})"]
    for item in due:
        out.append(f"- {item['effective_due']}  {_line(item['title'])}")
        if base_url:
            out.append(_link(base_url, f"task/{item['id']}"))
    if not due:
        out.append("- Nothing due in the next 7 days.")

    count = digest["inbox_count"]
    out += ["", "Inbox"]
    out.append(f"- {count} email{'' if count == 1 else 's'} waiting to be filed.")
    if base_url:
        out.append(_link(base_url, "inbox"))

    out += ["", "Progress"]
    for ws in digest["workstreams"]:
        out.append(f"- {_line(ws['name'])}: {ws['done']}/{ws['total']} done ({ws['pct']}%)")
    if not digest["workstreams"]:
        out.append("- No workstreams yet.")

    if base_url:
        out += ["", f"Open the planner: {base_url.rstrip('/')}/"]
    return subject, "\n".join(out) + "\n"


def recipients(cfg: Config) -> list[str]:
    """The organizers' primary (login) addresses, never their aliases."""
    return list(dict.fromkeys(o.email for o in cfg.organizers))


def send_digest(
    cfg: Config,
    con: sqlite3.Connection,
    smtp_factory: SmtpFactory = smtplib.SMTP_SSL,
) -> bool:
    """Build and send one digest message to all organizers' primary emails.

    Returns False, sending nothing, when SMTP or the recipients are not configured. A transport
    error propagates (the worker logs its type and retries next tick). On success it records
    `meta.last_digest_date` as today (event timezone). `smtp_factory(host, port, timeout=...)`
    must return a context manager with `login` and `send_message` (tests inject a fake).

    The SMTP login uses `imap_user` and `SMTP_PASSWORD` (the same account, spec 4.4); with either
    unset the server is used without logging in, for a relay that does not need it.
    """
    to = recipients(cfg)
    if not cfg.smtp_host or not to:
        return False
    subject, body = render_digest(views.build_digest(con, cfg), cfg.base_url or "")
    msg = EmailMessage()
    msg["From"] = cfg.imap_user or to[0]
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    with smtp_factory(
        cfg.smtp_host, cfg.smtp_port or DEFAULT_SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS
    ) as smtp:
        if cfg.imap_user and cfg.smtp_password:
            smtp.login(cfg.imap_user, cfg.smtp_password)
        smtp.send_message(msg)
    meta.set(con, LAST_DIGEST_KEY, clock.today(cfg).isoformat())
    return True


def is_digest_day(cfg: Config, today: date, event_date: date) -> bool:
    """The configured weekday; plus, in the final stretch, the weekday three days later.

    The final stretch is the 14 days up to and including the event day. Nothing is sent after it.
    """
    weekday = cfg.digest_weekday
    if weekday is None:
        return False
    if today.weekday() == weekday:
        return event_date >= today
    extra = (weekday + EXTRA_DAY_OFFSET) % 7
    days_left = (event_date - today).days
    return (
        cfg.digest_final_stretch
        and today.weekday() == extra
        and 0 <= days_left <= FINAL_STRETCH_DAYS
    )


def daily_digest(
    con: sqlite3.Connection,
    cfg: Config,
    today: date,
    smtp_factory: SmtpFactory = smtplib.SMTP_SSL,
) -> bool | None:
    """The worker's daily job: send the digest if today is a digest day and the hour has come.

    Returns False when it is a digest day but before `digest_hour` (event timezone), so the
    worker calls again on its next tick; otherwise None or True, which the worker marks done.
    """
    if cfg.digest_weekday is None:
        return None
    extra = (cfg.digest_weekday + EXTRA_DAY_OFFSET) % 7
    if today.weekday() not in (cfg.digest_weekday, extra):
        return None  # cheap exit on the other five days: no queries
    event_date = date.fromisoformat(views.build_digest(con, cfg)["event"]["event_date"])
    if not is_digest_day(cfg, today, event_date):
        return None
    if clock.now().astimezone(cfg.timezone).hour < cfg.digest_hour:
        return False
    if meta.get(con, LAST_DIGEST_KEY) == today.isoformat():
        return True  # already sent today (the worker's own marker was lost); never twice
    if not cfg.smtp_host:
        log.warning("weekly digest is due but email.smtp_host is not set; skipping today")
        return None
    return send_digest(cfg, con, smtp_factory)
