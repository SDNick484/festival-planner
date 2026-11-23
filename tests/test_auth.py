"""Authentication and authorization (Task 6): Access JWTs, MCP tokens, CSRF, headers.

The app under test is the real `create_app` plus a few probe routes declared with `@requires`,
because the real `/api` and `/mcp` routes arrive in later tasks.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import jwt
import pytest
from access_keys import (
    ALIAS_A,
    ORGANIZER_A,
    ORGANIZER_B,
    READ_SECRET,
    WRITE_SECRET,
    AccessKeys,
    make_config,
)
from flask import g, jsonify

from festival_planner import auth
from festival_planner.auth import AuthError, match_token, requires, verify_access_jwt
from festival_planner.config import Token
from festival_planner.service import Actor
from festival_planner.web import create_app

JWT_HEADER = "Cf-Access-Jwt-Assertion"
SAME_ORIGIN = "http://localhost"
FORBIDDEN = {"error": "forbidden"}
UNAUTHORIZED = {"error": "unauthorized"}


WEB_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


def add_probe_routes(app):
    def who():
        actor: Actor = g.actor
        return jsonify(source=actor.source, actor=actor.actor)

    app.add_url_rule("/api/probe", "api_probe", requires("web")(who), methods=WEB_METHODS)
    app.add_url_rule("/mcp/probe", "mcp_read", requires("read")(who), methods=["GET"])
    app.add_url_rule("/mcp/probe", "mcp_write", requires("write")(who), methods=["POST"])
    app.add_url_rule("/undeclared", "undeclared", who)


@pytest.fixture
def keys(monkeypatch):
    return AccessKeys(monkeypatch)


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path)


@pytest.fixture
def client(cfg, keys):
    app = create_app(cfg)
    app.config.update(TESTING=True)
    add_probe_routes(app)
    return app.test_client()


def web_get(client, token):
    return client.get("/api/probe", headers={JWT_HEADER: token})


# --- Access JWT ----------------------------------------------------------------------------


def test_valid_jwt_allows(client, keys):
    r = web_get(client, keys.sign(ORGANIZER_A))
    assert r.status_code == 200
    assert r.json == {"source": "web", "actor": ORGANIZER_A}
    # Case-insensitive match; the actor is the configured (canonical) address.
    r = web_get(client, keys.sign(ORGANIZER_B.upper()))
    assert r.status_code == 200
    assert r.json == {"source": "web", "actor": ORGANIZER_B}


def test_wrong_aud_rejected(client, keys):
    r = web_get(client, keys.sign(ORGANIZER_A, aud="another-application"))
    assert r.status_code == 403
    assert r.json == FORBIDDEN


def test_expired_rejected(client, keys):
    r = web_get(client, keys.sign(ORGANIZER_A, exp_delta=-60))
    assert r.status_code == 403
    assert r.json == FORBIDDEN
    # Within the 30 s clock-skew allowance an expired token is still accepted.
    assert web_get(client, keys.sign(ORGANIZER_A, exp_delta=-10)).status_code == 200


def test_non_organizer_email_rejected(client, keys):
    stranger = "someone-else@example.org"
    r = web_get(client, keys.sign(stranger))
    assert r.status_code == 403
    assert r.json == FORBIDDEN
    assert stranger.encode() not in r.data
    assert b"organizer" not in r.data.lower()


def test_alias_email_cannot_log_in(client, keys):
    r = web_get(client, keys.sign(ALIAS_A))
    assert r.status_code == 403
    assert r.json == FORBIDDEN


def test_key_rotation_refetches_once(client, keys):
    assert web_get(client, keys.sign(ORGANIZER_A)).status_code == 200
    assert len(keys.fetches) == 1
    assert keys.fetches[0] == "https://team.example.com/cdn-cgi/access/certs"
    # Known kid: served from the cache.
    assert web_get(client, keys.sign(ORGANIZER_A)).status_code == 200
    assert len(keys.fetches) == 1

    keys.advance(auth.JWKS_REFETCH_SECONDS + 1)
    keys.rotate()
    # Unknown kid: one refetch picks up the new key.
    assert web_get(client, keys.sign(ORGANIZER_A)).status_code == 200
    assert len(keys.fetches) == 2
    # Further unknown kids inside the interval do not refetch (no amplification).
    for _ in range(3):
        r = web_get(client, keys.sign(ORGANIZER_A, kid="unknown-kid"))
        assert r.status_code == 403
    assert len(keys.fetches) == 2
    # A key rotated in during the interval is picked up once the interval has passed.
    keys.rotate()
    assert web_get(client, keys.sign(ORGANIZER_A)).status_code == 403
    assert len(keys.fetches) == 2
    keys.advance(auth.JWKS_REFETCH_SECONDS + 1)
    assert web_get(client, keys.sign(ORGANIZER_A)).status_code == 200
    assert len(keys.fetches) == 3


def test_jwks_fetch_failure_is_rate_limited(client, keys, monkeypatch):
    calls = []

    def broken(url):
        calls.append(url)
        raise OSError("unreachable")

    monkeypatch.setattr(auth, "_fetch_jwks", broken)
    for _ in range(3):
        assert web_get(client, keys.sign(ORGANIZER_A)).status_code == 403
    assert len(calls) == 1


def test_missing_jwt_rejected(client):
    r = client.get("/api/probe")
    assert r.status_code == 403
    assert r.json == FORBIDDEN


@pytest.mark.parametrize(
    "kwargs",
    [
        {"iss": "https://other-team.example.com"},
        {"omit": ("exp",)},
        {"omit": ("iat",)},
    ],
    ids=["wrong-iss", "no-exp", "no-iat"],
)
def test_bad_claims_rejected(client, keys, kwargs):
    r = web_get(client, keys.sign(ORGANIZER_A, **kwargs))
    assert r.status_code == 403
    assert r.json == FORBIDDEN


def test_missing_email_claim_rejected(client, keys):
    assert web_get(client, keys.sign(None)).status_code == 403


def test_algorithm_confusion_rejected(client, keys):
    claims = jwt.decode(keys.sign(ORGANIZER_A), options={"verify_signature": False})
    # HS256 signed with the published public key as the HMAC secret.
    public_pem = keys.public_pem()
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT", "kid": keys.kid}).encode())
    payload = b64url(json.dumps(claims).encode())
    mac = hmac.new(public_pem, f"{header}.{payload}".encode(), hashlib.sha256).digest()
    assert web_get(client, f"{header}.{payload}.{b64url(mac)}").status_code == 403
    # Unsigned, and garbage.
    unsigned = jwt.encode(claims, None, algorithm="none", headers={"kid": keys.kid})
    assert web_get(client, unsigned).status_code == 403
    assert web_get(client, "not-a-jwt").status_code == 403


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def test_verify_access_jwt_returns_email(cfg, keys):
    assert verify_access_jwt(keys.sign(ORGANIZER_A.upper()), cfg) == ORGANIZER_A
    with pytest.raises(AuthError):
        verify_access_jwt(keys.sign(ORGANIZER_A, aud="nope"), cfg)


def test_unconfigured_access_fails_closed(tmp_path, keys):
    app = create_app(make_config(tmp_path, cf_team_domain=None, cf_aud=None))
    add_probe_routes(app)
    r = app.test_client().get("/api/probe", headers={JWT_HEADER: keys.sign(ORGANIZER_A)})
    assert r.status_code == 403
    assert keys.fetches == []


def test_no_config_fails_closed(keys):
    app = create_app()
    add_probe_routes(app)
    c = app.test_client()
    assert c.get("/api/probe", headers={JWT_HEADER: keys.sign(ORGANIZER_A)}).status_code == 403
    r = c.get("/mcp/probe", headers={"Authorization": f"Bearer {READ_SECRET}"})
    assert r.status_code == 401


def test_dev_auth_email_skips_jwt(tmp_path, keys):
    app = create_app(make_config(tmp_path, dev_auth_email=ORGANIZER_B))
    add_probe_routes(app)
    c = app.test_client()
    r = c.get("/api/probe")
    assert r.status_code == 200
    assert r.json == {"source": "web", "actor": ORGANIZER_B}
    assert keys.fetches == []
    # The dev bypass does not relax the CSRF check or the token ban.
    assert c.post("/api/probe").status_code == 403
    r = c.get("/api/probe", headers={"Authorization": f"Bearer {WRITE_SECRET}"})
    assert r.status_code == 401


# --- tokens and path scoping ---------------------------------------------------------------


def test_token_rejected_on_api(client, keys):
    bearer = {"Authorization": f"Bearer {WRITE_SECRET}"}
    r = client.get("/api/probe", headers=bearer)
    assert r.status_code == 401
    assert r.json == UNAUTHORIZED
    # Even alongside a valid JWT, a token on /api is refused.
    r = client.get("/api/probe", headers={**bearer, JWT_HEADER: keys.sign(ORGANIZER_A)})
    assert r.status_code == 401


def test_jwt_rejected_on_mcp(client, keys):
    jwt_only = {JWT_HEADER: keys.sign(ORGANIZER_A)}
    r = client.get("/mcp/probe", headers=jwt_only)
    assert r.status_code == 401
    assert r.json == UNAUTHORIZED
    r = client.get("/mcp/probe", headers={**jwt_only, "Authorization": f"Bearer {READ_SECRET}"})
    assert r.status_code == 401


def test_token_allows_mcp_and_sets_actor(client):
    r = client.get("/mcp/probe", headers={"Authorization": f"Bearer {READ_SECRET}"})
    assert r.status_code == 200
    assert r.json == {"source": "duet", "actor": "duet"}
    r = client.post("/mcp/probe", headers={"Authorization": f"bearer {WRITE_SECRET}"})
    assert r.status_code == 200
    assert r.json == {"source": "claude", "actor": "claude"}


def test_read_token_cannot_write(client):
    r = client.post("/mcp/probe", headers={"Authorization": f"Bearer {READ_SECRET}"})
    assert r.status_code == 403
    assert r.json == FORBIDDEN


@pytest.mark.parametrize(
    "header",
    [None, "Bearer " + "x" * 32, "Basic " + WRITE_SECRET, "Bearer", "Bearer  ", "Bearer é" * 8],
    ids=["missing", "unknown", "wrong-scheme", "empty", "blank", "non-ascii"],
)
def test_bad_header_token_401(client, header):
    headers = {} if header is None else {"Authorization": header}
    r = client.get("/mcp/probe", headers=headers)
    assert r.status_code == 401
    assert r.json == UNAUTHORIZED


def test_path_token_from_environ(client):
    # Task 18's middleware moves a /mcp/<secret> path token into this environ key.
    ok = client.get("/mcp/probe", environ_base={auth.PATH_TOKEN_ENVIRON: READ_SECRET})
    assert ok.status_code == 200 and ok.json["actor"] == "duet"
    bad = client.get("/mcp/probe", environ_base={auth.PATH_TOKEN_ENVIRON: "z" * 32})
    assert bad.status_code == 404
    # The header wins over the path token, and a bad header is a 401.
    r = client.get(
        "/mcp/probe",
        headers={"Authorization": "Bearer " + "z" * 32},
        environ_base={auth.PATH_TOKEN_ENVIRON: READ_SECRET},
    )
    assert r.status_code == 401


def test_match_token_compares_every_token(cfg, monkeypatch):
    calls = []
    real = hmac.compare_digest

    def counting(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", counting)
    # The first configured token matches, yet every token is still compared.
    assert match_token(WRITE_SECRET, cfg) == Token("claude", "write", WRITE_SECRET)
    assert len(calls) == len(cfg.mcp_tokens)
    calls.clear()
    assert match_token("q" * 32, cfg) is None
    assert len(calls) == len(cfg.mcp_tokens)
    assert match_token("", cfg) is None
    assert match_token("é" * 30, cfg) is None


def test_match_token_with_shared_name(tmp_path):
    cfg = make_config(
        tmp_path,
        mcp_tokens=(Token("claude", "write", "o" * 30), Token("claude", "write", "n" * 30)),
    )
    assert match_token("n" * 30, cfg).name == "claude"
    assert match_token("o" * 30, cfg).name == "claude"


def test_requires_rejects_unknown_kind():
    with pytest.raises(ValueError):
        requires("admin")


def test_undeclared_route_fails_closed(client):
    # test_route_scopes keeps such a route out of the app; at runtime it is refused anyway.
    assert client.get("/undeclared").status_code == 403


# --- CSRF ----------------------------------------------------------------------------------


def test_cross_origin_write_rejected(client, keys):
    token = keys.sign(ORGANIZER_A)
    for origin in ("https://evil.example.net", "null", "http://localhost.example.net", None):
        headers = {JWT_HEADER: token}
        if origin is not None:
            headers["Origin"] = origin
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            r = client.open("/api/probe", method=method, headers=headers)
            assert r.status_code == 403, (method, origin)
            assert r.json == FORBIDDEN
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        r = client.open(
            "/api/probe", method=method, headers={JWT_HEADER: token, "Origin": SAME_ORIGIN}
        )
        assert r.status_code == 200, method
    # Reads do not need an Origin.
    assert web_get(client, token).status_code == 200


# --- headers -------------------------------------------------------------------------------

EXPECTED_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def test_security_headers_present(client, keys):
    responses = [
        client.get("/healthz"),
        client.get("/no-such-page"),
        client.get("/api/probe"),
        client.get("/mcp/probe"),
        web_get(client, keys.sign(ORGANIZER_A)),
    ]
    assert [r.status_code for r in responses] == [200, 404, 403, 401, 200]
    for r in responses:
        for name, value in EXPECTED_HEADERS.items():
            assert r.headers.get(name) == value, (r.request.path, name)
