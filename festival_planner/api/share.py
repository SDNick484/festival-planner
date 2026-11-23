"""Volunteer share links (Task 24): organizer management under `/api`, and the public `/s/*` pages.

* `/api/share-links` (web auth only; `register_api` mounts it for `/api` and never for
  `/mcp/api`): list, create (the response carries the raw token, once) and revoke.
* `/s/<token>` serves the static read-only page and `/s/<token>/data` its JSON. Both are
  `@requires("share")`. They are not API routes: they live outside `/api` and `/mcp`.
* `ShareTokenMiddleware` keeps the token out of logs, the same way `PathTokenMiddleware` does for
  `/mcp/<secret>`: it moves the token into `environ["fp.share_token"]` and rewrites PATH_INFO,
  RAW_URI and REQUEST_URI in place to `/s/redacted[/rest]`, which is why the Flask rules below
  are spelled `/s/redacted`. gunicorn writes its access log from that same environ dict; the
  development server's raw request line is redacted by `RedactPathTokenFilter` (mcp/server.py).
* Every `/s/*` response is `Cache-Control: no-store` and `Referrer-Policy: no-referrer`, so the
  token is neither cached nor sent onward in a Referer header.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from flask import Flask, Response, g, jsonify, request

from festival_planner import ui
from festival_planner.api._common import ID, Router, config, object_body
from festival_planner.auth import SHARE_TOKEN_ENVIRON, requires
from festival_planner.db import get_db
from festival_planner.service import share as share_service

SHARE_PREFIX = "/s"
PLACEHOLDER = "redacted"
_PREFIX_SLASH = SHARE_PREFIX + "/"
SHARE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
# What a volunteer's browser shows for an unknown, expired or revoked link (one body for all).
NOT_AVAILABLE_HTML = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<title>Link not available</title></head><body><main><h1>Link not available</h1>"
    "<p>This link may have expired or been turned off. Ask the organizers for a new one.</p>"
    "</main></body></html>"
)


# --- organizer management (/api) -----------------------------------------------------------


def register(router: Router) -> None:
    @router.route("/share-links", "GET")
    def list_share_links():
        return jsonify(share_service.list(get_db()))

    @router.route("/share-links", "POST")
    def create_share_link():
        body = object_body({"label", "workstream_id", "days"}, required=("label", "days"))
        con = get_db()
        link_id, raw = share_service.create(
            con, g.actor, body["label"], body.get("workstream_id"), body["days"]
        )
        resp = jsonify({**share_service.get(con, link_id), "token": raw, "url": f"/s/{raw}"})
        resp.status_code = 201
        resp.headers["Cache-Control"] = "no-store"  # the one response that carries the token
        return resp

    @router.route(f"/share-links/{ID}/revoke", "POST")
    def revoke_share_link(id: int):
        return jsonify(share_service.revoke(get_db(), g.actor, id))


# --- the public pages (/s/*) ---------------------------------------------------------------


class ShareTokenMiddleware:
    """WSGI middleware: `/s/<token>[/rest]` becomes `/s/redacted[/rest]`, with the token moved to
    `environ["fp.share_token"]`. The environ dict is rewritten in place (see the module doc).
    Whether the token is valid is for `@requires("share")` to decide."""

    def __init__(self, wsgi_app: Callable[..., Any]):
        self.wsgi_app = wsgi_app

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> Any:
        path = environ.get("PATH_INFO", "")
        if path.startswith(_PREFIX_SLASH):
            segment, slash, rest = path[len(_PREFIX_SLASH) :].partition("/")
            if segment:
                # PATH_INFO is WSGI's latin-1 view of the raw bytes; the token is UTF-8 text.
                token = segment.encode("latin-1", "replace").decode("utf-8", "replace")
                new_path = f"{_PREFIX_SLASH}{PLACEHOLDER}{slash}{rest}"
                environ[SHARE_TOKEN_ENVIRON] = token
                environ["PATH_INFO"] = new_path
                raw = quote(environ.get("SCRIPT_NAME", "") + new_path, safe="/:@!$&'()*+,;=-._~")
                query = environ.get("QUERY_STRING")
                if query:
                    raw += "?" + query
                for key in ("RAW_URI", "REQUEST_URI"):
                    if key in environ:
                        environ[key] = raw
        return self.wsgi_app(environ, start_response)


def register_share_pages(app: Flask, static_dir: Path = ui.STATIC_DIR) -> None:
    page_html = ui.render_html(static_dir, "share.html", ui.asset_version(static_dir))
    page_rule = f"{_PREFIX_SLASH}{PLACEHOLDER}"

    def share_page():
        return Response(page_html, mimetype="text/html")

    def share_data():
        return jsonify(share_service.shared_view(get_db(), config(), g.share_link))

    app.add_url_rule(page_rule, "share_page", requires("share")(share_page), methods=["GET"])
    app.add_url_rule(
        page_rule + "/data", "share_data", requires("share")(share_data), methods=["GET"]
    )

    @app.after_request
    def share_headers(response: Response) -> Response:
        # Every /s/* answer, including the 404s and 405s, so none of them differ.
        if not request.path.startswith(_PREFIX_SLASH):
            return response
        if request.endpoint == "share_page" and response.status_code == 404:
            # A person opened the link: a readable page instead of the JSON denial.
            response = Response(NOT_AVAILABLE_HTML, status=404, mimetype="text/html")
        response.headers.update(SHARE_HEADERS)
        return response
