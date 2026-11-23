"""Volunteer read-only share links (Task 24).

A share link is a high-entropy token in a `/s/<token>` URL. Only its SHA-256 is stored. The public
page and its `/data` endpoint show tasks (title, status, effective due date, owner display name)
and milestones for the link's scope, and nothing else. Unknown, expired and revoked links all get
the same 404. Organizers create, list and revoke links through `/api/share-links` (web auth only).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, date, datetime, timedelta

import pytest
from access_keys import ORGANIZER_A, ORGANIZER_B, WRITE_SECRET, AccessKeys, make_config

from festival_planner import clock, db, ui
from festival_planner.api.share import ShareTokenMiddleware
from festival_planner.mcp.server import RedactPathTokenFilter
from festival_planner.service import Actor, NotFound, ValidationError, share
from festival_planner.web import create_app

JWT_HEADER = "Cf-Access-Jwt-Assertion"
ORIGIN = "http://localhost"
NOW = datetime(2099, 10, 1, 12, 0, tzinfo=UTC)
EVENT_DATE = date(2099, 10, 10)
ACTOR = Actor("web", ORGANIZER_A)

# Everything /s/<token>/data may contain, at any depth.
ALLOWED_KEYS = {"tasks", "milestones", "title", "status", "effective_due", "owner", "name", "date"}
SENSITIVE_KEYS = {"notes", "emails", "contacts", "links", "waiting_on", "blocked_reason"}


@pytest.fixture
def keys(monkeypatch):
    return AccessKeys(monkeypatch)


@pytest.fixture
def now(monkeypatch):
    """The app's clock; tests move it by assigning `now.value`."""

    class Clock:
        value = NOW

    monkeypatch.setattr(clock, "now", lambda: Clock.value)
    return Clock


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path, event_date=EVENT_DATE)


@pytest.fixture
def app(cfg, keys, now):
    app = create_app(cfg)
    app.config.update(TESTING=True)
    return app


@pytest.fixture
def con(app):
    con = db.connect(app.config["DATABASE"])
    yield con
    con.close()


class Api:
    def __init__(self, app, keys):
        self.client = app.test_client()
        self.keys = keys

    def call(self, method, path, body=None):
        headers = {JWT_HEADER: self.keys.sign(ORGANIZER_A), "Origin": ORIGIN}
        return self.client.open(path, method=method, headers=headers, json=body)

    def make(self, path, body):
        r = self.call("POST", path, body)
        assert r.status_code == 201, r.json
        return r.json


@pytest.fixture
def api(app, keys):
    return Api(app, keys)


@pytest.fixture
def public(app):
    """A client with no credentials at all, like a volunteer's browser."""
    return app.test_client()


def make_link(api, label="Volunteers", workstream_id=None, days=30):
    return api.make(
        "/api/share-links", {"label": label, "workstream_id": workstream_id, "days": days}
    )


def all_keys(value) -> set[str]:
    found: set[str] = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            found.update(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def assert_share_headers(resp):
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Referrer-Policy"] == "no-referrer"


@pytest.fixture
def unknown_404(public):
    """The 404 for a token that never existed: every other refusal must look exactly like it."""
    page = public.get("/s/" + "x" * 43)
    data = public.get("/s/" + "x" * 43 + "/data")
    assert page.status_code == data.status_code == 404
    assert page.mimetype == "text/html" and b"Link not available" in page.get_data()
    assert data.json == {"error": "not found"}
    return page.get_data(), data.get_data()


def assert_same_404(public, token, unknown_404):
    page = public.get(f"/s/{token}")
    data = public.get(f"/s/{token}/data")
    assert (page.status_code, data.status_code) == (404, 404)
    assert (page.get_data(), data.get_data()) == unknown_404
    assert_share_headers(page)
    assert_share_headers(data)


# --- the five contract tests ---------------------------------------------------------------


def test_share_excludes_sensitive_fields(api, public):
    ws = api.make("/api/workstreams", {"name": "Vendors", "owner": ORGANIZER_B})["id"]
    stone = api.make("/api/milestones", {"name": "Order tents", "days_before_event": 30})
    stone_notes = "milestone secret notes"
    api.call("PATCH", f"/api/milestones/{stone['id']}", {"notes": stone_notes})
    api.make(
        "/api/tasks",
        {
            "workstream_id": ws,
            "title": "Book tent",
            "owner": ORGANIZER_A,
            "status": "waiting",
            "waiting_on": "waiting secret",
            "notes": "task secret notes",
            "links": [{"label": "quote", "url": "https://example.com/quote"}],
            "milestone_id": stone["id"],
        },
    )
    api.make(
        "/api/tasks",
        {"workstream_id": ws, "title": "Permit", "status": "blocked", "blocked_reason": "x-secret"},
    )
    api.make(
        "/api/contacts",
        {"name": "Tent Contact", "emails": ["tents@example.com"], "workstream_id": ws},
    )
    link = make_link(api)

    r = public.get(f"/s/{link['token']}/data")
    assert r.status_code == 200
    assert_share_headers(r)
    body = r.json
    keys = all_keys(body)
    assert keys <= ALLOWED_KEYS, keys - ALLOWED_KEYS
    assert not keys & SENSITIVE_KEYS
    assert set(body) == {"tasks", "milestones"}
    assert all(set(t) == {"title", "status", "effective_due", "owner"} for t in body["tasks"])
    assert all(set(m) == {"name", "date"} for m in body["milestones"])
    tasks = {t["title"]: t for t in body["tasks"]}
    assert tasks["Book tent"] == {
        "title": "Book tent",
        "status": "waiting",
        "effective_due": (EVENT_DATE - timedelta(days=30)).isoformat(),
        "owner": "Organizer A",
    }
    assert tasks["Permit"]["owner"] is None
    assert body["milestones"] == [
        {"name": "Order tents", "date": (EVENT_DATE - timedelta(days=30)).isoformat()}
    ]
    text = json.dumps(body)
    for secret in ("secret", "@", "example.com", "Tent Contact", "Vendors"):
        assert secret not in text, secret


def test_revoked_404(api, public, unknown_404):
    link = make_link(api)
    assert public.get(f"/s/{link['token']}/data").status_code == 200
    r = api.call("POST", f"/api/share-links/{link['id']}/revoke")
    assert r.status_code == 200 and r.json["revoked_at"] is not None
    assert_same_404(public, link["token"], unknown_404)
    listed = {row["id"]: row for row in api.call("GET", "/api/share-links").json}
    assert listed[link["id"]]["state"] == "revoked"


def test_expired_404(api, public, now, unknown_404):
    link = make_link(api, days=1)
    now.value = NOW + timedelta(days=1) - timedelta(seconds=1)
    assert public.get(f"/s/{link['token']}").status_code == 200
    now.value = NOW + timedelta(days=1)
    assert_same_404(public, link["token"], unknown_404)
    listed = {row["id"]: row for row in api.call("GET", "/api/share-links").json}
    assert listed[link["id"]]["state"] == "expired"


def test_scope_limited_to_workstream(api, public):
    ws1 = api.make("/api/workstreams", {"name": "Food"})["id"]
    ws2 = api.make("/api/workstreams", {"name": "Games"})["id"]
    used = api.make("/api/milestones", {"name": "Menu set", "days_before_event": 14})
    api.make("/api/milestones", {"name": "Prizes bought", "days_before_event": 7})
    api.make("/api/tasks", {"workstream_id": ws1, "title": "Plan menu", "milestone_id": used["id"]})
    api.make("/api/tasks", {"workstream_id": ws1, "title": "Buy napkins"})
    api.make("/api/tasks", {"workstream_id": ws2, "title": "Ring toss"})

    scoped = public.get(f"/s/{make_link(api, workstream_id=ws1)['token']}/data").json
    assert sorted(t["title"] for t in scoped["tasks"]) == ["Buy napkins", "Plan menu"]
    assert [m["name"] for m in scoped["milestones"]] == ["Menu set"]

    everything = public.get(f"/s/{make_link(api)['token']}/data").json
    assert sorted(t["title"] for t in everything["tasks"]) == [
        "Buy napkins",
        "Plan menu",
        "Ring toss",
    ]
    assert [m["name"] for m in everything["milestones"]] == ["Menu set", "Prizes bought"]


def test_raw_token_not_stored(api, con):
    link = make_link(api, label="Gate crew")
    token = link["token"]
    assert len(token) >= 43
    row = con.execute("SELECT * FROM share_link WHERE id = ?", (link["id"],)).fetchone()
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    # The raw token is nowhere in the database: not in any table, column or activity diff.
    tables = [
        r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    ]
    for table in tables:
        for values in con.execute(f'SELECT * FROM "{table}"').fetchall():  # noqa: S608
            assert not any(token in str(v) for v in values), table
    # The hash is not written to the activity log either.
    diffs = " ".join(
        r[0] or ""
        for r in con.execute("SELECT diff FROM activity WHERE entity_type = 'share_link'")
    )
    assert row["token_hash"] not in diffs and "Gate crew" in diffs
    # It is shown once: listing never returns the token or its hash.
    listed = api.call("GET", "/api/share-links").json
    assert listed and not all_keys(listed) & {"token", "token_hash"}
    assert token not in json.dumps(listed)


# --- the page ------------------------------------------------------------------------------


def test_share_page_loads_script_file(api, public):
    link = make_link(api)
    assert link["url"] == f"/s/{link['token']}"
    r = public.get(link["url"])
    assert r.status_code == 200 and r.mimetype == "text/html"
    assert_share_headers(r)
    html = r.get_data(as_text=True)
    version = ui.asset_version()
    assert f'src="/static/share.js?v={version}"' in html
    assert "<script>" not in html and not re.search(r"\son\w+=", html)
    assert "unsafe-inline" not in r.headers["Content-Security-Policy"]
    # The page is static: the token is not echoed into it.
    assert link["token"] not in html


def test_share_routes_get_only_and_ignore_trailing_paths(api, public, unknown_404):
    link = make_link(api)
    assert public.post(f"/s/{link['token']}/data").status_code == 405
    assert public.get(f"/s/{link['token']}/other").status_code == 404
    assert public.get("/s/").status_code == 404
    assert_same_404(public, link["token"][:-1], unknown_404)


# --- management ----------------------------------------------------------------------------


def test_management_requires_web_auth_and_is_not_on_mcp(app, api, public):
    assert public.get("/api/share-links").status_code == 403
    assert public.post("/api/share-links", json={"label": "x", "days": 1}).status_code == 403
    bearer = {"Authorization": f"Bearer {WRITE_SECRET}"}
    r = app.test_client().get("/mcp/api/share-links", headers=bearer)
    assert r.status_code == 404
    rules = [rule.rule for rule in app.url_map.iter_rules()]
    assert not [r for r in rules if r.startswith("/mcp") and ("share" in r or r.startswith("/s/"))]
    assert "/api/share-links" in rules


def test_create_validates_and_returns_token_once(api):
    ws = api.make("/api/workstreams", {"name": "Food"})["id"]
    link = make_link(api, label="  Food helpers ", workstream_id=ws, days=7)
    assert link["label"] == "Food helpers" and link["workstream_id"] == ws
    assert link["expires_at"] == "2099-10-08T12:00:00Z" and link["state"] == "active"
    assert re.fullmatch(r"[A-Za-z0-9_-]{43,}", link["token"])
    assert make_link(api)["token"] != link["token"]
    for bad in (
        {"label": "", "days": 7},
        {"label": "x", "days": 0},
        {"label": "x", "days": 366},
        {"label": "x", "days": "7"},
        {"label": "x", "days": 7, "workstream_id": 999},
        {"label": "x", "days": 7, "token": "mine"},
    ):
        r = api.call("POST", "/api/share-links", bad)
        assert r.status_code == 400, bad
    assert api.call("POST", "/api/share-links/999/revoke").status_code == 404


def test_revoke_is_logged_and_idempotent(con, cfg):
    link_id, token = share.create(con, ACTOR, "Crew", None, 3)
    assert share.lookup(con, token)["id"] == link_id
    first = share.revoke(con, ACTOR, link_id)
    again = share.revoke(con, ACTOR, link_id)
    assert first["revoked_at"] == again["revoked_at"]
    assert share.lookup(con, token) is None
    actions = [
        r[0]
        for r in con.execute(
            "SELECT action FROM activity WHERE entity_type = 'share_link' ORDER BY id"
        )
    ]
    assert actions == ["create", "revoke"]
    with pytest.raises(NotFound):
        share.revoke(con, ACTOR, link_id + 1)
    with pytest.raises(ValidationError):
        share.create(con, ACTOR, "Crew", None, True)


def test_link_to_deleted_workstream_is_404(api, public, unknown_404):
    ws = api.make("/api/workstreams", {"name": "Food"})["id"]
    link = make_link(api, workstream_id=ws)
    assert api.call("DELETE", f"/api/workstreams/{ws}").status_code == 204
    assert_same_404(public, link["token"], unknown_404)


# --- the token stays out of logs -----------------------------------------------------------


def test_middleware_moves_token_out_of_the_logged_path():
    seen = {}

    def inner(environ, start_response):
        seen.update(environ)
        return []

    token = "t" * 43
    environ = {
        "PATH_INFO": f"/s/{token}/data",
        "RAW_URI": f"/s/{token}/data?x=1",
        "REQUEST_URI": f"/s/{token}/data?x=1",
        "QUERY_STRING": "x=1",
        "SCRIPT_NAME": "",
    }
    ShareTokenMiddleware(inner)(environ, lambda *a: None)
    assert seen["fp.share_token"] == token
    assert seen["PATH_INFO"] == "/s/redacted/data"
    assert seen["RAW_URI"] == seen["REQUEST_URI"] == "/s/redacted/data?x=1"
    # gunicorn logs from the dict it handed over, so the rewrite happens in place.
    assert token not in json.dumps({k: v for k, v in environ.items() if k != "fp.share_token"})

    other = {"PATH_INFO": "/static/share.js", "RAW_URI": "/static/share.js"}
    ShareTokenMiddleware(inner)(other, lambda *a: None)
    assert other == {"PATH_INFO": "/static/share.js", "RAW_URI": "/static/share.js"}


def test_dev_server_log_line_redacted():
    token = "Ab3_-" * 9
    record = logging.LogRecord(
        "werkzeug",
        logging.INFO,
        __file__,
        1,
        '127.0.0.1 - - "GET /s/%s/data HTTP/1.1" 200 -',
        (token,),
        None,
    )
    RedactPathTokenFilter().filter(record)
    assert token not in record.getMessage()
    assert "/s/<redacted>/data" in record.getMessage()
