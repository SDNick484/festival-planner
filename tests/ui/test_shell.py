"""UI shell smoke tests (Task 9): the page loads under the CSP, the router and tab bar work, and
no static file can inject markup or inline handlers."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "festival_planner" / "static"

TABS = [("Home", "#/"), ("Workstreams", "#/ws"), ("Timeline", "#/timeline"), ("Inbox", "#/inbox")]
# Every route in the table, plus ones that must show "not found" instead of breaking.
ROUTES = ["#/", "#/ws", "#/ws/1", "#/task/1", "#/timeline", "#/inbox", "#/manage", "#/nope"]

# Collect CSP violations and uncaught errors from inside the page, before any script runs.
WATCH = """
window.__problems = [];
document.addEventListener("securitypolicyviolation", (e) =>
  window.__problems.push("csp: " + e.violatedDirective + " " + e.blockedURI));
window.addEventListener("error", (e) => window.__problems.push("error: " + e.message));
window.addEventListener("unhandledrejection", (e) =>
  window.__problems.push("rejection: " + e.reason));
"""


def test_no_console_errors_or_csp_violations(page, base_url):
    console = []
    page.on("console", lambda m: console.append(m.text) if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))
    page.on("requestfailed", lambda r: console.append(f"requestfailed: {r.url}"))
    page.add_init_script(WATCH)

    response = page.goto(base_url + "/")
    csp = response.headers["content-security-policy"]
    assert "unsafe-inline" not in csp and "script-src 'self'" in csp
    for route in ROUTES:
        page.goto(base_url + "/" + route)
        page.wait_for_selector("#view [data-view]")
    page.reload()
    page.wait_for_selector("#view [data-view]")

    assert page.evaluate("window.__problems") == []
    assert console == []


def test_shell_loads_and_tabs_navigate(page, base_url):
    page.goto(base_url + "/")
    assert page.title() == "Home · Festival Planner"
    nav = page.get_by_role("navigation", name="Primary")
    links = nav.get_by_role("link")
    assert links.all_inner_texts() == [name for name, _ in TABS]
    # Home is the start page and is marked as the current tab.
    assert page.locator("#view [data-view='home']").count() == 1
    assert links.nth(0).get_attribute("aria-current") == "page"

    for index, (name, hash_) in enumerate(TABS):
        links.nth(index).click()
        page.wait_for_url(f"**/{hash_}")
        page.wait_for_selector("#view [data-view]")
        assert nav.get_by_role("link", name=name).get_attribute("aria-current") == "page"
        assert page.locator("[aria-current='page']").count() == 1
        assert page.locator("#view h1").inner_text() == name
        assert page.title() == f"{name} · Festival Planner"

    # Deep links and the back button.
    page.goto(base_url + "/#/task/42")
    assert page.locator("#view [data-view='task']").get_attribute("data-id") == "42"
    page.go_back()
    assert page.locator("#view [data-view='inbox']").count() == 1
    page.goto(base_url + "/#/nope")
    assert "Not found" in page.locator("#view h1").inner_text()
    page.goto(base_url + "/#/ws/not-a-number")
    assert "Not found" in page.locator("#view h1").inner_text()


def test_assets_are_content_hashed_and_manifest_is_served(page, base_url):
    html = page.request.get(base_url + "/").text()
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    assert refs
    versions = {ref.partition("?v=")[2] for ref in refs}
    assert len(versions) == 1 and re.fullmatch(r"[0-9a-f]{10}", versions.pop())
    assert page.request.get(base_url + "/static/app.js").status == 200

    manifest = page.request.get(base_url + "/manifest.webmanifest")
    assert manifest.status == 200
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    data = json.loads(manifest.text())
    assert data["display"] == "standalone" and data["start_url"] == "/"
    sizes = {icon["sizes"] for icon in data["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for icon in data["icons"]:
        res = page.request.get(base_url + icon["src"])
        assert res.status == 200 and res.headers["content-type"] == "image/png"
        assert res.body().startswith(b"\x89PNG")


def test_layout_columns_and_color_scheme(page, base_url):
    columns = "getComputedStyle(document.querySelector('.view-home')).gridTemplateColumns"
    page.set_viewport_size({"width": 390, "height": 800})
    page.goto(base_url + "/")
    page.wait_for_selector("[data-view='home']")
    assert len(page.evaluate(columns).split()) == 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.set_viewport_size({"width": 1000, "height": 800})
    assert len(page.evaluate(columns).split()) == 2

    page.emulate_media(color_scheme="light")
    light = page.evaluate("getComputedStyle(document.body).backgroundColor")
    page.emulate_media(color_scheme="dark")
    dark = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert light != dark


def test_dom_helper_is_inert(page, base_url):
    page.goto(base_url + "/")
    result = page.evaluate(
        """async () => {
          const { h } = await import("/static/dom.js");
          const hostile = "<img src=x onerror=window.__pwned=1>";
          const clicks = [];
          const attrs = { class: "a", "data-x": hostile, hidden: false };
          attrs.on = { click: () => clicks.push(1) };
          const el = h("div", attrs, hostile, 7, null, [h("b", null, "x")]);
          document.body.append(el);
          el.click();
          const errors = [];
          const bads = [
            () => h("div", { onclick: "x()" }),
            () => h("script"),
            () => h("div", { style: "color:red" }),
            () => h("div", { on: { click: "not a function" } }),
          ];
          for (const bad of bads) {
            try { bad(); errors.push(false); } catch { errors.push(true); }
          }
          const link = (u) => h("a", { href: u }).hasAttribute("href");
          return {
            html: el.outerHTML,
            kids: el.childNodes.length,
            imgs: el.querySelectorAll("img").length,
            clicks: clicks.length,
            pwned: window.__pwned ?? null,
            errors,
            links: [
              link("javascript:alert(1)"),
              link(" JaVaScript:alert(1)"),
              link("java\tscript:alert(1)"),
              link("data:text/html,x"),
              link("https://example.com/x"),
              link("#/task/1"),
              link("mailto:a@example.com"),
            ],
          };
        }"""
    )
    assert result["imgs"] == 0 and result["pwned"] is None
    assert result["kids"] == 3  # the hostile string, "7" and the <b>: null is skipped
    assert result["clicks"] == 1
    assert result["errors"] == [True, True, True, True]
    assert result["links"] == [False, False, False, False, True, True, True]
    assert "&lt;img" in result["html"]


def test_api_client_roundtrip(page, base_url):
    page.goto(base_url + "/")
    result = page.evaluate(
        """async () => {
          const api = await import("/static/api.js");
          const workstreams = await api.get("/api/workstreams");
          const created = await api.post("/api/tasks", {
            title: "Example task", workstream_id: workstreams[0].id,
          });
          const patched = await api.patch("/api/tasks/" + created.id, {status: "doing"});
          let failure = null;
          try { await api.patch("/api/tasks/" + created.id, {title: "x".repeat(201)}); }
          catch (e) {
            failure = {status: e.status, fields: e.fields, isApi: e instanceof api.ApiError};
          }
          let missing = null;
          try { await api.get("/api/tasks/999999"); } catch (e) { missing = e.status; }
          const gone = await api.del("/api/tasks/" + created.id);
          const isList = Array.isArray(workstreams);
          return {isList, status: patched.status, failure, missing, gone};
        }"""
    )
    assert result["isList"] and result["status"] == "doing"
    assert result["failure"]["status"] == 400 and result["failure"]["isApi"]
    assert "title" in result["failure"]["fields"]
    assert result["missing"] == 404
    assert result["gone"] is None


# --- static files ---------------------------------------------------------------------------

# Markup injection and inline handlers. `find_unsafe` is checked against samples below so a
# broken pattern cannot make the scan pass vacuously.
FORBIDDEN = [
    (re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|srcdoc"), "HTML sink"),
    (re.compile(r"""<[a-zA-Z][^<>]*\son[a-zA-Z]+\s*="""), "inline handler attribute in markup"),
    (re.compile(r"""\.on[a-z]+\s*=(?!=)"""), "event handler property assignment"),
    (re.compile(r"""setAttribute\(\s*["'`]on""", re.IGNORECASE), "setAttribute of a handler"),
    (re.compile(r"javascript:", re.IGNORECASE), "javascript: URL"),
    (re.compile(r"\beval\s*\(|new\s+Function\b"), "dynamic code"),
]


def find_unsafe(name: str, text: str) -> list[str]:
    found = [f"{name}: {why}" for pattern, why in FORBIDDEN if pattern.search(text)]
    if name.endswith(".html") and re.search(r"<script(?![^>]*\ssrc=)", text, re.IGNORECASE):
        found.append(f"{name}: inline <script>")
    return found


@pytest.mark.parametrize(
    "sample",
    [
        "el.innerHTML = x",
        "a.outerHTML",
        "x.insertAdjacentHTML('beforeend', y)",
        '<button onclick="go()">',
        "el.onclick = fn",
        "el.setAttribute('onload', x)",
        '<a href="javascript:void(0)">',
        "eval(code)",
        "<script>run()</script>",
    ],
)
def test_unsafe_scan_catches_samples(sample):
    assert find_unsafe("sample.html", sample)


def test_static_has_no_innerhtml_or_inline_handlers():
    files = [p for p in STATIC.rglob("*") if p.suffix in {".html", ".js", ".css", ".webmanifest"}]
    assert {p.name for p in files} >= {"index.html", "app.js", "api.js", "dom.js", "style.css"}
    problems = []
    for path in files:
        problems += find_unsafe(str(path.relative_to(STATIC)), path.read_text(encoding="utf-8"))
    assert problems == []
