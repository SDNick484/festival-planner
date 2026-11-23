"""Offline-friendly PWA caching (Task 26): the shell and the last digest load without a network,
a banner says the data is old, and a write is refused rather than queued.

Service workers need a secure origin and http://127.0.0.1 counts as one. `context.set_offline`
cuts the network for the page and its service worker. Every check after a navigation or a click
uses auto-retrying `expect(...)` or `expect.poll(...)`, never an immediate read and never a sleep.
"""

from __future__ import annotations

import re

from playwright.sync_api import expect

BANNER = "Offline: showing last saved"
WRITE_ERROR = "You're offline; change not saved"

CACHED_PATHS = """async () => {
  const found = [];
  for (const name of await caches.keys()) {
    const cache = await caches.open(name);
    for (const request of await cache.keys()) found.push(new URL(request.url).pathname);
  }
  return found;
}"""

FORGET_API = """async () => {
  for (const name of await caches.keys()) {
    const cache = await caches.open(name);
    for (const request of await cache.keys()) {
      if (new URL(request.url).pathname.startsWith("/api/")) await cache.delete(request);
    }
  }
}"""


CONTROLLED = """new Promise((resolve) => {
  const worker = navigator.serviceWorker;
  if (worker.controller) resolve(true);
  else worker.addEventListener("controllerchange", () => resolve(true), { once: true });
})"""


def wait_for_control(page):
    """Wait (event-driven; page.wait_for_function would eval a string, which the page's CSP
    forbids) until the worker controls the page. The worker only takes control after its install
    step has stored the whole shell, so from here on the shell is in the cache."""
    assert page.evaluate(CONTROLLED) is True


def open_home_online(page, base_url):
    """Visit Home, wait until the service worker controls the page, then reload so the digest is
    fetched through it, and wait until the worker has stored both the shell and the digest."""
    page.goto(base_url + "/")
    wait_for_control(page)
    page.reload()
    expect(page.locator("#view [data-section='workstreams']")).to_have_count(1)
    # The worker stores the digest before it hands the response to the page, so once the page
    # has rendered it the copy is there.
    assert "/api/digest" in page.evaluate(CACHED_PATHS)
    expect(page.get_by_role("status")).to_have_count(0)


def test_shell_loads_offline(page, context, base_url):
    open_home_online(page, base_url)
    paths = page.evaluate(CACHED_PATHS)
    assert {"/", "/static/app.js", "/static/style.css", "/manifest.webmanifest"} <= set(paths)

    context.set_offline(True)
    page.reload()
    expect(page).to_have_title("Home · Festival Planner")
    expect(page.get_by_role("navigation", name="Primary")).to_be_visible()
    expect(page.locator("#view [data-view='home']")).to_have_count(1)

    # The router and the other screens work from the cached shell too.
    page.get_by_role("link", name="Inbox").click()
    expect(page.locator("#view [data-view='inbox']")).to_have_count(1)
    expect(page.locator("#view h1")).to_have_text("Inbox")


def test_offline_banner(page, context, base_url):
    open_home_online(page, base_url)

    context.set_offline(True)
    page.reload()
    # The last digest is shown, marked as old.
    expect(page.locator("#view [data-section='workstreams'] a.ws-row")).not_to_have_count(0)
    expect(page.locator("#view [data-section='header'] .countdown")).to_have_text(
        re.compile(r"^\d+ days$")
    )
    banner = page.get_by_role("status")
    expect(banner).to_have_text(BANNER)
    expect(banner).to_be_visible()

    # Back online, the next load gets fresh data and the banner goes.
    context.set_offline(False)
    page.reload()
    expect(page.locator("#view [data-section='workstreams']")).to_have_count(1)
    expect(page.get_by_role("status")).to_have_count(0)


def test_offline_without_cached_data_shows_error_not_banner(page, context, base_url):
    # A first visit with no network and nothing stored: there is nothing "last saved" to show.
    page.goto(base_url + "/")
    wait_for_control(page)
    assert "/static/app.js" in page.evaluate(CACHED_PATHS)
    page.evaluate(FORGET_API)
    assert "/api/digest" not in page.evaluate(CACHED_PATHS)

    context.set_offline(True)
    page.reload()
    expect(page.locator("#view")).to_contain_text("Could not load the dashboard")
    expect(page.get_by_role("status")).to_have_count(0)


def test_offline_write_shows_error(page, context, base_url):
    open_home_online(page, base_url)
    title = "Offline write must not be saved"
    context.set_offline(True)
    page.reload()
    expect(page.get_by_role("status")).to_have_text(BANNER)

    # The shell's own API client, as every screen's save button uses it.
    result = page.evaluate(
        """async (title) => {
          const api = await import("/static/api.js");
          try {
            await api.post("/api/tasks", {title, workstream_id: 1});
            return null;
          } catch (e) {
            return {name: e.name, status: e.status, message: e.message};
          }
        }""",
        title,
    )
    assert result == {"name": "ApiError", "status": 0, "message": WRITE_ERROR}
    alert = page.get_by_role("alert")
    expect(alert).to_have_text(WRITE_ERROR)

    # Nothing was queued: once back online the server has never seen the task, and no
    # background replay creates it.
    context.set_offline(False)
    page.reload()
    expect(page.locator("#view [data-section='workstreams']")).to_have_count(1)
    expect(page.get_by_role("status")).to_have_count(0)
    tasks = page.evaluate("fetch('/api/tasks?q=Offline+write').then((r) => r.json())")
    assert tasks == []


def test_sensitive_responses_are_never_cached(page, base_url):
    open_home_online(page, base_url)
    # An email list, a single task and an MCP path pass through the worker without being stored.
    page.evaluate(
        """async () => {
          const paths = ["/api/emails", "/api/emails/1", "/api/tasks/1", "/mcp/x", "/api/activity"];
          for (const path of paths) await fetch(path);
        }"""
    )
    paths = page.evaluate(CACHED_PATHS)
    assert [p for p in paths if p.startswith(("/api/emails", "/mcp", "/api/activity"))] == []
    assert "/api/tasks/1" not in paths
    assert "/api/digest" in paths


def test_service_worker_script_is_served_at_root(page, base_url):
    res = page.request.get(base_url + "/sw.js")
    assert res.status == 200
    assert res.headers["content-type"].startswith("text/javascript")
    assert res.headers["cache-control"] == "no-cache"
    script = res.text()
    assert "__ASSET_VERSION__" not in script and "__SHELL_FILES__" not in script
    version = re.search(r'const VERSION = "([0-9a-f]{10})"', script)
    assert version, "the cache version is the asset hash"
    html = page.request.get(base_url + "/").text()
    assert f"?v={version.group(1)}" in html
