"""Serving the browser shell: `/`, the manifest and static caching (no browser needed; the
Playwright tests in tests/ui drive the real page)."""

from __future__ import annotations

import re

from festival_planner import ui


def test_index_uses_content_hashed_asset_urls(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    assert resp.headers["Cache-Control"] == "no-cache"
    html = resp.get_data(as_text=True)
    version = ui.asset_version()
    assert re.fullmatch(r"[0-9a-f]{10}", version)
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    assert refs and all(ref.endswith(f"?v={version}") for ref in refs)
    # No inline script or handler: the CSP allows none.
    assert "<script>" not in html and " onclick" not in html
    assert "unsafe-inline" not in resp.headers["Content-Security-Policy"]


def test_asset_version_tracks_content(tmp_path):
    (tmp_path / "a.js").write_text("one")
    before = ui.asset_version(tmp_path)
    assert ui.asset_version(tmp_path) == before
    (tmp_path / "a.js").write_text("two")
    assert ui.asset_version(tmp_path) != before
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.js").write_text("x")
    assert len(ui.asset_version(tmp_path)) == 10


def test_static_cache_headers(client):
    version = ui.asset_version()
    hashed = client.get(f"/static/app.js?v={version}")
    assert hashed.status_code == 200
    assert "immutable" in hashed.headers["Cache-Control"]
    # Modules imported by app.js carry no version, so they are revalidated.
    assert client.get("/static/dom.js").headers["Cache-Control"] == "no-cache"
    assert client.get("/static/app.js?v=stale").headers["Cache-Control"] == "no-cache"
    assert client.get("/static/nope.js").status_code == 404


def test_manifest_is_public_and_typed(client):
    resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.mimetype == "application/manifest+json"
    assert resp.get_json(force=True)["start_url"] == "/"
