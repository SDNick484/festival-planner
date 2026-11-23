"""Every route declares its auth, and the declaration matches its path (Global Constraints).

`test_route_scopes` walks the real app's URL map, so any route a later task adds without
`@requires(...)`, or with the wrong kind for its path, fails here.

`/s/*` (volunteer share links, Task 24) is the one prefix reached without Access or a machine
token: its routes must be `@requires("share")`, and the "share" kind is allowed nowhere else.
"""

from __future__ import annotations

from access_keys import make_config
from flask import Flask

from festival_planner.auth import is_public_rule, requires
from festival_planner.web import create_app

SHARE_PREFIX = "/s/"


def scope_violations(app: Flask) -> list[str]:
    problems = []
    for rule in app.url_map.iter_rules():
        path = rule.rule
        if is_public_rule(path):
            continue
        kind = getattr(app.view_functions[rule.endpoint], "_fp_auth", None)
        if kind is None:
            problems.append(f"{path} ({rule.endpoint}): no @requires")
        elif path.startswith(SHARE_PREFIX):
            if kind != "share":
                problems.append(f"{path} ({rule.endpoint}): /s/* needs 'share', has {kind!r}")
        elif kind == "share":
            problems.append(f"{path} ({rule.endpoint}): 'share' outside /s/*")
        elif path == "/api" or path.startswith("/api/"):
            if kind != "web":
                problems.append(f"{path} ({rule.endpoint}): /api needs 'web', has {kind!r}")
        elif path == "/mcp" or path.startswith("/mcp/"):
            if kind not in ("read", "write"):
                problems.append(f"{path} ({rule.endpoint}): /mcp needs read/write, has {kind!r}")
        elif kind != "web":
            problems.append(f"{path} ({rule.endpoint}): token scope outside /mcp")
    return problems


def test_route_scopes(tmp_path):
    app = create_app(make_config(tmp_path))
    rules = [r.rule for r in app.url_map.iter_rules()]
    assert "/healthz" in rules
    assert [r for r in rules if r.startswith(SHARE_PREFIX)] == ["/s/redacted", "/s/redacted/data"]
    assert scope_violations(app) == []


def test_route_scope_check_catches_violations(tmp_path):
    app = create_app(make_config(tmp_path))

    def view():
        return ""

    app.add_url_rule("/api/ok", "api_ok", requires("web")(view))
    app.add_url_rule("/mcp/ok", "mcp_ok", requires("read")(view))
    app.add_url_rule("/api/missing", "api_missing", view)
    app.add_url_rule("/api/token", "api_token", requires("write")(view))
    app.add_url_rule("/mcp/jwt", "mcp_jwt", requires("web")(view))
    app.add_url_rule("/other", "other_token", requires("read")(view))
    app.add_url_rule("/static-ish", "static_ish", view)
    app.add_url_rule("/s/ok", "share_ok", requires("share")(view))
    app.add_url_rule("/s/web", "share_web", requires("web")(view))
    app.add_url_rule("/s/missing", "share_missing", view)
    app.add_url_rule("/api/share", "api_share", requires("share")(view))
    app.add_url_rule("/mcp/share", "mcp_share", requires("share")(view))
    app.add_url_rule("/elsewhere", "share_elsewhere", requires("share")(view))
    found = scope_violations(app)
    assert len(found) == 10, found
    for path in (
        "/api/missing",
        "/api/token",
        "/mcp/jwt",
        "/other",
        "/static-ish",
        "/s/web",
        "/s/missing",
        "/api/share",
        "/mcp/share",
        "/elsewhere",
    ):
        assert any(f.startswith(path + " ") for f in found), path


def test_public_allow_list_is_exact():
    for path in ("/healthz", "/", "/manifest.webmanifest", "/sw.js", "/static/<path:filename>"):
        assert is_public_rule(path), path
    for path in ("/healthz/x", "/static", "/staticfoo", "/api", "/mcp", "/index.html"):
        assert not is_public_rule(path), path
