from dataclasses import replace
from pathlib import Path

import pytest
from access_keys import ALIAS_A, ORGANIZER_A, ORGANIZER_B, make_config

from festival_planner import db, views
from festival_planner.mail.parse import ParsedEmail, parse_message
from festival_planner.service import (
    Actor,
    NotFound,
    ValidationError,
    activity,
    contacts,
    emails,
    tasks,
    workstreams,
)

FIXTURES = Path(__file__).parent / "fixtures" / "mail"
WEB = Actor("web", ORGANIZER_A)


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


@pytest.fixture
def ws(con):
    return workstreams.create(con, WEB, {"name": "Vendors"})["id"]


def fixture(name: str) -> ParsedEmail:
    return parse_message((FIXTURES / name).read_bytes())


def email(n: int = 1, **fields) -> ParsedEmail:
    values = {
        "message_id": f"<msg-{n}@example.com>",
        "thread_key": f"<msg-{n}@example.com>",
        "from_addr": "rentals@example.org",
        "to_addrs": ["capture@example.com"],
        "cc_addrs": [],
        "subject": f"Message {n}",
        "sent_at": "2098-01-06T09:30:00Z",
        "body_text": f"Body of message {n}.",
    }
    values.update(fields)
    return ParsedEmail(**values)


def log_for(con, entity_type, entity_id):
    return activity.list_activity(con, entity_type=entity_type, entity_id=entity_id)


# --- capture -------------------------------------------------------------------------------


def test_capture_dedup(con, cfg):
    parsed = fixture("forward_gmail.eml")
    first = emails.capture(con, cfg, parsed)
    assert first is not None and first["message_id"] == "<fwd-gmail-001@example.com>"
    assert emails.capture(con, cfg, parsed) is None
    assert emails.capture(con, cfg, replace(parsed, subject="changed")) is None
    assert con.execute("SELECT COUNT(*) FROM email_item").fetchone()[0] == 1
    assert len(activity.list_activity(con, entity_type="email_item")) == 1


def test_capture_alias_sets_captured_by(con, cfg):
    item = emails.capture(con, cfg, email(1, from_addr=ALIAS_A))
    assert item["captured_by"] == ORGANIZER_A
    entry = log_for(con, "email_item", item["id"])[0]
    assert (entry["source"], entry["actor"], entry["action"]) == ("email", ORGANIZER_A, "capture")

    item = emails.capture(con, cfg, email(2, from_addr=ORGANIZER_B.upper()))
    assert item["captured_by"] == ORGANIZER_B

    item = emails.capture(con, cfg, email(3, from_addr="rentals@example.org"))
    assert item["captured_by"] is None
    entry = log_for(con, "email_item", item["id"])[0]
    assert (entry["source"], entry["actor"]) == ("email", "external")


def test_capture_stores_fields_and_forwarded_sender(con, cfg):
    item = emails.capture(con, cfg, fixture("forward_gmail.eml"))
    assert item["captured_by"] == ORGANIZER_B
    # An organizer's forward: the original sender is the sender (spec 4.2).
    assert item["from_addr"] == "rentals@example.org"
    assert item["forward_note"] == "They said yes!"
    assert item["to_addrs"] == ["capture@example.com"]
    assert item["attachment_names"] == []
    assert item["state"] == "inbox"
    assert "12 tables" in item["body_text"]


def test_capture_external_forward_keeps_sender(con, cfg):
    item = emails.capture(
        con, cfg, email(1, from_addr="helper@example.org", forwarded_from="rentals@example.org")
    )
    assert item["from_addr"] == "helper@example.org"


def test_task_tag_autofiles(con, cfg, ws):
    task = tasks.create(con, WEB, {"workstream_id": ws, "title": "Book tent"})
    note = "Signed copy attached"
    item = emails.capture(
        con,
        cfg,
        email(
            1,
            subject=f"Re: quote [T-{task['id']}]",
            task_tag=task["id"],
            from_addr=ALIAS_A,
            forward_note=note,
        ),
    )
    assert item["state"] == "filed" and item["task_id"] == task["id"]
    assert item["suggested_workstream_id"] is None
    filed = log_for(con, "task", task["id"])[0]
    assert (filed["source"], filed["actor"], filed["action"]) == (
        "email",
        ORGANIZER_A,
        "file_email",
    )
    assert filed["note"] == note

    missing = emails.capture(con, cfg, email(2, subject="Re: quote [T-999]", task_tag=999))
    assert missing["state"] == "inbox" and missing["task_id"] is None

    tasks.delete(con, WEB, task["id"])
    deleted = emails.capture(
        con, cfg, email(3, subject=f"[T-{task['id']}] again", task_tag=task["id"])
    )
    assert deleted["state"] == "inbox"


# --- suggestions ---------------------------------------------------------------------------


def test_suggest_from_contact(con, cfg, ws):
    other = workstreams.create(con, WEB, {"name": "Rentals"})["id"]
    contacts.create(con, WEB, {"name": "Tent desk", "workstream_id": other, "emails": []})
    contacts.create(
        con, WEB, {"name": "Rentals desk", "workstream_id": ws, "emails": ["Rentals@example.org"]}
    )
    contacts.create(con, WEB, {"name": "No workstream", "emails": ["quotes@example.net"]})

    assert emails.suggest(con, email(1, from_addr="rentals@example.org")) == ws
    assert (
        emails.suggest(con, email(2, from_addr="x@example.com", cc_addrs=["rentals@example.org"]))
        == ws
    )
    assert (
        emails.suggest(con, email(3, from_addr=ORGANIZER_B, forwarded_from="RENTALS@example.org"))
        == ws
    )
    assert emails.suggest(con, email(4, from_addr="quotes@example.net")) is None
    assert emails.suggest(con, email(5, from_addr="nobody@example.com")) is None

    # Capture stores the suggestion; the Gmail forward's original sender is a contact.
    item = emails.capture(con, cfg, fixture("forward_gmail.eml"))
    assert item["suggested_workstream_id"] == ws


def test_suggest_from_thread(con, cfg, ws):
    later = workstreams.create(con, WEB, {"name": "Rentals"})["id"]
    t1 = tasks.create(con, WEB, {"workstream_id": ws, "title": "First"})
    t2 = tasks.create(con, WEB, {"workstream_id": later, "title": "Second"})
    thread = "<thread-root@example.com>"
    a = emails.capture(con, cfg, email(1, thread_key=thread))
    b = emails.capture(con, cfg, email(2, thread_key=thread))
    emails.capture(con, cfg, email(3, thread_key=thread))  # still in the inbox
    emails.file(con, WEB, a["id"], t1["id"])
    emails.file(con, WEB, b["id"], t2["id"])

    reply = email(4, thread_key=thread, from_addr="nobody@example.com")
    assert emails.suggest(con, reply) == later
    assert emails.suggest(con, email(5, thread_key="<other@example.com>")) is None

    # A contact match wins over the thread.
    contacts.create(con, WEB, {"name": "Desk", "workstream_id": ws, "emails": ["desk@example.org"]})
    assert emails.suggest(con, replace(reply, cc_addrs=["desk@example.org"])) == ws


# --- triage --------------------------------------------------------------------------------


def test_file_copies_forward_note_to_activity(con, cfg, ws):
    task = tasks.create(con, WEB, {"workstream_id": ws, "title": "Rent tables"})
    item = emails.capture(con, cfg, fixture("forward_gmail.eml"))
    claude = Actor("claude", "claude")
    filed = emails.file(con, claude, item["id"], task["id"])
    assert filed["state"] == "filed" and filed["task_id"] == task["id"]
    entry = log_for(con, "task", task["id"])[0]
    assert (entry["source"], entry["actor"], entry["action"]) == ("claude", "claude", "file_email")
    assert entry["note"] == "They said yes!"
    assert entry["diff"] == {"email_item": [None, item["id"]]}
    assert emails.get(con, item["id"])["state"] == "filed"


def test_file_rejects_missing_task_and_email(con, cfg):
    item = emails.capture(con, cfg, email(1))
    with pytest.raises(ValidationError) as exc:
        emails.file(con, WEB, item["id"], 999)
    assert "task_id" in exc.value.errors
    with pytest.raises(NotFound):
        emails.file(con, WEB, 999, 1)
    with pytest.raises(NotFound):
        emails.get(con, 999)
    assert emails.get(con, item["id"])["state"] == "inbox"


def test_file_new_task_uses_subject(con, cfg, ws):
    item = emails.capture(con, cfg, email(1, subject="Generator delivery window"))
    filed = emails.file_new_task(con, WEB, item["id"], ws)
    task = tasks.get(con, filed["task_id"])
    assert task["title"] == "Generator delivery window" and task["workstream_id"] == ws

    other = emails.capture(con, cfg, email(2, subject=""))
    titled = emails.file_new_task(con, WEB, other["id"], ws, title="Call the rentals desk")
    assert tasks.get(con, titled["task_id"])["title"] == "Call the rentals desk"

    blank = emails.capture(con, cfg, email(3, subject="   "))
    assert tasks.get(con, emails.file_new_task(con, WEB, blank["id"], ws)["task_id"])["title"]

    long = emails.capture(con, cfg, email(4, subject="x" * 900))
    created = tasks.get(con, emails.file_new_task(con, WEB, long["id"], ws)["task_id"])
    assert len(created["title"]) <= 200

    bad = emails.capture(con, cfg, email(5))
    with pytest.raises(ValidationError):
        emails.file_new_task(con, WEB, bad["id"], 999)
    assert emails.get(con, bad["id"])["state"] == "inbox"
    assert len(tasks.list(con)) == 4  # the failed one left no task behind


def test_dismiss_and_inbox(con, cfg):
    a = emails.capture(con, cfg, email(1))
    b = emails.capture(con, cfg, email(2))
    assert [e["id"] for e in emails.list_inbox(con)] == [b["id"], a["id"]]
    assert emails.inbox_count(con) == 2
    dismissed = emails.dismiss(con, WEB, a["id"])
    assert dismissed["state"] == "dismissed"
    entry = log_for(con, "email_item", a["id"])[0]
    assert (entry["action"], entry["diff"]) == ("dismiss", {"state": ["inbox", "dismissed"]})
    assert [e["id"] for e in emails.list_inbox(con)] == [b["id"]]
    assert emails.inbox_count(con) == 1


def test_digest_inbox_count(con, cfg):
    emails.capture(con, cfg, email(1))
    emails.capture(con, cfg, email(2))
    assert views.build_digest(con, cfg)["inbox_count"] == 2
