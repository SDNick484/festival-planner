import random
import sqlite3
from pathlib import Path

import pytest

from festival_planner import db
from festival_planner.mail.parse import ParsedEmail, parse_message

FIXTURES = Path(__file__).parent / "fixtures" / "mail"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def huge_message(size: int = 5 * 1024 * 1024) -> bytes:
    """A message with a `size`-byte plain-text body. Generated here, not committed (5 MB)."""
    head = (
        b"Message-ID: <huge-001@example.com>\n"
        b"Date: Mon, 13 Jan 2098 16:00:00 +0000\n"
        b"From: organizer-b@example.com\n"
        b"To: organizer-a@example.com\n"
        b"Subject: Very long message\n"
        b"MIME-Version: 1.0\n"
        b'Content-Type: text/plain; charset="utf-8"\n'
        b"Content-Transfer-Encoding: 7bit\n\n"
    )
    line = b"All work and no play makes a dull festival plan. " * 2 + b"\n"
    body = (line * (size // len(line) + 1))[:size]
    return head + body


def test_parse_plain():
    p = parse_message(load("plain.eml"))
    assert isinstance(p, ParsedEmail)
    assert p.message_id == "<plain-001@example.com>"
    assert p.thread_key == "<plain-001@example.com>"
    assert p.from_addr == "organizer-b@example.com"
    assert p.to_addrs == ["organizer-a@example.com", "quotes@example.net"]
    assert p.cc_addrs == ["helper@example.org"]
    assert p.subject == "Tent quote follow-up"
    assert p.sent_at == "2098-01-06T09:30:00Z"
    assert p.body_text.startswith("Hi all,\n\nThe tent quote came in.")
    assert p.body_text.endswith("Thanks,\nB")
    assert p.attachment_names == []
    assert p.forward_note is None
    assert p.forwarded_from is None
    assert p.task_tag is None


def test_parse_html_only_strips_tags_edge():
    p = parse_message(load("html_only.eml"))
    assert "<" not in p.body_text and ">" not in p.body_text
    assert "do not show" not in p.body_text  # script dropped
    assert "color: red" not in p.body_text  # style dropped
    assert "Ignore me" not in p.body_text  # title is not body text
    assert "Hello team," in p.body_text
    assert "valid for 7 days" in p.body_text and "& valid" in p.body_text  # entities decoded
    assert "Line one\nLine two" in p.body_text  # <br> keeps the line break
    assert "Hello team,\n" in p.body_text  # block tags break lines
    assert p.body_text.index("Tables") < p.body_text.index("Chairs")
    assert "Tables\nChairs" in p.body_text


def test_parse_attachment_names_only():
    p = parse_message(load("attachment.eml"))
    assert p.attachment_names == ["agreement.pdf", "site plan é.png"]
    assert p.body_text == "Signed agreement attached."
    assert "JVBER" not in p.body_text and "iVBOR" not in p.body_text  # no base64 leaks in


def test_parse_gmail_forward_note():
    p = parse_message(load("forward_gmail.eml"))
    assert p.forward_note == "They said yes!"
    assert p.forwarded_from == "rentals@example.org"
    assert p.from_addr == "organizer-b@example.com"
    assert "Your order for 12 tables is confirmed." in p.body_text


def test_parse_outlook_forward():
    p = parse_message(load("forward_outlook.eml"))
    assert p.forward_note == "Please file this under permits."
    assert p.forwarded_from == "permits@example.net"
    assert "The permit is approved." in p.body_text


def test_parse_bad_charset_edge():
    p = parse_message(load("latin1_no_charset.eml"))
    # No declared charset and not UTF-8: fall back to utf-8 with replacement, never raise.
    assert "�" in p.body_text
    assert p.body_text.startswith("The caf")
    assert "vendor sells cr" in p.body_text
    assert p.subject.startswith("Caf")


def test_parse_declared_charset_is_used():
    raw = load("latin1_no_charset.eml").replace(
        b"Content-Type: text/plain\n", b"Content-Type: text/plain; charset=iso-8859-1\n"
    )
    assert "café vendor sells crêpes" in parse_message(raw).body_text


@pytest.mark.parametrize("charset", ["x-no-such-charset", "utf-8", "us-ascii", '"', ""])
def test_parse_unusable_charset_edge(charset):
    raw = load("latin1_no_charset.eml").replace(
        b"Content-Type: text/plain\n", f"Content-Type: text/plain; charset={charset}\n".encode()
    )
    assert "vendor sells cr" in parse_message(raw).body_text


def test_parse_huge_body_truncated_edge():
    raw = huge_message()
    assert len(raw) > 5_000_000
    p = parse_message(raw)
    assert p.body_text.endswith("\n[truncated]")
    assert len(p.body_text.encode()) <= 200_000 + len("\n[truncated]")
    assert p.body_text.startswith("All work and no play")


def test_parse_huge_body_custom_cap_and_not_truncated_when_small():
    assert parse_message(huge_message(10_000), max_body=100).body_text.endswith("\n[truncated]")
    small = parse_message(load("plain.eml"))
    assert "[truncated]" not in small.body_text


def test_parse_truncation_never_splits_a_character():
    raw = huge_message(1000).replace(b"All work", "éééé".encode(), 1)
    raw = raw.replace(
        b'charset="utf-8"\nContent-Transfer-Encoding: 7bit',
        b'charset="utf-8"\nContent-Transfer-Encoding: 8bit',
    )
    p = parse_message(raw, max_body=3)
    assert p.body_text == "é\n[truncated]"


def test_parse_missing_message_id_edge():
    raw = load("missing_message_id.eml")
    p = parse_message(raw)
    import hashlib

    digest = hashlib.sha256(raw).hexdigest()
    assert p.message_id == f"<{digest}@generated.invalid>"
    assert p.thread_key == p.message_id
    assert parse_message(raw).message_id == p.message_id  # deterministic, so dedup works
    assert p.body_text == "This message has no Message-ID header."


def test_task_tag():
    base = load("plain.eml")
    tagged = base.replace(b"Subject: Tent quote follow-up", b"Subject: Re: Tent quote [T-42]")
    assert parse_message(tagged).task_tag == 42
    assert parse_message(base).task_tag is None
    for subject in (b"[T-]", b"[t-5]", b"[T-x]", b"T-5", b"[T-" + b"9" * 5000 + b"]"):
        assert parse_message(base.replace(b"Tent quote follow-up", subject)).task_tag is None


def test_thread_key_prefers_first_reference_then_in_reply_to():
    base = load("plain.eml")
    refs = base.replace(
        b"Subject:",
        b"In-Reply-To: <b@example.com>\nReferences: <a@example.com>\n <b@example.com>\nSubject:",
    )
    assert parse_message(refs).thread_key == "<a@example.com>"
    reply = base.replace(b"Subject:", b"In-Reply-To: <b@example.com>\nSubject:")
    assert parse_message(reply).thread_key == "<b@example.com>"


def test_forward_markers_must_stand_alone():
    base = load("plain.eml")

    def with_body(body: bytes) -> bytes:
        return base.split(b"\n\n", 1)[0] + b"\n\n" + body

    # A quoted marker is not a forward; neither is a lone rule or a rule not followed by From:.
    quoted = with_body(b"> ---------- Forwarded message ---------\n> From: x@example.org\n")
    assert parse_message(quoted).forward_note is None
    rule = with_body(b"Regards\n________________________________\nB, organizer\n")
    p = parse_message(rule)
    assert p.forward_note is None and p.forwarded_from is None


def test_forward_without_note_or_sender():
    base = load("plain.eml").split(b"\n\n", 1)[0] + b"\n\n"
    p = parse_message(base + b"---------- Forwarded message ---------\nDate: soon\n\nbody\n")
    assert p.forward_note is None
    assert p.forwarded_from is None


def test_forward_mailto_style_sender():
    base = load("plain.eml").split(b"\n\n", 1)[0] + b"\n\n"
    raw = base + (
        b"Note.\n\n________________________________\n"
        b"From: Permits [mailto:Permits@Example.NET]\nSent: x\n\nbody\n"
    )
    assert parse_message(raw).forwarded_from == "permits@example.net"


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.eml")), ids=lambda p: p.name)
def test_fixtures_parse_to_full_records(path):
    p = parse_message(path.read_bytes())
    assert p.message_id.startswith("<") and p.message_id.endswith(">")
    assert isinstance(p.body_text, str) and isinstance(p.attachment_names, list)


NASTY = [
    b"",
    b"\n\n\n",
    b"not an email at all",
    b"\x00\xff\xfe\x80" * 100,
    b"Subject: x\n\n",
    b"Content-Type: multipart/mixed\n\nno boundary",
    b'Content-Type: multipart/mixed; boundary="b"\n\n--b\n--b\nContent-Type: text/html\n\n<<<>>>',
    b"Content-Type: text/html\n\n<script>unterminated",
    b"Content-Type: text/html\n\n<!-- unterminated comment",
    b"Content-Type: text/html\n\n" + b"<div>" * 50_000,
    b"Date: not a date\nFrom: <>\nTo: ,,,;;;\nMessage-ID: <>\nSubject: [T-1]\n\nx",
    b"Date: Mon, 01 Jan 99999 00:00:00 +9999\nFrom: a\n\nx",
    b"Content-Transfer-Encoding: base64\n\n!!!not base64!!!",
    b"Content-Transfer-Encoding: quoted-printable\n\n=ZZ=\n=",
    b"Content-Type: image/png\n\n\x89PNG",
    b"Content-Type: multipart/alternative; boundary=x\n\n--x\n"
    b"Content-Type: text/plain; charset=bogus\n\n\xff\n--x--",
    b"Content-Type: message/rfc822\n\nSubject: inner\n\nbody",
    b"Message-ID: " + b"<" + b"a" * 100_000 + b">\n\nx",
    b"Subject: =?utf-8?q?=FF=FE?= =?bogus?q?x?=\n\nx",
    b"Content-Disposition: attachment; filename*=bogus''%zz\n\nx",
    b"References: <" + b"r" * 5000 + b">\n\n---------- Forwarded message ---------\n",
    b"\n________________________________\nFrom:\n",
]


@pytest.mark.parametrize("raw", NASTY, ids=range(len(NASTY)))
def test_parse_never_raises_on_hostile_input_edge(raw):
    p = parse_message(raw)
    assert isinstance(p, ParsedEmail)
    assert p.message_id
    assert isinstance(p.body_text, str)


def test_parse_never_raises_on_mutated_fixtures_edge():
    rng = random.Random(1234)  # noqa: S311  # seeded fuzzing, not security
    for path in sorted(FIXTURES.glob("*.eml")):
        raw = path.read_bytes()
        for _ in range(60):
            data = bytearray(raw)
            for _ in range(rng.randint(1, 8)):
                data[rng.randrange(len(data))] = rng.randrange(256)
            cut = rng.randrange(len(data) + 1)
            assert isinstance(parse_message(bytes(data[:cut])), ParsedEmail)


def test_html_strip_5mb_is_fast_enough_edge():
    body = b"<p>word <b>bold</b> &amp; more</p>\n" * (5 * 1024 * 1024 // 36)
    raw = b"Content-Type: text/html; charset=utf-8\nMessage-ID: <h@example.com>\n\n" + body
    p = parse_message(raw)
    assert p.body_text.endswith("\n[truncated]")


# --- migration 0002 ---------------------------------------------------------------------------


@pytest.fixture
def con(tmp_path):
    con = db.connect(tmp_path / "mail.db")
    db.migrate(con)
    yield con
    con.close()


def test_migration_0002_creates_email_tables(con):
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"email_item", "imap_state", "meta"} <= names
    cols = {r["name"] for r in con.execute("PRAGMA table_info(email_item)")}
    assert cols >= {
        "message_id",
        "thread_key",
        "from_addr",
        "to_addrs",
        "cc_addrs",
        "subject",
        "sent_at",
        "body_text",
        "attachment_names",
        "captured_by",
        "forward_note",
        "state",
        "task_id",
        "suggested_workstream_id",
        "body_purged_at",
    }
    assert {r["name"] for r in con.execute("PRAGMA table_info(imap_state)")} == {
        "label",
        "uidvalidity",
        "last_uid",
    }
    assert {r["name"] for r in con.execute("PRAGMA table_info(meta)")} == {"key", "value"}


def test_email_item_message_id_unique_and_state_checked(con):
    insert = (
        "INSERT INTO email_item (message_id, thread_key, from_addr, subject, state)"
        " VALUES (?, ?, 'a@example.com', 's', ?)"
    )
    con.execute(insert, ("<1@example.com>", "<1@example.com>", "inbox"))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(insert, ("<1@example.com>", "<1@example.com>", "inbox"))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(insert, ("<2@example.com>", "<2@example.com>", "bogus"))
    row = con.execute("SELECT * FROM email_item").fetchone()
    assert row["state"] == "inbox" and row["to_addrs"] == "[]" and row["body_purged_at"] is None


def test_email_item_task_id_is_a_foreign_key(con):
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO email_item (message_id, thread_key, from_addr, subject, task_id)"
            " VALUES ('<x@example.com>', '<x@example.com>', 'a@example.com', 's', 999)"
        )
