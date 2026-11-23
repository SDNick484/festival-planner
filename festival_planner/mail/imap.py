"""Read-only IMAP poller for the capture label (spec 4.2).

`poll_once` opens the label with EXAMINE (imaplib's `select(readonly=True)`), fetches only UIDs
above the last one handled with `BODY.PEEK[]` (which does not set \\Seen), and never sends SELECT,
STORE, EXPUNGE, COPY or MOVE. The mailbox is never modified.

Failure handling:
- Each message is parsed and captured in its own try/except. A failure logs the UID and the
  exception type only (never the body, addresses or credentials), counts as an error and is
  skipped for good: one bad message never stops the poll or blocks the ones after it.
- A database that is busy or broken (`sqlite3.OperationalError`) is not the message's fault: the
  poll stops there, and that UID is fetched again next time.
- Connection, login and protocol errors raise `PollError`, whose message has the IMAP password
  redacted and which does not chain the original exception (its text could hold the password).
- `last_uid` is saved after every message, so progress survives a crash mid-poll.
"""

from __future__ import annotations

import imaplib
import logging
import re
import sqlite3
from collections.abc import Callable
from typing import Any, NamedTuple

from festival_planner import clock
from festival_planner.config import Config
from festival_planner.mail.parse import parse_message
from festival_planner.service import emails

log = logging.getLogger(__name__)

DEFAULT_LABEL = "INBOX"
TIMEOUT_SECONDS = 60
REDACTED = "<redacted>"
_UID_RE = re.compile(rb"\bUID (\d+)")


class PollResult(NamedTuple):
    fetched: int
    captured: int
    errors: int


class PollError(Exception):
    """The poll could not run (configuration, connection, login or protocol). Already redacted."""


def redact(text: str, cfg: Config) -> str:
    """`text` with every configured secret replaced (IMAP and SMTP passwords, token secrets)."""
    secrets = [cfg.imap_password, cfg.smtp_password, *(t.secret for t in cfg.mcp_tokens)]
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


def _quote(mailbox: str) -> str:
    """An IMAP quoted string (Gmail labels often contain spaces)."""
    return '"' + mailbox.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _uidvalidity(client: Any) -> int | None:
    _, data = client.response("UIDVALIDITY")
    try:
        return int(data[0])
    except (TypeError, ValueError, IndexError):
        return None


def _messages(data: list) -> list[tuple[int | None, bytes | None]]:
    """(uid, raw) pairs from a `UID FETCH ... (BODY.PEEK[])` response, in server order.

    imaplib returns a tuple (header, literal) per message, then a b")" closer. A bytes item that
    names a UID but carries no literal is a garbled response: it is kept with raw=None so the
    caller counts it as an error and moves past it.
    """
    out: list[tuple[int | None, bytes | None]] = []
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2:
            header, raw = item[0], item[1]
        elif isinstance(item, bytes):
            header, raw = item, None
        else:
            continue
        match = _UID_RE.search(header if isinstance(header, bytes) else b"")
        if match is None and raw is None:
            continue  # the ")" closer, or noise
        uid = int(match.group(1)) if match else None
        out.append((uid, raw if isinstance(raw, bytes) else None))
    return out


def poll_once(
    con: sqlite3.Connection,
    cfg: Config,
    client_factory: Callable[..., Any] = imaplib.IMAP4_SSL,
) -> PollResult:
    """Fetch and capture new messages from the configured label. See the module docstring."""
    if not (cfg.imap_host and cfg.imap_user and cfg.imap_password):
        raise PollError("email capture is not configured (imap_host, imap_user, IMAP_PASSWORD)")
    label = cfg.imap_label or DEFAULT_LABEL
    client = None
    try:
        client = client_factory(cfg.imap_host, timeout=TIMEOUT_SECONDS)
        client.login(cfg.imap_user, cfg.imap_password)
        typ, _ = client.select(_quote(label), readonly=True)  # sends EXAMINE
        if typ != "OK":
            raise PollError(f"cannot open label {label!r} read-only")
        uidvalidity = _uidvalidity(client)
        known_validity, last_uid = emails.get_imap_state(con, label)
        if known_validity is not None and uidvalidity != known_validity:
            log.info("label UIDVALIDITY changed; rescanning (duplicates are ignored)")
            last_uid = 0
        typ, data = client.uid("FETCH", f"{last_uid + 1}:*", "(BODY.PEEK[])")
        if typ != "OK":
            raise PollError("UID FETCH failed")
        result = _capture_all(con, cfg, label, uidvalidity, last_uid, _messages(data))
    except PollError:
        raise
    except sqlite3.Error:
        raise  # not an IMAP problem; the worker logs it and backs off
    except Exception as exc:  # connection, TLS, login, protocol
        message = redact(f"IMAP poll failed: {type(exc).__name__}: {exc}", cfg)
        raise PollError(message) from None
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:  # noqa: S110 (best effort; the session may already be gone)
                pass
    emails.record_poll(con, clock.now().strftime("%Y-%m-%dT%H:%M:%SZ"))
    log.info(
        "email poll: fetched %d, captured %d, errors %d",
        result.fetched,
        result.captured,
        result.errors,
    )
    return result


def _capture_all(
    con: sqlite3.Connection,
    cfg: Config,
    label: str,
    uidvalidity: int | None,
    last_uid: int,
    messages: list[tuple[int | None, bytes | None]],
) -> PollResult:
    fetched = captured = errors = 0
    emails.save_imap_state(con, label, uidvalidity, last_uid)
    # `N:*` always includes the highest UID even when it is below N, so skip what we have seen.
    for uid, raw in sorted(
        ((u, r) for u, r in messages if u is None or u > last_uid), key=lambda m: m[0] or 0
    ):
        fetched += 1
        try:
            if uid is None or raw is None:
                raise ValueError("malformed FETCH response")
            if emails.capture(con, cfg, parse_message(raw)) is not None:
                captured += 1
        except sqlite3.OperationalError:
            raise  # busy or broken database: stop here and retry this UID next poll
        except Exception as exc:
            errors += 1
            # Only the UID and the exception type: the message text could quote the body.
            log.warning("email uid %s skipped: %s", uid, type(exc).__name__)
        if uid is not None and uid > last_uid:
            last_uid = uid
            emails.save_imap_state(con, label, uidvalidity, last_uid)
    return PollResult(fetched, captured, errors)
