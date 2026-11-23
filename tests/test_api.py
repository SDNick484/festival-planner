"""REST API (Task 7): thin adapters over the service layer, authenticated by Access JWT."""

from __future__ import annotations

import json
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

from festival_planner import clock, db, views
from festival_planner.api import register_api
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
    """A test client that signs in as an organizer and sends the same-origin header on writes."""

    def __init__(self, app, keys, email=ORGANIZER_A):
        self.client = app.test_client()
        self.keys = keys
        self.email = email

    def call(self, method, path, body=None, *, raw=None, headers=None):
        hdrs = {JWT_HEADER: self.keys.sign(self.email), "Origin": ORIGIN, **(headers or {})}
        kwargs = {}
        if raw is not None:
            kwargs.update(data=raw, content_type="application/json")
        elif body is not None:
            kwargs.update(json=body)
        return self.client.open(path, method=method, headers=hdrs, **kwargs)

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body, **kw)

    def patch(self, path, body=None, **kw):
        return self.call("PATCH", path, body, **kw)

    def put(self, path, body=None, **kw):
        return self.call("PUT", path, body, **kw)

    def delete(self, path, **kw):
        return self.call("DELETE", path, **kw)


@pytest.fixture
def api(app, keys):
    return Api(app, keys)


def make_ws(api, name="Vendors", **extra):
    r = api.post("/api/workstreams", {"name": name, **extra})
    assert r.status_code == 201, r.json
    return r.json["id"]


def make_task(api, ws, title="Book tent", **extra):
    r = api.post("/api/tasks", {"workstream_id": ws, "title": title, **extra})
    assert r.status_code == 201, r.json
    return r.json


# --- CRUD round trips ----------------------------------------------------------------------


def test_event_get_and_put(api):
    r = api.get("/api/event")
    assert r.status_code == 200
    assert r.json["name"] == "Example Fall Festival"
    assert r.json["event_date"] == EVENT_DATE.isoformat()
    r = api.put("/api/event", {"name": "Example Spring Fair", "goal_note": "Raise funds"})
    assert r.status_code == 200
    assert r.json["name"] == "Example Spring Fair"
    assert api.get("/api/event").json["goal_note"] == "Raise funds"
    r = api.put("/api/event", {"event_date": "2099-11-01"})
    assert r.json["event_date"] == "2099-11-01"


def test_workstream_crud(api):
    ws = make_ws(api, owner=ORGANIZER_B, color="#3366CC")
    r = api.get(f"/api/workstreams/{ws}")
    assert r.status_code == 200
    assert r.json["owner"] == ORGANIZER_B and r.json["color"] == "#3366cc"
    assert [w["id"] for w in api.get("/api/workstreams").json] == [ws]
    r = api.patch(f"/api/workstreams/{ws}", {"name": "Food", "archived": True})
    assert r.status_code == 200 and r.json["name"] == "Food" and r.json["archived"] is True
    assert api.delete(f"/api/workstreams/{ws}").status_code == 204
    assert api.get(f"/api/workstreams/{ws}").status_code == 404
    assert api.get("/api/workstreams").json == []


def test_owner_must_be_a_configured_organizer(api):
    r = api.post("/api/workstreams", {"name": "Food", "owner": "stranger@example.org"})
    assert r.status_code == 400 and "owner" in r.json["fields"]
    assert make_ws(api, "Food", owner=ORGANIZER_A)


def test_task_crud(api):
    ws = make_ws(api)
    t = make_task(api, ws, owner=ORGANIZER_A, due_date="2099-10-05", links=[])
    assert t["status"] == "todo" and t["due_date"] == "2099-10-05"
    r = api.patch(f"/api/tasks/{t['id']}", {"status": "blocked"})
    assert r.status_code == 400 and "blocked_reason" in r.json["fields"]
    r = api.patch(f"/api/tasks/{t['id']}", {"status": "blocked", "blocked_reason": "No permit"})
    assert r.status_code == 200 and r.json["status"] == "blocked"
    r = api.patch(f"/api/tasks/{t['id']}", {"status": "done"})
    assert r.json["done_at"] and r.json["blocked_reason"] is None
    assert api.get(f"/api/tasks/{t['id']}").json["title"] == "Book tent"
    assert api.delete(f"/api/tasks/{t['id']}").status_code == 204
    assert api.get(f"/api/tasks/{t['id']}").status_code == 404
    assert api.delete(f"/api/tasks/{t['id']}").status_code == 404


def test_milestone_crud(api):
    r = api.post("/api/milestones", {"name": "Permits in", "days_before_event": 30})
    assert r.status_code == 201
    mid = r.json["id"]
    assert api.get(f"/api/milestones/{mid}").json["days_before_event"] == 30
    r = api.patch(f"/api/milestones/{mid}", {"days_before_event": 21, "notes": "Due early"})
    assert r.json["days_before_event"] == 21 and r.json["notes"] == "Due early"
    assert [m["id"] for m in api.get("/api/milestones").json] == [mid]
    ws = make_ws(api)
    make_task(api, ws, milestone_id=mid)
    r = api.delete(f"/api/milestones/{mid}")  # still used by a task
    assert r.status_code == 400 and "tasks" in r.json["fields"]


def test_contact_crud(api):
    ws = make_ws(api)
    r = api.post(
        "/api/contacts",
        {"name": "Example Tent Co.", "workstream_id": ws, "emails": ["orders@example.com"]},
    )
    assert r.status_code == 201
    cid = r.json["id"]
    assert api.get(f"/api/contacts/{cid}").json["emails"] == ["orders@example.com"]
    r = api.patch(f"/api/contacts/{cid}", {"role": "Tents", "phone": "555-0100"})
    assert r.json["role"] == "Tents"
    assert [c["id"] for c in api.get(f"/api/contacts?workstream={ws}").json] == [cid]
    assert [c["id"] for c in api.get("/api/contacts?q=tent").json] == [cid]
    assert api.get("/api/contacts?q=zzz").json == []
    assert api.delete(f"/api/contacts/{cid}").status_code == 204
    assert api.get(f"/api/contacts/{cid}").status_code == 404


def test_task_note_is_logged(api):
    ws = make_ws(api)
    t = make_task(api, ws)
    r = api.post(f"/api/tasks/{t['id']}/notes", {"note": "Called the vendor"})
    assert r.status_code == 201
    rows = api.get(f"/api/activity?entity_type=task&entity_id={t['id']}").json
    note = next(a for a in rows if a["action"] == "note")
    assert note["note"] == "Called the vendor" and note["source"] == "web"
    assert api.post(f"/api/tasks/{t['id']}/notes", {"note": "  "}).status_code == 400
    assert api.post(f"/api/tasks/{t['id']}/notes", {}).status_code == 400
    assert api.post(f"/api/tasks/{t['id']}/notes", {"note": "x", "actor": "y"}).status_code == 400
    assert api.post("/api/tasks/999/notes", {"note": "x"}).status_code == 404


# --- filters -------------------------------------------------------------------------------


def test_task_filters(api):
    w1, w2 = make_ws(api, "Vendors"), make_ws(api, "Food")
    a = make_task(api, w1, "Book tent", owner=ORGANIZER_A, due_date="2099-09-01")
    b = make_task(api, w1, "Order signs", owner=ORGANIZER_B, due_date="2099-10-01", status="doing")
    c = make_task(api, w2, "Menu 100% final_v2", notes="caterer call")
    d = make_task(api, w2, "Hire band", due_date="2099-08-01", status="waiting")

    def ids(query):
        r = api.get(f"/api/tasks{query}")
        assert r.status_code == 200, r.json
        return [t["id"] for t in r.json]

    assert ids("") == [d["id"], a["id"], b["id"], c["id"]]  # due date order, undated last
    assert ids(f"?workstream={w1}") == [a["id"], b["id"]]
    assert ids("?status=doing") == [b["id"]]
    assert ids(f"?owner={ORGANIZER_A.upper()}") == [a["id"]]
    assert ids("?due_before=2099-09-15") == [d["id"], a["id"]]
    assert ids("?q=TENT") == [a["id"]]
    assert ids("?q=caterer") == [c["id"]]
    assert ids("?q=100%25") == [c["id"]]  # LIKE wildcards are literal
    assert ids("?q=%25") == [c["id"]]
    assert ids(f"?workstream={w2}&status=waiting") == [d["id"]]
    assert ids("?limit=2") == [d["id"], a["id"]]
    assert ids("?limit=2&offset=2") == [b["id"], c["id"]]
    assert ids("?offset=10") == []


def test_activity_filters(api):
    ws = make_ws(api)
    t = make_task(api, ws)
    api.patch(f"/api/tasks/{t['id']}", {"title": "Renamed"})
    rows = api.get("/api/activity").json
    assert [r["action"] for r in rows][:2] == ["update", "create"]
    assert len(api.get("/api/activity?entity_type=task").json) == 2
    assert len(api.get(f"/api/activity?entity_type=task&entity_id={t['id']}").json) == 2
    assert api.get("/api/activity?entity_type=contact").json == []
    assert len(api.get("/api/activity?source=web").json) == 3
    assert api.get("/api/activity?source=email").json == []
    assert api.get("/api/activity?since=2099-10-01T12:00:01Z").json == []
    assert len(api.get("/api/activity?since=2000-01-01").json) == 4  # plus the bootstrap row
    assert len(api.get("/api/activity?limit=1").json) == 1


# --- Review Focus 4 ------------------------------------------------------------------------


def test_api_rejects_unknown_and_bad_types(api):
    ws = make_ws(api)
    t = make_task(api, ws)
    tid = t["id"]

    def bad(method, path, body=None, field=None, **kw):
        r = api.call(method, path, body, **kw)
        assert r.status_code == 400, (method, path, body, r.status_code, r.data)
        assert set(r.json) == {"error", "fields"}
        if field:
            assert field in r.json["fields"], r.json
        return r

    # unknown fields, on create and update, for every resource
    bad("POST", "/api/tasks", {"workstream_id": ws, "title": "x", "bogus": 1}, "bogus")
    bad("PATCH", f"/api/tasks/{tid}", {"bogus": 1}, "bogus")
    bad("POST", "/api/workstreams", {"name": "x", "bogus": 1}, "bogus")
    bad("POST", "/api/milestones", {"name": "x", "days_before_event": 1, "bogus": 1}, "bogus")
    bad("POST", "/api/contacts", {"name": "x", "bogus": 1}, "bogus")
    bad("PUT", "/api/event", {"bogus": 1}, "bogus")
    # attributing a change to someone else is just an unknown field
    bad("PATCH", f"/api/tasks/{tid}", {"source": "email", "actor": "x"})
    bad("PATCH", f"/api/tasks/{tid}", {"id": 99}, "id")
    # wrong types
    bad("PATCH", f"/api/tasks/{tid}", {"due_date": 5}, "due_date")
    bad("PATCH", f"/api/tasks/{tid}", {"due_date": "2099-13-45"}, "due_date")
    bad("PATCH", f"/api/tasks/{tid}", {"title": ["a"]}, "title")
    bad("PATCH", f"/api/tasks/{tid}", {"title": {"a": 1}}, "title")
    bad("PATCH", f"/api/tasks/{tid}", {"status": ["done"]}, "status")
    bad("PATCH", f"/api/tasks/{tid}", {"status": "finished"}, "status")
    bad("PATCH", f"/api/tasks/{tid}", {"owner": 7}, "owner")
    bad("PATCH", f"/api/tasks/{tid}", {"milestone_id": "1"}, "milestone_id")
    bad("PATCH", f"/api/tasks/{tid}", {"milestone_id": 1.5}, "milestone_id")
    bad("PATCH", f"/api/tasks/{tid}", {"workstream_id": True}, "workstream_id")
    bad("PATCH", f"/api/tasks/{tid}", {"workstream_id": 10**30}, "workstream_id")
    bad("PATCH", f"/api/tasks/{tid}", {"links": "nope"}, "links")
    bad("PATCH", f"/api/tasks/{tid}", {"links": [{"label": 1, "url": 2}]}, "links")
    bad("PATCH", f"/api/tasks/{tid}", {"title": "x" * 201}, "title")
    bad("POST", "/api/tasks", {"workstream_id": ws}, "title")
    bad("POST", "/api/tasks", {"title": "x"}, "workstream_id")
    bad("POST", "/api/tasks", {"workstream_id": 999, "title": "x"}, "workstream_id")
    bad("POST", "/api/milestones", {"name": "x", "days_before_event": -1}, "days_before_event")
    bad("POST", "/api/contacts", {"name": "x", "emails": "a@example.com"}, "emails")
    bad("PUT", "/api/event", {"timezone": "Mars/Base"}, "timezone")
    # bodies that are not objects, and bad JSON
    for body in ([], "text", 5, None):
        bad("PATCH", f"/api/tasks/{tid}", raw=json.dumps(body))
    bad("PATCH", f"/api/tasks/{tid}", raw="{not json")
    bad("PATCH", f"/api/tasks/{tid}", raw="")
    bad("PATCH", f"/api/tasks/{tid}", raw=b"\xff\xfe\x00bad")
    bad("PATCH", f"/api/tasks/{tid}", raw="[" * 100_000)  # recursion bomb
    bad("PATCH", f"/api/tasks/{tid}", raw='{"title": "lone \\ud800 surrogate"}')
    bad("PATCH", f"/api/tasks/{tid}", raw='{"\\udc00": 1}')
    # queries
    bad("GET", "/api/tasks?limit=-1", field="limit")
    bad("GET", "/api/tasks?limit=0", field="limit")
    bad("GET", "/api/tasks?limit=501", field="limit")
    bad("GET", "/api/tasks?limit=abc", field="limit")
    bad("GET", "/api/tasks?limit=1.5", field="limit")
    bad("GET", f"/api/tasks?limit={10**30}", field="limit")
    bad("GET", "/api/tasks?offset=-1", field="offset")
    bad("GET", f"/api/tasks?offset={2**63}", field="offset")
    bad("GET", "/api/tasks?workstream=abc", field="workstream")
    bad("GET", f"/api/tasks?workstream={10**30}", field="workstream")
    bad("GET", "/api/tasks?status=finished", field="status")
    bad("GET", "/api/tasks?due_before=tomorrow", field="due_before")
    bad("GET", "/api/tasks?bogus=1", field="bogus")
    bad("GET", "/api/tasks?limit=1&limit=2", field="limit")
    bad("GET", "/api/workstreams?limit=-1", field="limit")
    bad("GET", "/api/milestones?limit=-1", field="limit")
    bad("GET", "/api/contacts?limit=-1", field="limit")
    bad("GET", "/api/contacts?workstream=abc", field="workstream")
    bad("GET", "/api/activity?limit=-1", field="limit")
    bad("GET", "/api/activity?since=yesterday", field="since")
    bad("GET", "/api/activity?since=0001-01-01T00:00:00%2B14:00", field="since")
    bad("GET", "/api/activity?entity_id=abc", field="entity_id")
    bad("GET", f"/api/activity?entity_id={10**30}", field="entity_id")
    bad("GET", "/api/digest?x=1", field="x")
    # non-integer or absurd ids in the path are plain 404s, as JSON
    for path in (
        "/api/tasks/abc",
        "/api/tasks/-1",
        "/api/tasks/0",
        "/api/tasks/1.5",
        f"/api/tasks/{10**30}",
        "/api/tasks/abc/notes",
        "/api/workstreams/abc",
        "/api/milestones/abc",
        "/api/contacts/abc",
    ):
        for method in ("GET", "PATCH", "DELETE", "POST"):
            r = api.call(method, path, {})
            assert r.status_code in (404, 405), (method, path, r.status_code)
            assert r.is_json and "error" in r.json, (method, path)
        assert api.get(path).status_code == 404, path
    assert api.get("/api/tasks/abc").json == {"error": "not found", "fields": {}}
    assert api.get("/api/nothing-here").status_code == 404
    # nothing above changed the task or created anything
    assert api.get(f"/api/tasks/{tid}").json == {**t, "emails": []}  # GET adds the filed emails
    assert len(api.get("/api/tasks").json) == 1


def test_error_body_shape(api):
    r = api.post("/api/workstreams", {})
    assert r.status_code == 400
    assert r.json == {"error": "validation failed", "fields": {"name": "required"}}
    r = api.get("/api/tasks/9999")
    assert r.status_code == 404 and set(r.json) == {"error", "fields"}
    r = api.call("PATCH", "/api/tasks/1", raw="{oops")
    assert r.status_code == 400 and r.json["error"] and r.json["fields"] == {}
    r = api.call("PUT", "/api/tasks")  # no such method on the collection
    assert r.status_code == 405 and r.is_json and "PUT" not in r.headers["Allow"]


def test_oversized_body_is_413(api):
    r = api.post("/api/workstreams", raw='{"name": "' + "x" * (2 << 20) + '"}')
    assert r.status_code == 413 and r.is_json


def test_server_error_never_leaks_traceback(api, monkeypatch):
    from festival_planner.service import workstreams

    def boom(*args, **kwargs):
        raise RuntimeError("secret-internal-detail /srv/app.py")

    monkeypatch.setattr(workstreams, "list", boom)
    r = api.get("/api/workstreams")
    assert r.status_code == 500
    assert r.json == {"error": "internal error", "fields": {}}
    assert b"secret-internal-detail" not in r.data and b"Traceback" not in r.data


def test_busy_database_is_503_not_500(api, monkeypatch):
    import sqlite3

    from festival_planner.service import workstreams

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(workstreams, "create", locked)
    r = api.post("/api/workstreams", {"name": "x"})
    assert r.status_code == 503 and r.json["error"] and b"locked" not in r.data


# --- source and actor ----------------------------------------------------------------------


def test_patch_logs_web_source(api, app):
    ws = make_ws(api)
    t = make_task(api, ws)
    r = api.patch(f"/api/tasks/{t['id']}", {"title": "Renamed", "status": "doing"})
    assert r.status_code == 200
    rows = api.get(f"/api/activity?entity_type=task&entity_id={t['id']}").json
    assert rows[0]["action"] == "update"
    assert (rows[0]["source"], rows[0]["actor"]) == ("web", ORGANIZER_A)
    assert rows[0]["diff"] == {"title": ["Book tent", "Renamed"], "status": ["todo", "doing"]}
    # the other organizer's change is attributed to that organizer
    other = Api(app, api.keys, ORGANIZER_B)
    other.patch(f"/api/tasks/{t['id']}", {"title": "Again"})
    row = api.get(f"/api/activity?entity_type=task&entity_id={t['id']}").json[0]
    assert (row["source"], row["actor"]) == ("web", ORGANIZER_B)
    # a no-op patch logs nothing
    n = len(api.get("/api/activity").json)
    assert api.patch(f"/api/tasks/{t['id']}", {"title": "Again"}).status_code == 200
    assert len(api.get("/api/activity").json) == n


def test_digest_endpoint_matches_build_digest(api, app, cfg):
    ws = make_ws(api, "Vendors")
    make_ws(api, "Empty")
    make_task(api, ws, "Overdue thing", due_date="2099-09-20")
    make_task(api, ws, "Soon thing", due_date="2099-10-03", status="doing")
    make_task(api, ws, "Stuck thing", status="blocked", blocked_reason="No permit")
    r = api.get("/api/digest")
    assert r.status_code == 200
    with app.app_context():
        expected = views.build_digest(db.get_db(), cfg)
    assert r.json == json.loads(json.dumps(expected))
    assert r.json["countdown"] == {"days": 9, "label": "9 days"}
    assert [i["title"] for i in r.json["needs_attention"]] == ["Overdue thing", "Stuck thing"]
    assert [i["title"] for i in r.json["due_this_week"]] == ["Soon thing"]


# --- auth ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/event"),
        ("PUT", "/api/event"),
        ("GET", "/api/workstreams"),
        ("POST", "/api/workstreams"),
        ("GET", "/api/tasks"),
        ("POST", "/api/tasks"),
        ("PATCH", "/api/tasks/1"),
        ("DELETE", "/api/tasks/1"),
        ("POST", "/api/tasks/1/notes"),
        ("GET", "/api/milestones"),
        ("GET", "/api/contacts"),
        ("GET", "/api/activity"),
        ("GET", "/api/digest"),
    ],
)
def test_every_route_requires_the_access_jwt(app, keys, method, path):
    client = app.test_client()
    assert client.open(path, method=method).status_code == 403
    r = client.open(path, method=method, headers={"Authorization": f"Bearer {WRITE_SECRET}"})
    assert r.status_code == 401
    stranger = {JWT_HEADER: keys.sign("someone-else@example.org"), "Origin": ORIGIN}
    assert client.open(path, method=method, headers=stranger).status_code == 403


def test_cross_origin_writes_are_refused(app, keys):
    ok = {JWT_HEADER: keys.sign(ORGANIZER_A)}
    client = app.test_client()
    r = client.post("/api/workstreams", json={"name": "x"}, headers=ok)
    assert r.status_code == 403
    r = client.post("/api/workstreams", json={"name": "x"}, headers={**ok, "Origin": "http://evil"})
    assert r.status_code == 403
    assert client.get("/api/workstreams", headers=ok).json == []


# --- register_api is reusable --------------------------------------------------------------


def test_register_api_prefix_and_token_auth(cfg, keys):
    app = Flask(__name__)
    app.config["FP"] = cfg
    from festival_planner import auth

    db.init_app(app)
    auth.init_app(app)
    register_api(app, "/api", "web")
    register_api(app, "/mcp/api", "token")
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        kind = app.view_functions[rule.endpoint]._fp_auth
        if rule.rule.startswith("/api"):
            assert kind == "web", rule
        else:
            assert rule.rule.startswith("/mcp/api"), rule
            want = "read" if rule.methods & {"GET"} else "write"
            assert kind == want, (rule, kind)
    assert any(r.rule == "/mcp/api/digest" for r in app.url_map.iter_rules())
    assert len(app.blueprints) == 2
    with app.app_context():
        con = db.get_db()
        con.execute(
            "INSERT INTO event (id, name, event_date, timezone)"
            " VALUES (1, 'E', '2099-10-10', 'UTC')"
        )
        con.commit()
    client = app.test_client()
    assert client.get("/mcp/api/event", headers={"Authorization": "Bearer nope"}).status_code == 401
    # the mirror takes tokens only, with the token's scope deciding what it may do
    read = {"Authorization": f"Bearer {READ_SECRET}"}
    write = {"Authorization": f"Bearer {WRITE_SECRET}"}
    assert client.get("/mcp/api/workstreams", headers=read).json == []
    assert client.get("/mcp/api/digest", headers=read).status_code == 200
    r = client.post("/mcp/api/workstreams", json={"name": "Food"}, headers=read)
    assert r.status_code == 403  # a read token cannot write
    r = client.post("/mcp/api/workstreams", json={"name": "Food"}, headers=write)
    assert r.status_code == 201
    r = client.post("/mcp/api/workstreams", json={"name": "x", "bogus": 1}, headers=write)
    assert r.status_code == 400 and "bogus" in r.json["fields"]
    assert client.get("/mcp/api/workstreams/abc", headers=read).json["error"] == "not found"
    # the activity source is the token's name, never anything the client sends
    rows = client.get("/mcp/api/activity?entity_type=workstream", headers=read).json
    assert [(r["source"], r["actor"]) for r in rows] == [("claude", "claude")]
    jwt_header = {JWT_HEADER: keys.sign(ORGANIZER_A), "Origin": ORIGIN}
    assert client.get("/mcp/api/workstreams", headers=jwt_header).status_code == 401
    assert client.get("/api/workstreams", headers=write).status_code == 401
    # JSON errors only under a registered prefix
    assert client.get("/elsewhere").mimetype == "text/html"
    with pytest.raises(ValueError):
        register_api(app, "/api", "web")  # the same prefix twice would clash
    with pytest.raises(ValueError):
        register_api(app, "/other", "basic")


# --- what the UI's task screens rely on -----------------------------------------------------


def test_task_responses_include_effective_due(api):
    ws = make_ws(api)
    stone = api.post("/api/milestones", {"name": "T-30", "days_before_event": 30}).json
    own = make_task(api, ws, "Own date", due_date="2099-09-20", milestone_id=stone["id"])
    derived = make_task(api, ws, "From milestone", milestone_id=stone["id"])
    bare = make_task(api, ws, "No date")
    assert own["effective_due"] == "2099-09-20"
    assert derived["effective_due"] == "2099-09-10"  # the event date minus 30 days
    assert bare["effective_due"] is None
    assert api.get(f"/api/tasks/{derived['id']}").json["effective_due"] == "2099-09-10"
    listed = {t["title"]: t["effective_due"] for t in api.get("/api/tasks").json}
    assert listed == {"Own date": "2099-09-20", "From milestone": "2099-09-10", "No date": None}
    updated = api.patch(f"/api/tasks/{bare['id']}", {"due_date": "2099-09-01"}).json
    assert updated["effective_due"] == "2099-09-01"
    # Moving the event date moves a milestone-only task with it.
    api.put("/api/event", {"event_date": "2099-11-10"})
    assert api.get(f"/api/tasks/{derived['id']}").json["effective_due"] == "2099-10-11"


def test_organizers_lists_email_and_display_name_only(api):
    r = api.get("/api/organizers")
    assert r.status_code == 200
    assert [o["email"] for o in r.json] == [ORGANIZER_A, ORGANIZER_B]
    assert all(set(o) == {"email", "display_name"} for o in r.json)
