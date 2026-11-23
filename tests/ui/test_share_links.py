"""Share links on the Manage screen and the volunteer page (Task 24).

The live server's database is shared by the session, so the workstream and task made here are
deleted afterwards (a revoked link has nothing left to show). After any navigation, click or save
the checks use Playwright's auto-retrying `expect`, never an immediate read.
"""

from __future__ import annotations

import re

from playwright.sync_api import expect


def api_send(page, base_url, method, path, body=None):
    res = page.request.fetch(
        base_url + path, method=method, data=body, headers={"Origin": base_url}
    )
    assert res.ok, (method, path, res.status, res.text())
    return None if res.status == 204 else res.json()


def test_create_open_and_revoke_share_link(page, base_url, browser):
    ws = api_send(page, base_url, "POST", "/api/workstreams", {"name": "Share crew"})
    task = api_send(
        page,
        base_url,
        "POST",
        "/api/tasks",
        {"workstream_id": ws["id"], "title": "Hang banners", "notes": "private note"},
    )
    volunteer = browser.new_context()  # no session, no cookies: a volunteer's phone
    try:
        page.goto(base_url + "/#/manage")
        section = page.locator("[data-section='share']")
        form = section.locator("[data-form='add-share-link']")
        expect(
            form.locator("select[name='workstream_id'] option", has_text="Share crew")
        ).to_have_count(1)
        form.locator("input[name='label']").fill("Banner team")
        form.locator("select[name='workstream_id']").select_option(label="Share crew")
        form.locator("input[name='days']").fill("5")
        form.get_by_role("button", name="Create link").click()

        url_box = section.locator("[data-share-new] input[data-share-url]")
        expect(url_box).to_have_value(re.compile(re.escape(base_url) + r"/s/[A-Za-z0-9_-]{43,}$"))
        url = url_box.input_value()
        row = section.locator("[data-item='share-link'][data-name='Banner team']")
        expect(row).to_have_attribute("data-state", "active")
        expect(row).to_contain_text("Share crew")

        # The volunteer sees the task, and not its notes.
        shared = volunteer.new_page()
        shared.goto(url)
        item = shared.locator("[data-share-item='task']", has_text="Hang banners")
        expect(item).to_have_count(1)
        expect(item).to_contain_text("To do")
        expect(shared.locator("#share")).to_have_attribute("data-state", "ready")
        expect(shared.locator("body")).not_to_contain_text("private note")

        # Shown once: a fresh visit to Manage no longer has the token.
        page.reload()
        expect(row).to_have_attribute("data-state", "active")
        expect(section.locator("[data-share-new]")).to_have_count(0)

        row.get_by_role("button", name="Revoke").click()
        row.get_by_role("button", name="Confirm revoke").click()
        expect(row).to_have_attribute("data-state", "revoked")
        expect(row.get_by_role("button", name="Revoke")).to_have_count(0)

        shared.goto(url)
        expect(shared.locator("h1")).to_have_text("Link not available")
        expect(shared.locator("[data-share-item]")).to_have_count(0)
    finally:
        volunteer.close()
        page.request.fetch(
            f"{base_url}/api/tasks/{task['id']}", method="DELETE", headers={"Origin": base_url}
        )
        page.request.fetch(
            f"{base_url}/api/workstreams/{ws['id']}", method="DELETE", headers={"Origin": base_url}
        )


def test_share_page_has_no_console_errors(page, base_url, browser):
    link = api_send(page, base_url, "POST", "/api/share-links", {"label": "Console", "days": 1})
    try:
        volunteer = browser.new_context()
        shared = volunteer.new_page()
        errors: list[str] = []
        shared.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        shared.on("pageerror", lambda e: errors.append(str(e)))
        shared.goto(base_url + link["url"])
        expect(shared.locator("#share")).to_have_attribute("data-state", "ready")
        expect(shared.locator("[data-share='tasks']")).to_be_visible()
        assert errors == []
        volunteer.close()
    finally:
        api_send(page, base_url, "POST", f"/api/share-links/{link['id']}/revoke")
