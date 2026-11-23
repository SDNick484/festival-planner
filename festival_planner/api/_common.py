"""Plumbing shared by the API handler modules: routing with auth, request parsing and errors.

Handlers are thin adapters. They parse the request, call the service layer with `g.actor` (set by
`@requires` from the authenticated identity, never from the request) and return JSON. Every
failure becomes `{"error": str, "fields": {..}}` with a 4xx status; nothing a client sends may
produce a 500, and a 500 never carries a traceback.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable, Collection, Mapping
from typing import Any

from flask import Blueprint, Flask, Response, current_app, jsonify, request
from werkzeug.exceptions import HTTPException

from festival_planner.auth import requires
from festival_planner.config import Config
from festival_planner.service import NotFound, ValidationError

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 1 << 20
INT64_MAX = 2**63 - 1
INT64_MIN = -(2**63)
# A path id is a positive int64. Anything else (letters, 0, a sign, too large) is a routing 404.
ID = f"<int(min=1, max={INT64_MAX}):id>"
_INT_RE = re.compile(r"-?\d{1,19}")
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_PREFIXES_KEY = "fp_api_prefixes"


class BadRequest(ValidationError):
    """A 400 whose message says what was wrong with the request as a whole."""

    def __init__(self, message: str, errors: dict[str, str] | None = None):
        super().__init__(errors or {})
        self.message = message


class Router:
    """Registers view functions on one blueprint with the auth kind its mount point needs.

    `auth="web"`: every route is `@requires("web")` (an Access JWT). `auth="token"`: GET routes
    are `@requires("read")` and the rest `@requires("write")` (a named, scoped token).
    """

    def __init__(self, blueprint: Blueprint, auth: str):
        self.blueprint = blueprint
        self.auth = auth

    def route(self, rule: str, method: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        if self.auth == "web":
            kind = "web"
        else:
            kind = "read" if method == "GET" else "write"

        def decorator(view: Callable[..., Any]) -> Callable[..., Any]:
            guarded = requires(kind)(view)  # type: ignore[arg-type]
            self.blueprint.add_url_rule(
                rule, endpoint=view.__name__, view_func=guarded, methods=[method]
            )
            return view

        return decorator


# --- context -------------------------------------------------------------------------------


def config() -> Config:
    cfg = current_app.config.get("FP")
    if cfg is None:  # unreachable behind @requires, which refuses requests without a config
        raise RuntimeError("no configuration loaded")
    return cfg


def organizer_emails() -> list[str]:
    return [o.email for o in config().organizers]


# --- request bodies ------------------------------------------------------------------------


def _has_surrogate(value: Any) -> bool:
    """True if any string or key in the JSON value holds a lone surrogate (SQLite cannot store
    it, so it would otherwise surface as a 500). Iterative: nesting depth is attacker-chosen."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if _SURROGATE_RE.search(item):
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def json_body() -> Any:
    """The request body parsed as JSON. Anything else is a 400 (too large: 413)."""
    request.max_content_length = MAX_BODY_BYTES
    raw = request.get_data()  # raises RequestEntityTooLarge (413) past the limit
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):  # bad JSON, bad UTF-8, or absurd nesting
        raise BadRequest("request body must be valid JSON") from None
    if _has_surrogate(value):
        raise BadRequest("request body is not valid text", {"_": "contains invalid unicode"})
    return value


def object_body(allowed: Collection[str], required: Collection[str] = ()) -> dict[str, Any]:
    """The JSON body as an object whose keys are all in `allowed` (for handlers with no service
    validator of their own)."""
    data = json_body()
    if not isinstance(data, dict):
        raise ValidationError({"_": "must be an object"})
    errors = {str(k)[:100]: "unknown field" for k in data if k not in allowed}
    errors.update({k: "required" for k in required if k not in data})
    if errors:
        raise ValidationError(errors)
    return data


# --- query strings -------------------------------------------------------------------------


class Query:
    """Reads and checks a request's query string. Unknown and repeated parameters are errors.

    Collects every problem and raises one `ValidationError` from `done()`, so a client sees all
    of its mistakes at once. Range and meaning checks that the service layer owns (limit 1..500,
    status values, ...) stay there; this only guarantees the value has the right type and fits
    SQLite, so the service never sees something that would blow up in the driver.
    """

    def __init__(self, allowed: Collection[str]):
        self.errors: dict[str, str] = {}
        for key in request.args:
            if key not in allowed:
                self.errors[key[:100]] = "unknown parameter"
            elif len(request.args.getlist(key)) > 1:
                self.errors[key] = "must be given once"

    def text(self, name: str) -> str | None:
        return request.args.get(name)

    def integer(self, name: str, *, lo: int = INT64_MIN, hi: int = INT64_MAX) -> int | None:
        raw = request.args.get(name)
        if raw is None or name in self.errors:
            return None
        if not _INT_RE.fullmatch(raw):
            self.errors[name] = "must be an integer"
            return None
        value = int(raw)
        if not lo <= value <= hi:
            self.errors[name] = "is out of range"
            return None
        return value

    def done(self) -> None:
        if self.errors:
            raise ValidationError(self.errors)


def page_args(query: Query) -> dict[str, int]:
    """`limit` and `offset` as given; the service layer owns the allowed ranges."""
    out = {}
    for name in ("limit", "offset"):
        value = query.integer(name)
        if value is not None:
            out[name] = value
    return out


# --- responses and errors ------------------------------------------------------------------


def error_response(status: int, message: str, fields: Mapping[str, str] | None = None) -> Response:
    resp = jsonify(error=message, fields=dict(fields or {}))
    resp.status_code = status
    return resp


def no_content() -> Response:
    return Response(status=204)


def _http_error(exc: HTTPException) -> Response:
    resp = error_response(exc.code or 500, (exc.name or "error").lower())
    allow = exc.get_headers()
    resp.headers.extend((k, v) for k, v in allow if k == "Allow")
    return resp


def _install_blueprint_errors(blueprint: Blueprint) -> None:
    @blueprint.errorhandler(ValidationError)
    def validation(exc: ValidationError) -> Response:
        return error_response(400, getattr(exc, "message", "validation failed"), exc.errors)

    @blueprint.errorhandler(NotFound)
    def not_found(exc: NotFound) -> Response:
        return error_response(404, str(exc) or "not found")

    @blueprint.errorhandler(HTTPException)
    def http(exc: HTTPException) -> Response:
        return _http_error(exc)

    @blueprint.errorhandler(sqlite3.OperationalError)
    def busy(exc: sqlite3.OperationalError) -> Response:
        log.warning("api: database error: %s", exc)
        resp = error_response(503, "database busy, try again")
        resp.headers["Retry-After"] = "1"
        return resp

    @blueprint.errorhandler(Exception)
    def unexpected(exc: Exception) -> Response:
        log.error("api: unhandled error on %s %s", request.method, request.path, exc_info=exc)
        return error_response(500, "internal error")


def _install_app_errors(app: Flask, prefix: str) -> None:
    """JSON 404/405 for unmatched paths under an API prefix (routing runs before any blueprint).

    One handler serves every prefix; everything outside them keeps Flask's default responses.
    """
    prefixes: list[str] = app.extensions.setdefault(_PREFIXES_KEY, [])
    prefixes.append(prefix)
    if len(prefixes) > 1:
        return

    @app.errorhandler(HTTPException)
    def api_http_errors(exc: HTTPException):
        path = request.path
        if any(path == p or path.startswith(p + "/") for p in prefixes):
            return _http_error(exc)
        return exc


def install_errors(app: Flask, blueprint: Blueprint, prefix: str) -> None:
    _install_blueprint_errors(blueprint)
    _install_app_errors(app, prefix)
