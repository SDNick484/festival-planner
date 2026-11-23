"""JSON REST API: thin adapters over the service layer (design §5).

`register_api(app, prefix, auth)` mounts every handler at `prefix`. The web UI uses
`register_api(app, "/api", "web")` (Access JWT). Task 18 mounts the same handlers for machine
clients at `/mcp/api` with `auth="token"` (GET needs a `read` token, everything else `write`).
`WEB_ONLY_MODULES` (share-link management) are mounted only with `auth="web"`.
"""

from __future__ import annotations

import re
from typing import Literal

from flask import Blueprint, Flask

from festival_planner.api import (
    activity,
    contacts,
    digest,
    event,
    export,
    inbox,
    milestones,
    share,
    tasks,
    workstreams,
)
from festival_planner.api._common import Router, install_errors

MODULES = (event, workstreams, tasks, milestones, contacts, activity, digest, inbox)
# Organizer-only: machine tokens never create or see share links or CSV exports.
WEB_ONLY_MODULES = (share, export)
_PREFIX_RE = re.compile(r"/[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*")


def register_api(
    app: Flask, prefix: str = "/api", auth: Literal["web", "token"] = "web"
) -> Blueprint:
    """Mount the API at `prefix`. Each prefix gets its own blueprint, named after it, so the
    same handlers can be mounted more than once; mounting one prefix twice raises ValueError."""
    if not _PREFIX_RE.fullmatch(prefix):
        raise ValueError(f"prefix must look like /api or /mcp/api, got {prefix!r}")
    if auth not in ("web", "token"):
        raise ValueError(f"auth must be 'web' or 'token', got {auth!r}")
    name = "api" + re.sub(r"\W+", "_", prefix)
    if name in app.blueprints:
        raise ValueError(f"the API is already registered at {prefix}")
    blueprint = Blueprint(name, __name__, url_prefix=prefix)
    install_errors(app, blueprint, prefix)
    router = Router(blueprint, auth)
    for module in MODULES + (WEB_ONLY_MODULES if auth == "web" else ()):
        module.register(router)
    app.register_blueprint(blueprint)
    return blueprint
