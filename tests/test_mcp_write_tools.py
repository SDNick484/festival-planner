"""Task 20: the write-scope MCP tools."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, date, datetime

import pytest
from access_keys import ORGANIZER_A, READ_SECRET, WRITE_SECRET, AccessKeys, make_config
from test_service_emails import email

import festival_planner.mcp.tools  # noqa: F401  (registers the tools)
from festival_planner import clock, db
from festival_planner.mcp.server import TOOLS
from festival_planner.service import Actor, activity, contacts, emails, tasks
from festival_planner.service import workstreams as ws_service
from festival_planner.web import create_app

NOW = datetime(2099, 10, 1, 12, 0, tzinfo=UTC)
WEB = Actor("web", ORGANIZER_A)
READ = {"Authorization": f"Bearer {READ_SECRET}"}
WRITE = {"Authorization": f"Bearer {WRITE_SECRET}"}

WRITE_TOOLS = {
    "create_task",
    "update_task",
    "log_note",
    "file_email",
    "dismiss_email",
    "upsert_contact",
}


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path, event_date=date(2099, 10, 10))


@pytest.fixture
def app(cfg, monkeypatch):
    AccessKeys(monkeypatch)
    monkeypatch.setattr(clock, "now", lambda: NOW)
    app = create_app(cfg)
    app.config.update(TESTING=True)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def con(app):
    with closing(db.connect(app.config["DATABASE"])) as con:
        yield con


@pytest.fixture
def ws(con):
    return ws_service.create(con, WEB, {"name": "Vendors"})["id"]


def call(client, name, arguments=None, *, headers=WRITE):
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    return client.post("/mcp", json=msg, headers=headers).json


def result(client, name, arguments=None, **kw):
    reply = call(client, name, arguments, **kw)
    assert "error" not in reply, reply
    res = reply["result"]
    assert not res.get("isError"), res
    assert json.loads(res["content"][0]["text"]) == res["structuredContent"]
    return res["structuredContent"]


def tool_error(client, name, arguments=None, **kw):
    res = call(client, name, arguments, **kw)["result"]
    assert res["isError"] is True
    assert "structuredContent" not in res
    return json.loads(res["content"][0]["text"])


def listed(client, headers):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    return {
        t["name"]: t for t in client.post("/mcp", json=msg, headers=headers).json["result"]["tools"]
    }


# --- registration and scope ----------------------------------------------------------------


def test_no_delete_tools_registered(client):
    assert WRITE_TOOLS <= TOOLS.keys()
    for name in TOOLS:
        assert "delete" not in name.lower() and "remove" not in name.lower(), name
    for headers in (READ, WRITE):
        assert not [n for n in listed(client, headers) if "delete" in n or "remove" in n]
    assert {TOOLS[n].scope for n in WRITE_TOOLS} == {"write"}
    for name in WRITE_TOOLS:
        schema = TOOLS[name].schema
        assert schema.get("additionalProperties") is False
        assert TOOLS[name].description
        # The actor and source come from the token, never from an argument.
        assert not {"actor", "source", "con", "cfg"} & schema["properties"].keys()


def test_read_token_gets_write_tools_hidden_and_refused(client, con, ws):
    assert WRITE_TOOLS <= listed(client, WRITE).keys()
    assert not WRITE_TOOLS & listed(client, READ).keys()
    calls = {
        "create_task": {"workstream_id": ws, "title": "Nope"},
        "update_task": {"id": 1, "title": "Nope"},
        "log_note": {"entity_type": "task", "entity_id": 1, "note": "Nope"},
        "file_email": {"email_id": 1, "task_id": 1},
        "dismiss_email": {"email_id": 1},
        "upsert_contact": {"name": "Nope"},
    }
    assert calls.keys() == WRITE_TOOLS
    for name, arguments in calls.items():
        reply = call(client, name, arguments, headers=READ)
        assert reply["error"]["code"] == -32001, name
        assert "result" not in reply
    assert tasks.list(con) == []
    assert contacts.list(con) == []


# --- source and actor ----------------------------------------------------------------------


def test_update_task_logs_token_source(client, con, ws):
    task = tasks.create(con, WEB, {"workstream_id": ws, "title": "Book tents"})
    got = result(client, "update_task", {"id": task["id"], "status": "doing"})
    assert got["task"]["status"] == "doing"
    entry = activity.list_activity(con, entity_type="task", entity_id=task["id"])[0]
    assert entry["source"] == "claude"
    assert entry["actor"] == "claude"
    assert entry["diff"]["status"] == ["todo", "doing"]


def test_source_is_never_taken_from_arguments(client, con, ws):
    for name, arguments in [
        ("create_task", {"workstream_id": ws, "title": "X", "source": "web"}),
        ("create_task", {"workstream_id": ws, "title": "X", "actor": ORGANIZER_A}),
        ("log_note", {"entity_type": "task", "entity_id": 1, "note": "x", "source": "web"}),
    ]:
        reply = call(client, name, arguments)
        assert reply["error"]["code"] == -32602
    assert tasks.list(con) == []


def test_every_write_tool_logs_the_token_name(client, con, cfg, ws):
    made = result(
        client,
        "create_task",
        {
            "workstream_id": ws,
            "title": "Order signs",
            "owner": ORGANIZER_A,
            "due_date": "2099-09-01",
        },
    )["task"]
    assert made["owner"] == ORGANIZER_A and made["due_date"] == "2099-09-01"
    assert made["effective_due"] == "2099-09-01"
    result(client, "log_note", {"entity_type": "task", "entity_id": made["id"], "note": "Called"})
    result(client, "log_note", {"entity_type": "workstream", "entity_id": ws, "note": "Kickoff"})
    contact = result(client, "upsert_contact", {"name": "Pat Vendor"})["contact"]
    mail = emails.capture(con, cfg, email(1))
    result(client, "file_email", {"email_id": mail["id"], "task_id": made["id"]})
    other = emails.capture(con, cfg, email(2))
    result(client, "dismiss_email", {"email_id": other["id"]})
    rows = activity.list_activity(con, limit=200)
    mine = [r for r in rows if r["source"] == "claude"]
    assert {r["action"] for r in mine} >= {"create", "note", "file_email", "dismiss"}
    assert {r["entity_type"] for r in mine} >= {"task", "workstream", "contact", "email_item"}
    assert contact["id"]
    assert all(r["actor"] == "claude" for r in mine)
    assert not [r for r in rows if r["source"] == "web" and r["entity_type"] == "contact"]


# --- tasks ---------------------------------------------------------------------------------


def test_create_task_defaults_and_all_fields(client, con, ws):
    plain = result(client, "create_task", {"workstream_id": ws, "title": "Plain"})["task"]
    assert plain["status"] == "todo" and plain["owner"] is None and plain["links"] == []
    full = result(
        client,
        "create_task",
        {
            "workstream_id": ws,
            "title": "Full",
            "status": "blocked",
            "blocked_reason": "No permit",
            "notes": "n",
            "links": [{"label": "Form", "url": "https://example.com/form"}],
            "waiting_on": "City office",
        },
    )["task"]
    assert full["blocked_reason"] == "No permit"
    assert full["links"] == [{"label": "Form", "url": "https://example.com/form"}]
    assert tasks.get(con, full["id"])["title"] == "Full"


def test_create_task_owner_must_be_an_organizer(client, con, ws):
    err = tool_error(
        client, "create_task", {"workstream_id": ws, "title": "X", "owner": "x@example.com"}
    )
    assert "owner" in err["fields"]
    assert tasks.list(con) == []


def test_update_task_is_partial_and_can_clear(client, con, ws):
    task = tasks.create(
        con, WEB, {"workstream_id": ws, "title": "A", "due_date": "2099-09-01", "notes": "keep"}
    )
    got = result(client, "update_task", {"id": task["id"], "due_date": None})["task"]
    assert got["due_date"] is None and got["notes"] == "keep" and got["title"] == "A"
    done = result(client, "update_task", {"id": task["id"], "status": "done"})["task"]
    assert done["done_at"]


def test_update_task_with_no_changes_is_harmless(client, con, ws):
    task = tasks.create(con, WEB, {"workstream_id": ws, "title": "A"})
    before = len(activity.list_activity(con, limit=200))
    assert result(client, "update_task", {"id": task["id"]})["task"]["title"] == "A"
    assert len(activity.list_activity(con, limit=200)) == before


def test_update_task_missing_is_a_tool_error(client):
    assert "not found" in tool_error(client, "update_task", {"id": 999, "title": "x"})["error"]


def test_validation_error_is_tool_error(client, con, ws):
    err = tool_error(
        client, "create_task", {"workstream_id": ws, "title": "X", "status": "blocked"}
    )
    assert err["error"] == "validation failed"
    assert "blocked_reason" in err["fields"]
    err = tool_error(client, "create_task", {"workstream_id": 9999, "title": "X"})
    assert "workstream_id" in err["fields"]
    err = tool_error(client, "create_task", {"workstream_id": ws, "title": "   "})
    assert "title" in err["fields"]
    err = tool_error(client, "create_task", {"workstream_id": ws, "title": "X", "due_date": "soon"})
    assert "due_date" in err["fields"]
    assert tasks.list(con) == []


def test_lone_surrogates_are_tool_errors_not_crashes(client, con, ws):
    err = tool_error(client, "create_task", {"workstream_id": ws, "title": "bad \ud800"})
    assert "title" in err["fields"]
    err = tool_error(
        client,
        "create_task",
        {
            "workstream_id": ws,
            "title": "ok",
            "links": [{"label": "\ud800", "url": "https://example.com"}],
        },
    )
    assert "links" in err["fields"]
    err = tool_error(client, "upsert_contact", {"name": "x", "emails": ["\ud800@example.com"]})
    assert "emails" in err["fields"]
    assert tasks.list(con) == []


def test_bad_argument_types_are_rpc_errors(client, ws):
    for name, arguments, field in [
        ("create_task", {"title": "No workstream"}, "workstream_id"),
        ("create_task", {"workstream_id": ws}, "title"),
        ("create_task", {"workstream_id": "1", "title": "x"}, "workstream_id"),
        ("create_task", {"workstream_id": ws, "title": "x" * 201}, "title"),
        ("create_task", {"workstream_id": ws, "title": "x", "status": "finished"}, "status"),
        ("update_task", {"title": "no id"}, "id"),
        ("update_task", {"id": True}, "id"),
        ("log_note", {"entity_type": "milestone", "entity_id": 1, "note": "x"}, "entity_type"),
        ("log_note", {"entity_type": "task", "entity_id": 1}, "note"),
        ("file_email", {"task_id": 1}, "email_id"),
        ("file_email", {"email_id": 1, "new_task": "x"}, "new_task"),
        ("dismiss_email", {}, "email_id"),
        ("upsert_contact", {"id": 0, "name": "x"}, "id"),
        ("upsert_contact", {"name": "x", "emails": "a@example.com"}, "emails"),
    ]:
        reply = call(client, name, arguments)
        assert reply["error"]["code"] == -32602, (name, arguments)
        assert field in reply["error"]["data"]["fields"]


# --- notes ---------------------------------------------------------------------------------


def test_log_note_on_task_and_workstream(client, con, ws):
    task = tasks.create(con, WEB, {"workstream_id": ws, "title": "A"})
    got = result(
        client, "log_note", {"entity_type": "task", "entity_id": task["id"], "note": " Hi "}
    )
    assert got["activity_id"] > 0
    entry = activity.list_activity(con, entity_type="task", entity_id=task["id"])[0]
    assert (entry["action"], entry["note"], entry["source"]) == ("note", "Hi", "claude")
    result(client, "log_note", {"entity_type": "workstream", "entity_id": ws, "note": "Hello"})


def test_log_note_errors(client, ws):
    assert (
        "note"
        in tool_error(
            client, "log_note", {"entity_type": "workstream", "entity_id": ws, "note": "  "}
        )["fields"]
    )
    assert (
        "not found"
        in tool_error(client, "log_note", {"entity_type": "task", "entity_id": 999, "note": "x"})[
            "error"
        ]
    )


# --- email triage --------------------------------------------------------------------------


def test_file_email_to_existing_task(client, con, cfg, ws):
    task = tasks.create(con, WEB, {"workstream_id": ws, "title": "A"})
    mail = emails.capture(con, cfg, email(1))
    got = result(client, "file_email", {"email_id": mail["id"], "task_id": task["id"]})
    assert got["email"]["state"] == "filed" and got["email"]["task_id"] == task["id"]
    assert got["task"]["id"] == task["id"]
    assert emails.list_inbox(con) == []


def test_file_email_to_new_task(client, con, cfg, ws):
    mail = emails.capture(con, cfg, email(1, subject="Tent quote"))
    got = result(client, "file_email", {"email_id": mail["id"], "new_task": {"workstream_id": ws}})
    assert got["task"]["title"] == "Tent quote" and got["task"]["workstream_id"] == ws
    assert got["email"]["task_id"] == got["task"]["id"]
    mail = emails.capture(con, cfg, email(2))
    got = result(
        client,
        "file_email",
        {"email_id": mail["id"], "new_task": {"workstream_id": ws, "title": "Chosen"}},
    )
    assert got["task"]["title"] == "Chosen"
    entry = activity.list_activity(con, entity_type="task", entity_id=got["task"]["id"])
    assert {e["source"] for e in entry} == {"claude"}


def test_file_email_needs_exactly_one_target(client, con, cfg, ws):
    mail = emails.capture(con, cfg, email(1))
    task = tasks.create(con, WEB, {"workstream_id": ws, "title": "A"})
    for arguments in (
        {"email_id": mail["id"]},
        {"email_id": mail["id"], "task_id": task["id"], "new_task": {"workstream_id": ws}},
    ):
        err = tool_error(client, "file_email", arguments)
        assert "task_id" in err["fields"]
    for new_task in (
        {},
        {"title": "x"},
        {"workstream_id": "1"},
        {"workstream_id": ws, "x": 1},
    ):
        err = tool_error(client, "file_email", {"email_id": mail["id"], "new_task": new_task})
        assert "new_task" in err["fields"], new_task
    assert emails.get(con, mail["id"])["state"] == "inbox"
    assert tasks.list(con, workstream_id=ws) == [tasks.get(con, task["id"])]


def test_file_email_errors(client, con, cfg, ws):
    assert "not found" in tool_error(client, "file_email", {"email_id": 999, "task_id": 1})["error"]
    mail = emails.capture(con, cfg, email(1))
    assert (
        "task_id"
        in tool_error(client, "file_email", {"email_id": mail["id"], "task_id": 999})["fields"]
    )
    err = tool_error(
        client, "file_email", {"email_id": mail["id"], "new_task": {"workstream_id": 999}}
    )
    assert "workstream_id" in err["fields"]
    assert emails.get(con, mail["id"])["state"] == "inbox"


def test_dismiss_email(client, con, cfg):
    mail = emails.capture(con, cfg, email(1))
    got = result(client, "dismiss_email", {"email_id": mail["id"]})
    assert got["email"]["state"] == "dismissed"
    assert "not found" in tool_error(client, "dismiss_email", {"email_id": 999})["error"]


# --- contacts ------------------------------------------------------------------------------


def test_upsert_contact_creates_then_updates(client, con, ws):
    made = result(
        client,
        "upsert_contact",
        {
            "name": "Pat Vendor",
            "workstream_id": ws,
            "emails": ["Pat@Example.org"],
            "phone": "555-0100",
        },
    )["contact"]
    assert made["emails"] == ["pat@example.org"] and made["workstream_id"] == ws
    changed = result(client, "upsert_contact", {"id": made["id"], "role": "Sales", "phone": None})[
        "contact"
    ]
    assert changed["id"] == made["id"] and changed["role"] == "Sales" and changed["phone"] is None
    assert changed["name"] == "Pat Vendor" and changed["emails"] == ["pat@example.org"]
    assert len(contacts.list(con)) == 1
    actions = [e["action"] for e in activity.list_activity(con, entity_type="contact")]
    assert actions == ["update", "create"]


def test_upsert_contact_errors(client):
    assert "name" in tool_error(client, "upsert_contact", {"organization": "No name"})["fields"]
    assert (
        "workstream_id"
        in tool_error(client, "upsert_contact", {"name": "x", "workstream_id": 999})["fields"]
    )
    assert "not found" in tool_error(client, "upsert_contact", {"id": 999, "name": "x"})["error"]
