"""Authentication and authorization, decided by path (design §4.1, §4.3).

* Web routes (`/api/*`, `@requires("web")`) accept only a Cloudflare Access JWT, verified here
  independently of Access: RS256 against the team JWKS, `aud`, `iss`, `exp` and `iat`, and the
  email must be a configured organizer's primary address. Writes also need a same-origin
  `Origin` header (CSRF).
* Machine routes (`/mcp/*`, `@requires("read")` / `@requires("write")`) accept only a named,
  scoped token from `MCP_TOKENS`, as `Authorization: Bearer` or as a path token that Task 18's
  middleware moves into `environ[PATH_TOKEN_ENVIRON]`.
* Presenting the other kind of credential is a 401. Failures return a generic body; the reason
  goes only to the server log, and tokens and JWTs are never logged.

`g.actor` is set from the authenticated identity only: `Actor("web", email)` or
`Actor(token.name, token.name)`.
"""

from __future__ import annotations

import functools
import hmac
import json
import logging
import threading
import time
import urllib.request
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, get_args
from urllib.parse import urlsplit

import jwt
from flask import Flask, Response, current_app, g, jsonify, request
from jwt.algorithms import RSAAlgorithm

from festival_planner.service import Actor

if TYPE_CHECKING:
    from festival_planner.config import Config, Token

AuthKind = Literal["web", "read", "write"]
KINDS: tuple[str, ...] = get_args(AuthKind)

JWT_HEADER = "Cf-Access-Jwt-Assertion"
PATH_TOKEN_ENVIRON = "fp.path_token"  # noqa: S105 (an environ key name, not a secret)
JWT_LEEWAY_SECONDS = 30
JWKS_REFETCH_SECONDS = 300
JWKS_FETCH_TIMEOUT_SECONDS = 5
JWKS_MAX_BYTES = 1 << 20
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Routes that need no auth declaration (Global Constraints). Matched against the rule string.
PUBLIC_RULES = frozenset({"/healthz", "/", "/manifest.webmanifest", "/sw.js"})
PUBLIC_PREFIXES = ("/static/",)

_DENIAL_BODIES = {401: "unauthorized", 403: "forbidden", 404: "not found"}

log = logging.getLogger(__name__)


class AuthError(Exception):
    """A credential failed verification. The message is for the server log only."""


def is_public_rule(rule: str) -> bool:
    return rule in PUBLIC_RULES or rule.startswith(PUBLIC_PREFIXES)


# --- Access JWT ----------------------------------------------------------------------------

# Indirections so tests can replace the network fetch and the clock.
_monotonic: Callable[[], float] = time.monotonic


def _fetch_jwks(url: str) -> Any:
    """GET the team's certs endpoint and return the decoded JSON."""
    if not url.startswith("https://"):
        raise AuthError("JWKS URL must be https")
    # S310: the scheme is checked to be https above.
    req = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=JWKS_FETCH_TIMEOUT_SECONDS) as resp:  # noqa: S310
        body = resp.read(JWKS_MAX_BYTES + 1)
    if len(body) > JWKS_MAX_BYTES:
        raise AuthError("JWKS response too large")
    return json.loads(body)


def _parse_jwks(data: Any) -> dict[str, Any]:
    """Map kid -> RSA public key for every usable signing key in a JWKS document."""
    if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
        raise AuthError("JWKS document has no keys list")
    keys: dict[str, Any] = {}
    for jwk in data["keys"]:
        if not isinstance(jwk, dict) or jwk.get("kty") != "RSA":
            continue
        kid = jwk.get("kid")
        if not isinstance(kid, str) or not kid:
            continue
        if jwk.get("alg", "RS256") != "RS256" or jwk.get("use", "sig") != "sig":
            continue
        try:
            keys[kid] = RSAAlgorithm.from_jwk(jwk)
        except (jwt.InvalidKeyError, ValueError, TypeError, KeyError):
            continue
    return keys


class JwksCache:
    """Per-process cache of the team's signing keys.

    A known `kid` is served from memory. An unknown `kid` triggers a fetch, but fetches
    (successful or not) happen at most once every `JWKS_REFETCH_SECONDS`, so a stream of
    forged kids cannot turn the app into a request amplifier. A successful fetch replaces the
    whole key set, so keys Cloudflare has retired stop working after the next refetch.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._url: str | None = None
        self._keys: dict[str, Any] = {}
        self._last_fetch: float | None = None

    def get_key(self, url: str, kid: str) -> Any:
        with self._lock:
            if url != self._url:
                self._url, self._keys, self._last_fetch = url, {}, None
            key = self._keys.get(kid)
            if key is not None:
                return key
            now = _monotonic()
            if self._last_fetch is not None and now - self._last_fetch < JWKS_REFETCH_SECONDS:
                raise AuthError("unknown kid; JWKS refetch is rate-limited")
            self._last_fetch = now
            try:
                keys = _parse_jwks(_fetch_jwks(url))
            except AuthError:
                raise
            except Exception as exc:
                raise AuthError(f"JWKS fetch failed: {type(exc).__name__}") from None
            if keys:
                self._keys = keys
            key = self._keys.get(kid)
            if key is None:
                raise AuthError("unknown kid")
            return key


_jwks_cache = JwksCache()


def verify_access_jwt(token: str, cfg: Config) -> str:
    """Verify a Cloudflare Access JWT and return its email, lowercased. Raises `AuthError`."""
    team, aud = cfg.cf_team_domain, cfg.cf_aud
    if not team or not aud:
        raise AuthError("Cloudflare Access is not configured")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise AuthError(f"malformed JWT: {exc}") from None
    if header.get("alg") != "RS256":
        raise AuthError("JWT algorithm is not RS256")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise AuthError("JWT has no kid")
    key = _jwks_cache.get_key(f"https://{team}/cdn-cgi/access/certs", kid)
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=aud,
            issuer=f"https://{team}",
            leeway=JWT_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthError(f"JWT rejected: {exc}") from None
    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        raise AuthError("JWT has no email claim")
    return email.strip().lower()


# --- tokens --------------------------------------------------------------------------------


def match_token(secret: str, cfg: Config) -> Token | None:
    """The configured token whose secret equals `secret`, or None.

    Compares against every configured token with `hmac.compare_digest` and never stops early,
    so timing does not reveal which (or whether an earlier) token matched. Secrets are compared
    as UTF-8 bytes: `compare_digest` rejects non-ASCII `str` with a TypeError.
    """
    candidate = secret.encode("utf-8", "surrogatepass")
    found = None
    for token in cfg.mcp_tokens:
        if hmac.compare_digest(candidate, token.secret.encode("utf-8")):
            found = token
    return found


# --- the decorator -------------------------------------------------------------------------


def _deny(status: int, *, challenge: bool = False) -> Response:
    resp = jsonify(error=_DENIAL_BODIES[status])
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-store"
    if challenge:
        resp.headers["WWW-Authenticate"] = "Bearer"
    return resp


def _same_origin() -> bool:
    """True when the `Origin` header names this request's host.

    The scheme may be http or https: TLS ends at the tunnel, so the app itself sees http while
    the browser's origin is https. The host (with port) must match exactly.
    """
    origin = request.headers.get("Origin")
    if not origin:
        return False
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or parts.path or parts.query or parts.fragment:
        return False
    if not parts.netloc or "@" in parts.netloc:
        return False
    return parts.netloc.lower() == request.host.lower()


def _authenticate_web() -> Response | None:
    if request.headers.get("Authorization") is not None or request.environ.get(PATH_TOKEN_ENVIRON):
        log.warning("auth: token presented on a web route (%s)", request.endpoint)
        return _deny(401)
    cfg: Config | None = current_app.config.get("FP")
    if cfg is None:
        log.error("auth: no configuration loaded; refusing web request")
        return _deny(403)
    if cfg.dev_auth_email:
        email = cfg.dev_auth_email
    else:
        token = request.headers.get(JWT_HEADER)
        if not token:
            log.warning("auth: no Access JWT on %s", request.endpoint)
            return _deny(403)
        try:
            email = verify_access_jwt(token, cfg)
        except AuthError as exc:
            log.warning("auth: Access JWT rejected on %s: %s", request.endpoint, exc)
            return _deny(403)
    organizer = next((o.email for o in cfg.organizers if o.email == email), None)
    if organizer is None:
        log.warning("auth: %s is not an organizer's primary email", email)
        return _deny(403)
    if request.method in WRITE_METHODS and not _same_origin():
        log.warning("auth: cross-origin or missing Origin on %s %s", request.method, email)
        return _deny(403)
    g.actor = Actor("web", organizer)
    return None


def _authenticate_token(kind: str) -> Response | None:
    if request.headers.get(JWT_HEADER) is not None:
        log.warning("auth: Access JWT presented on a token route (%s)", request.endpoint)
        return _deny(401, challenge=True)
    cfg: Config | None = current_app.config.get("FP")
    header = request.headers.get("Authorization")
    if header is not None:
        scheme, _, secret = header.strip().partition(" ")
        token = None
        if scheme.lower() == "bearer" and cfg is not None:
            token = match_token(secret.strip(), cfg)
        if token is None:
            log.warning("auth: bad bearer token on %s", request.endpoint)
            return _deny(401, challenge=True)
    else:
        secret = request.environ.get(PATH_TOKEN_ENVIRON)
        if not secret:
            log.warning("auth: no token on %s", request.endpoint)
            return _deny(401, challenge=True)
        token = match_token(secret, cfg) if cfg is not None else None
        if token is None:
            # A bad path token looks like a missing page (design §4.3).
            log.warning("auth: bad path token on %s", request.endpoint)
            return _deny(404)
    if kind == "write" and token.scope != "write":
        log.warning("auth: token %s lacks write scope for %s", token.name, request.endpoint)
        return _deny(403)
    g.actor = Actor(token.name, token.name)
    g.token_scope = token.scope  # /mcp shows a read token only the read tools
    return None


def requires(kind: AuthKind) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare and enforce a route's auth. Sets `view._fp_auth = kind`.

    "web": an organizer's Access JWT (only for web routes such as `/api/*`).
    "read" / "write": a token with that scope (only for `/mcp/*`); "read" accepts either scope.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown auth kind {kind!r}; expected one of {KINDS}")

    def decorator(view: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(view)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            denial = _authenticate_web() if kind == "web" else _authenticate_token(kind)
            if denial is not None:
                return denial
            return view(*args, **kwargs)

        wrapper._fp_auth = kind  # type: ignore[attr-defined]
        return wrapper

    return decorator


def init_app(app: Flask) -> None:
    """Fail closed: refuse any matched route that is neither public nor `@requires`-declared.

    `tests/test_route_scopes.py` keeps such routes out of the app; this is the runtime backstop.
    """

    @app.before_request
    def _require_auth_declaration() -> Response | None:
        rule = request.url_rule
        if rule is None or is_public_rule(rule.rule):
            return None
        view = current_app.view_functions.get(request.endpoint or "")
        if getattr(view, "_fp_auth", None) not in KINDS:
            log.error("auth: route %s has no @requires declaration", rule.rule)
            return _deny(403)
        return None
