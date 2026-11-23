"""The browser shell: `/` (index.html with content-hashed asset URLs), the manifest, and static
caching. The files themselves live in `festival_planner/static/` and are served by Flask's
standard `/static/` route. All of these routes are on the public allow-list (auth.py); the data
behind them is only reachable through `/api/*`.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from flask import Flask, Response, request, send_from_directory

STATIC_DIR = Path(__file__).resolve().parent / "static"
VERSION_LENGTH = 10
_ASSET_RE = re.compile(r'((?:src|href)="/static/[^"?#]+)"')
IMMUTABLE = "public, max-age=31536000, immutable"


def asset_version(static_dir: Path = STATIC_DIR) -> str:
    """The first 10 hex digits of a SHA-256 over every file under `static_dir` (paths and bytes,
    in sorted order), so any change to any file changes the version."""
    digest = hashlib.sha256()
    for path in sorted(p for p in static_dir.rglob("*") if p.is_file()):
        digest.update(path.relative_to(static_dir).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:VERSION_LENGTH]


def render_index(static_dir: Path, version: str) -> str:
    """index.html with `?v=<version>` on every `/static/` URL it references."""
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    return _ASSET_RE.sub(lambda m: f'{m.group(1)}?v={version}"', html)


def register_ui(app: Flask, static_dir: Path = STATIC_DIR) -> None:
    version = asset_version(static_dir)
    index_html = render_index(static_dir, version)

    @app.get("/")
    def index():
        resp = Response(index_html, mimetype="text/html")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/manifest.webmanifest")
    def manifest():
        resp = send_from_directory(
            static_dir, "manifest.webmanifest", mimetype="application/manifest+json"
        )
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.after_request
    def static_caching(response: Response) -> Response:
        # A URL carrying the content hash never changes meaning, so it can be cached for good.
        # Files the browser reaches without it (modules imported by app.js, icons) are
        # revalidated each time. Debug mode revalidates everything: files change without a restart.
        if request.path.startswith("/static/") and response.status_code == 200:
            hashed = request.args.get("v") == version and not app.debug
            response.headers["Cache-Control"] = IMMUTABLE if hashed else "no-cache"
        return response
