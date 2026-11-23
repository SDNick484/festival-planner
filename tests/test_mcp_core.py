"""Task 18: the /mcp JSON-RPC endpoint, path-token middleware, tool registry, /mcp/api mirror."""

from __future__ import annotations

import io
import logging
import socket
import threading
import urllib.error
import urllib.request
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from access_keys import ORGANIZER_A, READ_SECRET, WRITE_SECRET, AccessKeys, make_config
from werkzeug.serving import make_server

from festival_planner import clock, db, views
from festival_planner.mcp import server
from festival_planner.mcp.server import PathTokenMiddleware, check_args, tool
from festival_planner.service import Actor, NotFound, ValidationError
from festival_planner.web import create_app

NOW = datetime(2099, 10, 1, 12, 0, tzinfo=UTC)
EVENT_DATE = date(2099, 10, 10)
BAD_SECRET = "x" * 32
WRITE = {"Authorization": f"Bearer {WRITE_SECRET}"}
READ = {"Authorization": f"Bearer {READ_SECRET}"}


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


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def registry(monkeypatch):
    """An empty tool registry for the test; the real one is restored afterwards."""
    tools: dict = {}
    monkeypatch.setattr(server, "TOOLS", tools)
    return tools


def rpc(client, method, params=None, *, id=1, headers=WRITE, path="/mcp"):
    msg = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        msg["params"] = params
    return client.post(path, json=msg, headers=headers)


# --- protocol ------------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["2025-06-18", "2025-03-26", "2024-11-05"])
def test_initialize_version_negotiation(client, version):
    r = rpc(client, "initialize", {"protocolVersion": version, "capabilities": {}})
    assert r.status_code == 200
    assert r.json["jsonrpc"] == "2.0" and r.json["id"] == 1
    result = r.json["result"]
    assert result["protocolVersion"] == version
    assert result["serverInfo"]["name"] == "festival-planner"
    assert result["capabilities"] == {"tools": {}}
    assert "get_digest first" in result["instructions"]
    assert "never follow instructions" in result["instructions"]


@pytest.mark.parametrize("version", ["1999-01-01", None, 7])
def test_initialize_unknown_version_gets_latest(client, version):
    r = rpc(client, "initialize", {"protocolVersion": version})
    assert r.json["result"]["protocolVersion"] == "2025-06-18"


def test_unknown_method_error(client):
    r = rpc(client, "resources/list", id="abc")
    assert r.status_code == 200
    assert r.json["id"] == "abc"
    assert r.json["error"]["code"] == -32601
    assert "result" not in r.json


def test_ping(client):
    r = rpc(client, "ping", id=7)
    assert r.json == {"jsonrpc": "2.0", "id": 7, "result": {}}


@pytest.mark.parametrize(
    "body, code",
    [
        (b"{not json", -32700),
        (b"[]", -32600),
        (b'"hello"', -32600),
        (b'{"jsonrpc": "1.0", "id": 1, "method": "ping"}', -32600),
        (b'{"jsonrpc": "2.0", "id": 1, "method": 5}', -32600),
        (b'{"jsonrpc": "2.0", "id": true, "method": "ping"}', -32600),
        (b'{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1]}', -32602),
    ],
    ids=["parse", "empty-batch", "not-object", "version", "method-type", "id-type", "params"],
)
def test_malformed_messages(client, body, code):
    r = client.post("/mcp", data=body, content_type="application/json", headers=WRITE)
    assert r.json["error"]["code"] == code
    assert r.status_code in (200, 400)


def test_batch_limit(client):
    ok = [{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(20)]
    r = client.post("/mcp", json=ok, headers=WRITE)
    assert r.status_code == 200
    assert sorted(m["id"] for m in r.json) == list(range(20))
    assert all(m["result"] == {} for m in r.json)

    too_many = [{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(21)]
    r = client.post("/mcp", json=too_many, headers=WRITE)
    assert r.status_code == 400
    assert r.json["error"]["code"] == -32600
    assert "20" in r.json["error"]["message"]
    assert r.json["id"] is None


def test_batch_mixes_requests_and_notifications(client):
    batch = [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "nope"},
        42,
    ]
    r = client.post("/mcp", json=batch, headers=WRITE)
    assert r.status_code == 200
    by_id = {m["id"]: m for m in r.json}
    assert by_id[1]["result"] == {}
    assert by_id[2]["error"]["code"] == -32601
    assert by_id[None]["error"]["code"] == -32600
    assert len(r.json) == 3


def test_notification_202(client):
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    r = client.post("/mcp", json=note, headers=WRITE)
    assert r.status_code == 202
    assert r.data == b""
    r = client.post(
        "/mcp", json=[note, {**note, "method": "notifications/cancelled"}], headers=WRITE
    )
    assert r.status_code == 202
    assert r.data == b""
    # A client's JSON-RPC response (for example to a server request) is accepted the same way.
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "result": {}}, headers=WRITE)
    assert r.status_code == 202


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_get_405(client, method):
    r = client.open("/mcp", method=method, headers=READ)
    assert r.status_code == 405
    assert r.headers["Allow"] == "POST"
    r = client.open(f"/mcp/{READ_SECRET}", method=method)
    assert r.status_code == 405
    assert r.headers["Allow"] == "POST"


def test_body_too_large_413(client):
    r = client.post("/mcp", data=b" " * (2 << 20), content_type="application/json", headers=WRITE)
    assert r.status_code == 413


# --- tokens --------------------------------------------------------------------------------


def test_header_token_ok_bad_401(client):
    assert rpc(client, "ping", headers=WRITE).status_code == 200
    assert rpc(client, "ping", headers=READ).status_code == 200
    for headers in ({"Authorization": f"Bearer {BAD_SECRET}"}, {}):
        r = rpc(client, "ping", headers=headers)
        assert r.status_code == 401
        assert r.headers["WWW-Authenticate"] == "Bearer"
        assert "result" not in (r.json or {})
    # The header wins over a valid path token.
    r = rpc(
        client,
        "ping",
        headers={"Authorization": f"Bearer {BAD_SECRET}"},
        path=f"/mcp/{WRITE_SECRET}",
    )
    assert r.status_code == 401


def test_path_token_ok_bad_404(client):
    r = rpc(client, "ping", headers={}, path=f"/mcp/{WRITE_SECRET}")
    assert r.status_code == 200 and r.json["result"] == {}
    r = rpc(client, "ping", headers={}, path=f"/mcp/{BAD_SECRET}")
    assert r.status_code == 404
    assert BAD_SECRET not in r.get_data(as_text=True)
    # The mirror takes the path token too.
    assert client.get(f"/mcp/{READ_SECRET}/api/digest").status_code == 200
    assert client.get(f"/mcp/{BAD_SECRET}/api/digest").status_code == 404
    # "api" (or any segment under 24 characters) is a path, not a token: no token -> 401.
    assert client.get("/mcp/api/digest").status_code == 401
    assert client.get("/mcp/" + "y" * 23).status_code == 404


def test_middleware_rewrites_path_and_raw_uri():
    seen = {}

    def inner(environ, start_response):
        seen.update(environ)
        start_response("200 OK", [])
        return [b""]

    mw = PathTokenMiddleware(inner)
    cases = [
        (f"/mcp/{WRITE_SECRET}", "/mcp", WRITE_SECRET),
        (f"/mcp/{WRITE_SECRET}/", "/mcp", WRITE_SECRET),
        (f"/mcp/{WRITE_SECRET}/api/tasks/3", "/mcp/api/tasks/3", WRITE_SECRET),
        ("/mcp/api/tasks", "/mcp/api/tasks", None),
        ("/mcp", "/mcp", None),
        ("/api/tasks", "/api/tasks", None),
    ]
    for path, want, token in cases:
        seen.clear()
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": path,
            "QUERY_STRING": "a=1",
            "RAW_URI": path + "?a=1",
            "REQUEST_URI": path + "?a=1",
        }
        mw(environ, lambda *a: None)
        assert seen["PATH_INFO"] == want
        assert seen.get("fp.path_token") == token
        if token:
            assert seen["RAW_URI"] == seen["REQUEST_URI"] == want + "?a=1"
            assert token not in seen["RAW_URI"]
        # The caller's environ dict itself is rewritten: gunicorn logs from that very dict.
        assert environ["PATH_INFO"] == want


def _gunicorn_request(app, raw: bytes, capture: list[str]) -> bytes:
    """Serve one raw HTTP request the way gunicorn's sync worker does, and log it."""
    from gunicorn.config import Config as GConfig
    from gunicorn.glogging import Logger
    from gunicorn.http import wsgi
    from gunicorn.http.parser import RequestParser

    gcfg = GConfig()
    gcfg.set("accesslog", "-")
    glog = Logger(gcfg)

    class Capture(logging.Handler):
        def emit(self, record):
            capture.append(record.getMessage())

    handler = Capture()
    glog.access_log.addHandler(handler)
    ours, theirs = socket.socketpair()
    try:
        theirs.sendall(raw)
        theirs.shutdown(socket.SHUT_WR)
        req = next(iter(RequestParser(gcfg, ours, ("127.0.0.1", 5000))))
        resp, environ = wsgi.create(req, ours, ("127.0.0.1", 5000), ("127.0.0.1", 8000), gcfg)
        resp.force_close()
        body = app(environ, resp.start_response)
        try:
            for chunk in body:
                resp.write(chunk)
            resp.close()
        finally:
            glog.access(resp, req, environ, timedelta(milliseconds=1))
            if hasattr(body, "close"):
                body.close()
        ours.shutdown(socket.SHUT_WR)
        out = io.BytesIO()
        while chunk := theirs.recv(65536):
            out.write(chunk)
        return out.getvalue()
    finally:
        glog.access_log.removeHandler(handler)
        ours.close()
        theirs.close()


def test_path_token_not_in_access_log(app, caplog):
    secrets = (WRITE_SECRET, READ_SECRET, BAD_SECRET)

    # gunicorn (the production server): its access log reads RAW_URI and PATH_INFO from the
    # environ the app was called with, so the rewritten path is what it records.
    lines: list[str] = []
    ping = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
    for secret in (WRITE_SECRET, BAD_SECRET):
        raw = (
            f"POST /mcp/{secret}?x=1 HTTP/1.1\r\nHost: localhost\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(ping)}\r\n\r\n"
        ).encode() + ping
        out = _gunicorn_request(app, raw, lines)
        assert out.startswith(b"HTTP/1.1 200" if secret == WRITE_SECRET else b"HTTP/1.1 404")
    out = _gunicorn_request(
        app, f"GET /mcp/{READ_SECRET}/api/digest HTTP/1.1\r\n\r\n".encode(), lines
    )
    assert out.startswith(b"HTTP/1.1 200")
    assert len(lines) == 3
    assert '"POST /mcp?x=1 HTTP/1.1" 200' in lines[0]
    assert '"POST /mcp?x=1 HTTP/1.1" 404' in lines[1]
    assert '"GET /mcp/api/digest HTTP/1.1" 200' in lines[2]

    # werkzeug's server (development): its request line comes from the raw socket, so a log
    # filter redacts it.
    caplog.set_level(logging.DEBUG)
    srv = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        req = urllib.request.Request(  # noqa: S310 (a local http URL)
            f"{base}/mcp/{WRITE_SECRET}", data=ping, headers={"Content-Type": "application/json"}
        )
        with closing(urllib.request.urlopen(req, timeout=5)) as resp:  # noqa: S310
            assert resp.status == 200
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/mcp/{BAD_SECRET}/api/digest", timeout=5)  # noqa: S310
        assert exc.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(5)
    werkzeug = [r.getMessage() for r in caplog.records if r.name == "werkzeug"]
    assert len(werkzeug) >= 2
    assert any("POST /mcp/" in m for m in werkzeug)

    everything = lines + [r.getMessage() for r in caplog.records]
    for secret in secrets:
        assert not [m for m in everything if secret in m]


# --- the /mcp/api mirror -------------------------------------------------------------------


def test_mcp_api_digest_with_token(app, client, cfg):
    r = client.get("/mcp/api/digest", headers=READ)
    assert r.status_code == 200
    with closing(db.connect(app.config["DATABASE"])) as con:
        assert r.json == views.build_digest(con, cfg)
    assert client.get("/mcp/api/digest", headers=WRITE).json == r.json
    assert client.get(f"/mcp/{READ_SECRET}/api/digest").json == r.json
    assert client.get("/mcp/api/digest").status_code == 401


def test_read_token_cannot_write_via_mcp_api(client):
    r = client.post("/mcp/api/workstreams", json={"name": "Food"}, headers=READ)
    assert r.status_code == 403
    r = client.post(f"/mcp/{READ_SECRET}/api/workstreams", json={"name": "Food"})
    assert r.status_code == 403
    assert client.get("/mcp/api/workstreams", headers=READ).json == []
    r = client.post("/mcp/api/workstreams", json={"name": "Food"}, headers=WRITE)
    assert r.status_code == 201
    ws = r.json["id"]
    assert client.delete(f"/mcp/api/workstreams/{ws}", headers=READ).status_code == 403
    assert (
        client.patch(f"/mcp/api/workstreams/{ws}", json={"name": "x"}, headers=READ).status_code
        == 403
    )
    assert client.get(f"/mcp/api/workstreams/{ws}", headers=READ).json["name"] == "Food"


def test_mcp_api_refuses_access_jwt(client, keys):
    r = client.get("/mcp/api/digest", headers={"Cf-Access-Jwt-Assertion": keys.sign(ORGANIZER_A)})
    assert r.status_code == 401


def test_actor_is_token_name(app, client, registry):
    # Over the REST mirror: the activity row names the token, whatever the body claims.
    r = client.post("/mcp/api/workstreams", json={"name": "Food"}, headers=WRITE)
    assert r.status_code == 201
    ws = r.json["id"]
    r = client.patch(
        f"/mcp/api/workstreams/{ws}", json={"name": "x", "source": "web"}, headers=WRITE
    )
    assert r.status_code == 400
    r = client.get(f"/mcp/api/activity?entity_type=workstream&entity_id={ws}", headers=READ)
    assert r.status_code == 200 and r.json
    assert {(a["source"], a["actor"]) for a in r.json} == {("claude", "claude")}

    # Over tools/call: the tool receives the token's identity, never one from the arguments.
    seen = []

    @tool("whoami", "read", "Who is calling.", {"type": "object", "properties": {}})
    def whoami(con, cfg, actor):
        seen.append(actor)
        return {"source": actor.source, "actor": actor.actor}

    r = rpc(client, "tools/call", {"name": "whoami", "arguments": {}}, headers=READ)
    assert r.json["result"]["structuredContent"] == {"source": "duet", "actor": "duet"}
    assert seen == [Actor("duet", "duet")]
    r = rpc(client, "tools/call", {"name": "whoami", "arguments": {"actor": "web"}})
    assert r.json["error"]["code"] == -32602
    assert seen == [Actor("duet", "duet")]


# --- tools ---------------------------------------------------------------------------------


def test_tools_list_empty_registry(client, registry):
    r = rpc(client, "tools/list")
    assert r.json["result"] == {"tools": []}


def test_tools_list_and_call(client, registry):
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer", "minimum": 1}},
        "required": ["n"],
        "additionalProperties": False,
    }

    @tool("double", "read", "Doubles n.", schema)
    def double(con, cfg, actor, n):
        return {"value": n * 2}

    @tool("poke", "write", "A write tool.", {"type": "object"})
    def poke(con, cfg, actor):
        return {"poked": True}

    @tool("strict", "write", "Raises service errors.", {"type": "object"})
    def strict(con, cfg, actor):
        raise ValidationError({"title": "required"})

    @tool("missing", "read", "Raises NotFound.", {"type": "object"})
    def missing(con, cfg, actor):
        raise NotFound("task 9")

    @tool("broken", "read", "Raises a bug.", {"type": "object"})
    def broken(con, cfg, actor):
        raise RuntimeError("secret detail")

    listed = rpc(client, "tools/list").json["result"]["tools"]
    assert {t["name"] for t in listed} == {"double", "poke", "strict", "missing", "broken"}
    entry = next(t for t in listed if t["name"] == "double")
    assert entry == {"name": "double", "description": "Doubles n.", "inputSchema": schema}
    # A read token does not see write tools, and cannot call them.
    names = {t["name"] for t in rpc(client, "tools/list", headers=READ).json["result"]["tools"]}
    assert names == {"double", "missing", "broken"}
    r = rpc(client, "tools/call", {"name": "poke"}, headers=READ)
    assert "error" in r.json and "result" not in r.json
    assert rpc(client, "tools/call", {"name": "poke"}).json["result"]["structuredContent"] == {
        "poked": True
    }

    r = rpc(client, "tools/call", {"name": "double", "arguments": {"n": 4}})
    result = r.json["result"]
    assert result["structuredContent"] == {"value": 8}
    assert result["content"] == [{"type": "text", "text": '{"value": 8}'}]
    assert not result.get("isError")
    for bad in ({}, {"n": 0}, {"n": "4"}, {"n": 4, "extra": 1}, {"n": True}):
        r = rpc(client, "tools/call", {"name": "double", "arguments": bad})
        assert r.json["error"]["code"] == -32602, bad
    r = rpc(client, "tools/call", {"name": "double", "arguments": [4]})
    assert r.json["error"]["code"] == -32602
    r = rpc(client, "tools/call", {"name": "nope"})
    assert r.json["error"]["code"] == -32602
    r = rpc(client, "tools/call", {"arguments": {}})
    assert r.json["error"]["code"] == -32602

    r = rpc(client, "tools/call", {"name": "strict"}).json["result"]
    assert r["isError"] is True and "title" in r["content"][0]["text"]
    r = rpc(client, "tools/call", {"name": "missing"}).json["result"]
    assert r["isError"] is True
    r = rpc(client, "tools/call", {"name": "broken"})
    assert r.status_code == 200
    assert r.json["error"]["code"] == -32603
    assert "secret detail" not in r.get_data(as_text=True)


def test_tool_registration_rules(registry):
    @tool("one", "read", "x", {"type": "object"})
    def one(con, cfg, actor):
        return {}

    with pytest.raises(ValueError):
        tool("one", "read", "x", {"type": "object"})(one)
    with pytest.raises(ValueError):
        tool("two", "admin", "x", {"type": "object"})(one)
    with pytest.raises(ValueError):
        tool("three", "read", "x", {"type": "string"})(one)


SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "integer", "minimum": 1, "maximum": 10},
        "title": {"type": "string", "maxLength": 5},
        "status": {"type": "string", "enum": ["todo", "done"]},
        "flag": {"type": "boolean"},
        "score": {"type": "number"},
        "tags": {"type": "array"},
        "extra": {"type": "object"},
        "maybe": {"type": ["string", "null"]},
    },
    "required": ["id"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    "args",
    [
        {"id": 1},
        {"id": 10, "title": "abcde", "status": "done", "flag": False, "score": 1.5},
        {"id": 2, "score": 3, "tags": [], "extra": {}, "maybe": None},
        {"id": 2, "maybe": "x"},
    ],
)
def test_check_args_accepts(args):
    check_args(SCHEMA, args)


@pytest.mark.parametrize(
    "args, field",
    [
        ({}, "id"),
        ({"id": 0}, "id"),
        ({"id": 11}, "id"),
        ({"id": 1.5}, "id"),
        ({"id": True}, "id"),
        ({"id": "1"}, "id"),
        ({"id": 1, "title": "abcdef"}, "title"),
        ({"id": 1, "title": 5}, "title"),
        ({"id": 1, "status": "doing"}, "status"),
        ({"id": 1, "flag": 1}, "flag"),
        ({"id": 1, "score": True}, "score"),
        ({"id": 1, "tags": {}}, "tags"),
        ({"id": 1, "maybe": 3}, "maybe"),
        ({"id": 1, "other": 1}, "other"),
    ],
)
def test_check_args_rejects(args, field):
    with pytest.raises(ValidationError) as exc:
        check_args(SCHEMA, args)
    assert field in exc.value.errors


def test_check_args_rejects_non_object():
    with pytest.raises(ValidationError):
        check_args(SCHEMA, [1])
