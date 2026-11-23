"""The IMAP poller against a fake client that mimics `imaplib.IMAP4_SSL`'s interface.

The fake records the IMAP command each call would put on the wire. `select(readonly=True)` is how
imaplib sends EXAMINE, so the fake records it as EXAMINE and a plain `select()` as SELECT.
"""

import imaplib
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from access_keys import ORGANIZER_B, make_config

from festival_planner import clock, db
from festival_planner.mail import imap
from festival_planner.service import emails
from festival_planner.web import create_app

FIXTURES = Path(__file__).parent / "fixtures" / "mail"
PASSWORD = "app-password-do-not-log-7Qx"  # noqa: S105 (a fake, asserted absent from logs)
POLL_AT = datetime(2099, 10, 1, 12, 0, tzinfo=UTC)


def raw(n: int, body: str = "", subject: str = "") -> bytes:
    return (
        f"Message-ID: <poll-{n}@example.com>\r\n"
        f"From: {ORGANIZER_B}\r\n"
        "To: capture@example.com\r\n"
        f"Subject: {subject or f'Message {n}'}\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n\r\n'
        f"{body or f'Body {n}.'}\r\n"
    ).encode()


class FakeServer:
    """One mailbox label: UID -> raw message, plus a UIDVALIDITY. Shared by every session."""

    def __init__(self, messages: dict[int, bytes], uidvalidity: int = 7):
        self.messages = dict(messages)
        self.uidvalidity = uidvalidity
        self.commands: list[tuple] = []
        self.login_error: Exception | None = None

    def factory(self, host, timeout=None):
        assert host == "imap.example.com" and timeout
        return FakeClient(self)


class FakeClient:
    def __init__(self, server: FakeServer):
        self.server = server
        self._untagged: dict[str, list] = {}

    def _record(self, *command):
        self.server.commands.append(command)

    def login(self, user, password):
        self._record("LOGIN", user)  # never record the password
        if self.server.login_error:
            raise self.server.login_error
        return "OK", [b"logged in"]

    def select(self, mailbox="INBOX", readonly=False):
        self._record("EXAMINE" if readonly else "SELECT", mailbox)
        self._untagged = {"UIDVALIDITY": [str(self.server.uidvalidity).encode()]}
        return "OK", [str(len(self.server.messages)).encode()]

    def response(self, code):
        return code, self._untagged.pop(code, [None])

    def uid(self, command, *args):
        self._record("UID " + command.upper(), *args)
        assert command.upper() == "FETCH"
        first = int(args[0].split(":")[0])
        uids = sorted(u for u in self.server.messages if u >= first)
        if not uids and self.server.messages:
            uids = [max(self.server.messages)]  # RFC 3501: N:* always includes the highest UID
        if not uids:
            return "OK", [None]
        data: list = []
        for seq, uid in enumerate(uids, start=1):
            body = self.server.messages[uid]
            if body is None:  # a garbled server response: no literal at all
                data.append(f"{seq} (UID {uid} BODY[] NIL)".encode())
                continue
            data.append((f"{seq} (UID {uid} BODY[] {{{len(body)}}}".encode(), body))
            data.append(b")")
        return "OK", data

    def store(self, *args):
        self._record("STORE", *args)

    def expunge(self):
        self._record("EXPUNGE")

    def copy(self, *args):
        self._record("COPY", *args)

    def logout(self):
        self._record("LOGOUT")
        return "BYE", [b""]


@pytest.fixture
def cfg(tmp_path):
    return make_config(
        tmp_path,
        imap_host="imap.example.com",
        imap_user="planner@example.com",
        imap_password=PASSWORD,
        imap_label="Festival Planner",
    )


@pytest.fixture
def con(tmp_path):
    con = db.connect(tmp_path / db.DB_FILENAME)
    db.migrate(con)
    yield con
    con.close()


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: POLL_AT)


def names(server: FakeServer) -> list[str]:
    return [c[0] for c in server.commands]


def test_poll_uses_examine_and_peek(con, cfg):
    server = FakeServer({1: raw(1), 2: raw(2)})
    result = imap.poll_once(con, cfg, client_factory=server.factory)
    assert result == imap.PollResult(fetched=2, captured=2, errors=0)
    assert names(server) == ["LOGIN", "EXAMINE", "UID FETCH", "LOGOUT"]
    assert server.commands[1] == ("EXAMINE", '"Festival Planner"')
    assert server.commands[2] == ("UID FETCH", "1:*", "(BODY.PEEK[])")

    # Nothing new: the fetch starts after the last UID, and the server's N:* quirk (it returns
    # the highest message even below N) does not re-process anything.
    assert imap.poll_once(con, cfg, client_factory=server.factory) == imap.PollResult(0, 0, 0)
    assert server.commands[-2] == ("UID FETCH", "3:*", "(BODY.PEEK[])")

    server.messages[5] = raw(5)
    assert imap.poll_once(con, cfg, client_factory=server.factory).captured == 1
    assert server.commands[-2] == ("UID FETCH", "3:*", "(BODY.PEEK[])")
    assert not {"SELECT", "STORE", "EXPUNGE", "COPY"} & set(names(server))
    assert emails.get_imap_state(con, "Festival Planner") == (7, 5)


def test_poll_empty_mailbox(con, cfg):
    server = FakeServer({})
    assert imap.poll_once(con, cfg, client_factory=server.factory) == imap.PollResult(0, 0, 0)
    assert emails.get_imap_state(con, "Festival Planner") == (7, 0)


def test_uidvalidity_change_resets(con, cfg):
    server = FakeServer({1: raw(1), 2: raw(2)}, uidvalidity=7)
    imap.poll_once(con, cfg, client_factory=server.factory)
    assert emails.get_imap_state(con, "Festival Planner") == (7, 2)

    # The label was rebuilt: new UIDVALIDITY, messages renumbered, one of them new.
    server.messages = {1: raw(2), 2: raw(1), 3: raw(3)}
    server.uidvalidity = 8
    result = imap.poll_once(con, cfg, client_factory=server.factory)
    assert server.commands[-2] == ("UID FETCH", "1:*", "(BODY.PEEK[])")
    assert result == imap.PollResult(fetched=3, captured=1, errors=0)  # dedup by Message-ID
    assert emails.get_imap_state(con, "Festival Planner") == (8, 3)
    assert con.execute("SELECT COUNT(*) FROM email_item").fetchone()[0] == 3


def test_poll_continues_after_bad_message(con, cfg, monkeypatch, caplog):
    """RF2: hostile or odd mail is logged and skipped, and never stops the poll."""
    body_marker = "PRIVATE-BODY-TEXT-1234"
    real_parse = imap.parse_message

    def parse(data, **kwargs):
        if b"EXPLODE" in data:
            raise ValueError(f"parser bug near {body_marker}")
        return real_parse(data, **kwargs)

    monkeypatch.setattr(imap, "parse_message", parse)
    huge = raw(9, body=("A dull festival plan. " * 250_000))  # about 5 MB
    server = FakeServer(
        {
            1: raw(1),
            2: raw(2, body=f"EXPLODE {body_marker}"),
            3: (FIXTURES / "latin1_no_charset.eml").read_bytes(),
            4: None,  # garbled FETCH response with no message literal
            5: (FIXTURES / "missing_message_id.eml").read_bytes(),
            6: huge,
            7: (FIXTURES / "html_only.eml").read_bytes(),
            8: b"\x00\xff not a mime message at all \xfe",
        }
    )
    with caplog.at_level(logging.INFO, logger="festival_planner"):
        result = imap.poll_once(con, cfg, client_factory=server.factory)
    assert result == imap.PollResult(fetched=8, captured=6, errors=2)
    assert emails.get_imap_state(con, "Festival Planner") == (7, 8)
    assert con.execute("SELECT COUNT(*) FROM email_item").fetchone()[0] == 6
    assert re.search(r"uid 2\b.*ValueError", caplog.text)
    assert re.search(r"uid 4\b", caplog.text)
    assert body_marker not in caplog.text
    capped = con.execute("SELECT body_text FROM email_item WHERE message_id LIKE '<poll-9@%'")
    assert capped.fetchone()[0].endswith("[truncated]")
    # The poll was still a success.
    assert emails.last_email_poll(con) == "2099-10-01T12:00:00Z"


def test_poll_logs_redact_password(con, cfg, caplog):
    server = FakeServer({1: raw(1)})
    server.login_error = imaplib.IMAP4.error(f"LOGIN failed for planner@example.com {PASSWORD}")
    with caplog.at_level(logging.DEBUG), pytest.raises(imap.PollError) as exc:
        imap.poll_once(con, cfg, client_factory=server.factory)
    assert PASSWORD not in str(exc.value)
    assert "<redacted>" in str(exc.value)
    # The original exception (which carries the password) is not chained into tracebacks.
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
    assert PASSWORD not in caplog.text
    assert names(server)[-1] == "LOGOUT"
    assert emails.last_email_poll(con) is None  # a failed poll is not a successful one

    # A connection error (no session at all) is redacted the same way.
    def refuse(host, timeout=None):
        raise OSError(f"connection refused ({PASSWORD})")

    with caplog.at_level(logging.DEBUG), pytest.raises(imap.PollError) as exc:
        imap.poll_once(con, cfg, client_factory=refuse)
    assert PASSWORD not in str(exc.value) and PASSWORD not in caplog.text


def test_poll_requires_configuration(con, cfg, tmp_path):
    with pytest.raises(imap.PollError):
        imap.poll_once(con, make_config(tmp_path), client_factory=FakeServer({}).factory)


def test_healthz_reports_last_poll(cfg, tmp_path):
    app = create_app(cfg)
    client = app.test_client()
    assert client.get("/healthz").json == {"ok": True, "last_email_poll": None}

    con = db.connect(tmp_path / db.DB_FILENAME)
    try:
        imap.poll_once(con, cfg, client_factory=FakeServer({1: raw(1)}).factory)
    finally:
        con.close()
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json == {"ok": True, "last_email_poll": "2099-10-01T12:00:00Z"}
