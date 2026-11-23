"""Inbox API (Task 16): list, get, file, new-task and dismiss, over the same service layer."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from access_keys import (
    ORGANIZER_A,
    ORGANIZER_B,
    READ_SECRET,
    WRITE_SECRET,
    AccessKeys,
    make_config,
)
from flask import Flask

from festival_planner import auth, clock, db
from festival_planner.api import register_api
from festival_planner.mail.parse import ParsedEmail
from festival_planner.service import activity, emails
from festival_planner.web import create_app

JWT_HEADER = "Cf-Access-Jwt-Assertion"
ORIGIN = "http://localhost"
NOW = datetime(2099, 10, 1, 12, 0, tzinfo=UTC)
EVENT_DATE = date(2099, 10, 10)


@pytest.fixture
def keys(monkeypatch):
    return AccessKeys(monkeypatch)


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path, event_date=EVENT_DATE)


@pytest.fixture
def app(cfg, keys, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: NOW)
    app = create_app(cfg)
    app.config.update(TESTING=True)
    return app


class Api:
    def __init__(self, app, keys, email=ORGANIZER_A):
        self.client = app.test_client()
        self.keys = keys
        self.email = email

    def call(self, method, path, body=None):
        headers = {JWT_HEADER: self.keys.sign(self.email), "Origin": ORIGIN}
        kwargs = {} if body is None else {"json": body}
        return self.client.open(path, method=method, headers=headers, **kwargs)

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, body=None):
        return self.call("POST", path, {} if body is None else body)


@pytest.fixture
def api(app, keys):
    return Api(app, keys)


def make_ws(api, name="Vendors"):
    r = api.post("/api/workstreams", {"name": name})
    assert r.status_code == 201, r.json
    return r.json["id"]


def make_task(api, ws, title="Book tent"):
    r = api.post("/api/tasks", {"workstream_id": ws, "title": title})
    assert r.status_code == 201, r.json
    return r.json["id"]


_counter = iter(range(1, 1000))


def capture(app, cfg, **fields):
    n = next(_counter)
    values = {
        "message_id": f"<inbox-{n}@example.com>",
        "thread_key": f"<inbox-{n}@example.com>",
        "from_addr": "rentals@example.org",
        "to_addrs": ["capture@example.com"],
        "cc_addrs": [],
        "subject": f"Tent quote {n}",
        "sent_at": "2098-01-06T09:30:00Z",
        "body_text": f"Body of message {n}.",
    }
    values.update(fields)
    with app.app_context():
        row = emails.capture(db.get_db(), cfg, ParsedEmail(**values))
    assert row is not None
    return row["id"]


def log_for(app, entity_type, entity_id):
    with app.app_context():
        return activity.list_activity(db.get_db(), entity_type=entity_type, entity_id=entity_id)


# --- reads ---------------------------------------------------------------------------------


def test_inbox_lists_previews_not_bodies(app, cfg, api):
    long_body = "x" * 150 + "y" * 150
    short = capture(app, cfg, body_text="short body", forward_note="Please handle")
    big = capture(app, cfg, body_text=long_body, attachment_names=["quote.pdf"])
    filed = capture(app, cfg)
    api.post(f"/api/inbox/{filed}/dismiss")
    rows = api.get("/api/inbox").json
    assert [r["id"] for r in rows] == [big, short]  # newest first, dismissed ones are gone
    assert all("body_text" not in r for r in rows)
    assert rows[0]["preview"] == long_body[:200]
    assert rows[0]["preview_truncated"] is True
    assert rows[0]["attachment_names"] == ["quote.pdf"]
    assert rows[1]["preview"] == "short body"
    assert rows[1]["preview_truncated"] is False
    assert rows[1]["forward_note"] == "Please handle"


def test_get_email_returns_full_body(app, cfg, api):
    long_body = "z" * 500
    eid = capture(app, cfg, body_text=long_body)
    got = api.get(f"/api/emails/{eid}").json
    assert got["body_text"] == long_body
    assert got["subject"] and got["state"] == "inbox"
    assert api.get("/api/emails/9999").status_code == 404
    assert api.get("/api/emails/abc").status_code == 404


def test_purged_body_has_empty_preview(app, cfg, api):
    eid = capture(app, cfg)
    with app.app_context():
        con = db.get_db()
        con.execute("UPDATE email_item SET body_text = NULL WHERE id = ?", (eid,))
        con.commit()
    row = api.get("/api/inbox").json[0]
    assert row["preview"] == "" and row["preview_truncated"] is False


def test_task_includes_filed_emails_without_bodies(app, cfg, api):
    ws = make_ws(api)
    tid = make_task(api, ws)
    eid = capture(app, cfg, body_text="q" * 300, forward_note="Check the price")
    other = capture(app, cfg)
    assert api.post(f"/api/inbox/{eid}/file", {"task_id": tid}).status_code == 200
    got = api.get(f"/api/tasks/{tid}").json
    assert [e["id"] for e in got["emails"]] == [eid]
    assert "body_text" not in got["emails"][0]
    assert got["emails"][0]["preview"] == "q" * 200
    assert got["emails"][0]["forward_note"] == "Check the price"
    assert other not in [e["id"] for e in got["emails"]]
    # a task with nothing filed has an empty list; other task endpoints are unchanged
    empty = make_task(api, ws, "Other")
    assert api.get(f"/api/tasks/{empty}").json["emails"] == []
    assert "emails" not in api.get("/api/tasks").json[0]


# --- triage --------------------------------------------------------------------------------


def test_file_from_inbox_logs_web(app, cfg, api):
    ws = make_ws(api)
    tid = make_task(api, ws)
    eid = capture(app, cfg, forward_note="Please book this", body_text="b" * 400)
    r = api.post(f"/api/inbox/{eid}/file", {"task_id": tid})
    assert r.status_code == 200
    assert r.json["state"] == "filed" and r.json["task_id"] == tid
    assert "body_text" not in r.json
    assert api.get("/api/inbox").json == []
    entry = log_for(app, "task", tid)[0]
    assert entry["action"] == "file_email"
    assert (entry["source"], entry["actor"]) == ("web", ORGANIZER_A)
    assert entry["note"] == "Please book this"


def test_source_and_actor_come_from_identity_not_body(app, cfg, keys):
    eid = capture(app, cfg)
    ws_api = Api(app, keys, ORGANIZER_B)
    tid = make_task(ws_api, make_ws(ws_api))
    bad = ws_api.post(f"/api/inbox/{eid}/file", {"task_id": tid, "source": "email", "actor": "x"})
    assert bad.status_code == 400 and "source" in bad.json["fields"]
    assert ws_api.post(f"/api/inbox/{eid}/file", {"task_id": tid}).status_code == 200
    entry = log_for(app, "task", tid)[0]
    assert (entry["source"], entry["actor"]) == ("web", ORGANIZER_B)


def test_new_task_defaults_title_to_subject(app, cfg, api):
    ws = make_ws(api)
    eid = capture(app, cfg, subject="Example Tent Co. quote", forward_note="Need by Friday")
    r = api.post(f"/api/inbox/{eid}/new-task", {"workstream_id": ws})
    assert r.status_code == 200, r.json
    assert r.json["state"] == "filed"
    task = api.get(f"/api/tasks/{r.json['task_id']}").json
    assert task["title"] == "Example Tent Co. quote"
    assert task["workstream_id"] == ws
    assert [e["id"] for e in task["emails"]] == [eid]
    r = api.post(
        f"/api/inbox/{capture(app, cfg)}/new-task", {"workstream_id": ws, "title": "Call back"}
    )
    assert api.get(f"/api/tasks/{r.json['task_id']}").json["title"] == "Call back"


def test_dismiss(app, cfg, api):
    eid = capture(app, cfg)
    r = api.post(f"/api/inbox/{eid}/dismiss")
    assert r.status_code == 200 and r.json["state"] == "dismissed"
    assert "body_text" not in r.json
    assert api.get("/api/inbox").json == []
    entry = log_for(app, "email_item", eid)[0]
    assert entry["action"] == "dismiss"
    assert (entry["source"], entry["actor"]) == ("web", ORGANIZER_A)
    assert api.post("/api/inbox/9999/dismiss").status_code == 404


def test_inbox_rejects_bad_input_with_4xx(app, cfg, api):
    ws = make_ws(api)
    eid = capture(app, cfg)
    file_url = f"/api/inbox/{eid}/file"
    new_url = f"/api/inbox/{eid}/new-task"

    def bad(url, body, field, status=400):
        r = api.post(url, body)
        assert r.status_code == status, (body, r.status_code, r.json)
        if field:
            assert field in r.json["fields"], r.json

    bad(file_url, {}, "task_id")
    bad(file_url, {"task_id": "1"}, "task_id")
    bad(file_url, {"task_id": True}, "task_id")
    bad(file_url, {"task_id": 2**70}, "task_id")
    bad(file_url, {"task_id": 0}, "task_id")
    bad(file_url, {"task_id": 9999}, "task_id")
    bad(file_url, {"task_id": [1]}, "task_id")
    bad(file_url, [1], "_")
    bad(new_url, {}, "workstream_id")
    bad(new_url, {"workstream_id": "x"}, "workstream_id")
    bad(new_url, {"workstream_id": 2**70}, "workstream_id")
    bad(new_url, {"workstream_id": 9999}, "workstream_id")
    bad(new_url, {"workstream_id": ws, "title": ""}, "title")
    bad(new_url, {"workstream_id": ws, "title": 5}, "title")
    bad(new_url, {"workstream_id": ws, "extra": 1}, "extra")
    bad("/api/inbox/9999/file", {"task_id": 1}, None, 404)
    bad("/api/inbox/9999/new-task", {"workstream_id": ws}, None, 404)
    assert api.get("/api/inbox").json[0]["id"] == eid  # nothing above changed it
    with app.app_context():
        assert db.get_db().execute("SELECT COUNT(*) FROM task").fetchone()[0] == 0


def test_inbox_mirror_scopes(cfg, keys):
    app = Flask(__name__)
    app.config["FP"] = cfg
    db.init_app(app)
    auth.init_app(app)
    register_api(app, "/api", "web")
    register_api(app, "/mcp/api", "token")
    for rule in app.url_map.iter_rules():
        if "inbox" in rule.rule or "/emails/" in rule.rule:
            want = "web" if rule.rule.startswith("/api") else None
            kind = app.view_functions[rule.endpoint]._fp_auth
            assert kind == (want or ("read" if "GET" in rule.methods else "write")), rule
    client = app.test_client()
    read = {"Authorization": f"Bearer {READ_SECRET}"}
    write = {"Authorization": f"Bearer {WRITE_SECRET}"}
    assert client.get("/mcp/api/inbox", headers=read).json == []
    assert client.post("/mcp/api/inbox/1/dismiss", json={}, headers=read).status_code == 403
    assert client.post("/mcp/api/inbox/1/dismiss", json={}, headers=write).status_code == 404
    assert client.get("/api/inbox", headers=read).status_code == 401
