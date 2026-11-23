"""Timeline and Manage screens (Task 12).

The live server and its database are shared by the whole session, so every test that changes data
puts it back (the `restore` fixture). After any navigation, click or save the checks use
Playwright's auto-retrying `expect`, never a read the previous state could satisfy.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import pytest
from playwright.sync_api import expect


def api_get(page, base_url, path):
    res = page.request.get(base_url + path)
    assert res.ok, (path, res.status, res.text())
    return res.json()


def api_send(page, base_url, method, path, body=None):
    res = page.request.fetch(
        base_url + path, method=method, data=body, headers={"Origin": base_url}
    )
    assert res.ok, (method, path, res.status, res.text())
    return None if res.status == 204 else res.json()


@pytest.fixture
def restore(page, base_url):
    """Collects what a test created and puts the event back as it was."""
    original = api_get(page, base_url, "/api/event")
    created: list[str] = []
    yield created
    # Tasks first: a workstream or milestone with live tasks cannot be deleted.
    for path in sorted(created, key=lambda p: not p.startswith("/api/tasks")):
        page.request.fetch(base_url + path, method="DELETE", headers={"Origin": base_url})
    keep = {k: original[k] for k in ("name", "event_date", "timezone")}
    api_send(page, base_url, "PUT", "/api/event", keep)


def make(page, base_url, restore, path, body):
    row = api_send(page, base_url, "POST", path, body)
    restore.append(f"{path}/{row['id']}")
    return row


def utc_today() -> date:
    # The test config's event timezone is UTC, so the app's "today" is the UTC date.
    return datetime.now(UTC).date()


def milestone(page, days):
    return page.locator(f"[data-view='timeline'] [data-tl='milestone'][data-days='{days}']")


# --- timeline -------------------------------------------------------------------------------


def test_timeline_today_marker_position(page, base_url, restore):
    today = utc_today()
    # T-21 is yesterday, T-14 is six days away: "Today" must sit between them.
    api_send(
        page,
        base_url,
        "PUT",
        "/api/event",
        {"event_date": (today + timedelta(days=20)).isoformat()},
    )
    page.goto(base_url + "/#/timeline")
    marker = page.locator("[data-view='timeline'] [data-tl='today']")
    expect(marker).to_have_count(1)
    expect(marker).to_have_attribute("data-date", today.isoformat())
    expect(marker).to_contain_text("Today")
    expect(marker.locator("xpath=preceding-sibling::*[1]")).to_have_attribute("data-days", "21")
    expect(marker.locator("xpath=following-sibling::*[1]")).to_have_attribute("data-days", "14")

    # Past milestones are all above the marker and future ones all below it, in date order.
    order = page.locator("[data-view='timeline'] [data-tl]").evaluate_all(
        "els => els.map(e => e.dataset.tl === 'milestone' ? Number(e.dataset.days) : e.dataset.tl)"
    )
    assert order == [60, 45, 30, 21, "today", 14, 7, 1, "event"]

    # Moving the event so that every milestone is in the future puts the marker first.
    api_send(
        page,
        base_url,
        "PUT",
        "/api/event",
        {"event_date": (today + timedelta(days=90)).isoformat()},
    )
    page.reload()
    expect(marker.locator("xpath=following-sibling::*[1]")).to_have_attribute("data-days", "60")
    expect(page.locator("[data-view='timeline'] [data-tl]").first).to_have_attribute(
        "data-tl", "today"
    )


def test_timeline_shows_tasks_and_done_state(page, base_url, restore):
    ws = make(page, base_url, restore, "/api/workstreams", {"name": "Example Timeline Stream"})
    done = make(
        page,
        base_url,
        restore,
        "/api/milestones",
        {"name": "Example all done", "days_before_event": 3},
    )
    mixed = make(
        page,
        base_url,
        restore,
        "/api/milestones",
        {"name": "Example mixed", "days_before_event": 4},
    )
    make(
        page,
        base_url,
        restore,
        "/api/milestones",
        {"name": "Example empty", "days_before_event": 5},
    )
    for title, status, m in (
        ("Example finished task", "done", done),
        ("Example second finished task", "done", done),
        ("Example finished among others", "done", mixed),
        ("Example open task", "todo", mixed),
    ):
        make(
            page,
            base_url,
            restore,
            "/api/tasks",
            {"title": title, "status": status, "workstream_id": ws["id"], "milestone_id": m["id"]},
        )
    page.goto(base_url + "/#/timeline")
    all_done, not_done, empty = milestone(page, 3), milestone(page, 4), milestone(page, 5)
    expect(all_done).to_have_class("tl-item tl-done")
    expect(all_done).to_contain_text("T-3 · Example all done")
    expect(all_done).to_contain_text("2 tasks")
    expect(not_done).not_to_have_class("tl-item tl-done")
    expect(not_done).to_contain_text("2 tasks")
    expect(empty).not_to_have_class("tl-item tl-done")
    expect(empty).to_contain_text("No tasks")

    # The attached tasks are hidden until the milestone is expanded.
    open_task = not_done.get_by_role("link", name="Example open task")
    expect(open_task).to_be_hidden()
    not_done.get_by_text("Example mixed").click()
    expect(open_task).to_be_visible()
    expect(open_task).to_have_attribute(
        "href", f"#/task/{_task_id(page, base_url, 'Example open task')}"
    )


def _task_id(page, base_url, title):
    return next(
        t["id"] for t in api_get(page, base_url, "/api/tasks?limit=500") if t["title"] == title
    )


def test_change_event_date_moves_timeline(page, base_url, restore):
    page.goto(base_url + "/#/timeline")
    expect(milestone(page, 14)).to_have_attribute("data-date", "2099-09-26")

    page.get_by_role("link", name="Home").click()
    page.goto(base_url + "/#/manage")
    section = page.locator("[data-section='event']")
    date_input = section.get_by_label("Date")
    expect(date_input).to_have_value("2099-10-10")
    date_input.fill("2099-11-10")
    section.get_by_role("button", name="Save event").click()
    expect(section.get_by_role("status")).to_have_text("Saved")
    assert api_get(page, base_url, "/api/event")["event_date"] == "2099-11-10"

    page.get_by_role("link", name="Timeline").click()
    expect(page.locator("[data-view='timeline']")).to_have_count(1)
    expect(milestone(page, 14)).to_have_attribute("data-date", "2099-10-27")
    expect(milestone(page, 1)).to_have_attribute("data-date", "2099-11-09")
    expect(page.locator("[data-tl='event']")).to_have_attribute("data-date", "2099-11-10")


# --- manage ---------------------------------------------------------------------------------


def test_event_save_error_keeps_input(page, base_url, restore):
    page.goto(base_url + "/#/manage")
    section = page.locator("[data-section='event']")
    section.get_by_label("Time zone").fill("Not/AZone")
    section.get_by_role("button", name="Save event").click()
    expect(section.locator("[data-field='timezone'] .mg-error")).to_contain_text("IANA time zone")
    expect(section.get_by_label("Time zone")).to_have_value("Not/AZone")
    assert api_get(page, base_url, "/api/event")["timezone"] == "UTC"


def test_manage_add_milestone(page, base_url, restore):
    page.goto(base_url + "/#/manage")
    section = page.locator("[data-section='milestones']")
    add = section.locator("form[data-form='add-milestone']")
    add.get_by_label("Name").fill("Example setup day")
    add.get_by_label("Days before").fill("2")
    add.get_by_role("button", name="Add milestone").click()

    row = section.locator("[data-item='milestone'][data-name='Example setup day']")
    expect(row).to_have_count(1)
    expect(row.get_by_label("Days before")).to_have_value("2")
    expect(add.get_by_label("Name")).to_have_value("")
    created = next(
        m for m in api_get(page, base_url, "/api/milestones") if m["name"] == "Example setup day"
    )
    restore.append(f"/api/milestones/{created['id']}")
    assert created["days_before_event"] == 2

    # It is derived from the event date on the timeline, too.
    page.get_by_role("link", name="Timeline").click()
    expect(milestone(page, 2)).to_have_attribute("data-date", "2099-10-08")

    # A bad value is reported next to the field and the typed input stays.
    page.go_back()
    add = page.locator("[data-section='milestones'] form[data-form='add-milestone']")
    add.get_by_label("Name").fill("Example too early")
    add.get_by_label("Days before").fill("99999")
    add.get_by_role("button", name="Add milestone").click()
    expect(add.locator("[data-field='days_before_event'] .mg-error")).to_be_visible()
    expect(add.get_by_label("Name")).to_have_value("Example too early")


def test_manage_edit_milestone(page, base_url, restore):
    made = make(
        page,
        base_url,
        restore,
        "/api/milestones",
        {"name": "Example old name", "days_before_event": 9},
    )
    page.goto(base_url + "/#/manage")
    row = page.locator(f"[data-item='milestone'][data-id='{made['id']}']")
    row.get_by_label("Name").fill("Example new name")
    row.get_by_label("Days before").fill("8")
    row.get_by_role("button", name="Save").click()
    expect(row).to_have_attribute("data-name", "Example new name")
    expect(row.get_by_role("status")).to_have_text("Saved")
    updated = api_get(page, base_url, f"/api/milestones/{made['id']}")
    assert (updated["name"], updated["days_before_event"]) == ("Example new name", 8)


def test_manage_workstreams_add_rename_reorder(page, base_url, restore):
    page.goto(base_url + "/#/manage")
    section = page.locator("[data-section='workstreams']")
    add = section.locator("form[data-form='add-workstream']")
    add.get_by_label("Name").fill("Example Last Stream")
    add.get_by_role("button", name="Add workstream").click()
    last = section.locator("[data-item='workstream'][data-name='Example Last Stream']")
    expect(last).to_have_count(1)
    created = next(
        w for w in api_get(page, base_url, "/api/workstreams") if w["name"] == "Example Last Stream"
    )
    restore.append(f"/api/workstreams/{created['id']}")

    def names():
        return [w["name"] for w in api_get(page, base_url, "/api/workstreams")]

    before = names()
    assert before[-1] == "Example Last Stream"
    # New streams go to the end; the last one cannot move down.
    expect(last.get_by_role("button", name=re.compile(r"^Move .* down$"))).to_be_disabled()
    last.get_by_role("button", name=re.compile(r"^Move .* up$")).click()
    items = section.locator("[data-item='workstream']")
    expect(items.nth(len(before) - 2)).to_have_attribute("data-name", "Example Last Stream")
    assert names() == [*before[:-2], "Example Last Stream", before[-2]]

    # Rename and archive.
    last = section.locator(f"[data-item='workstream'][data-id='{created['id']}']")
    last.get_by_label("Name", exact=True).fill("Example Renamed Stream")
    last.get_by_label("Archived").check()
    last.get_by_role("button", name="Save").click()
    expect(last).to_have_attribute("data-name", "Example Renamed Stream")
    row = api_get(page, base_url, f"/api/workstreams/{created['id']}")
    assert row["name"] == "Example Renamed Stream" and row["archived"] is True


def test_delete_requires_inline_confirm(page, base_url, restore):
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    made = make(page, base_url, restore, "/api/workstreams", {"name": "Example Temp Stream"})
    page.goto(base_url + "/#/manage")
    row = page.locator(
        f"[data-section='workstreams'] [data-item='workstream'][data-id='{made['id']}']"
    )
    expect(row).to_have_count(1)

    row.get_by_role("button", name="Delete", exact=True).click()
    prompt = row.locator("[data-confirm]")
    expect(prompt).to_be_visible()
    expect(prompt).to_contain_text("Example Temp Stream")
    assert (
        api_get(page, base_url, f"/api/workstreams/{made['id']}")["name"] == "Example Temp Stream"
    )

    # Cancel backs out and nothing is deleted.
    prompt.get_by_role("button", name="Cancel").click()
    expect(prompt).to_have_count(0)
    expect(row.get_by_role("button", name="Delete", exact=True)).to_be_visible()
    assert (
        api_get(page, base_url, f"/api/workstreams/{made['id']}")["name"] == "Example Temp Stream"
    )

    row.get_by_role("button", name="Delete", exact=True).click()
    row.get_by_role("button", name="Confirm delete").click()
    expect(row).to_have_count(0)
    assert made["id"] not in [w["id"] for w in api_get(page, base_url, "/api/workstreams")]
    assert dialogs == []  # never window.confirm


def test_delete_refused_shows_inline_error(page, base_url, restore):
    ws = make(page, base_url, restore, "/api/workstreams", {"name": "Example Busy Stream"})
    stone = make(
        page,
        base_url,
        restore,
        "/api/milestones",
        {"name": "Example Busy Milestone", "days_before_event": 6},
    )
    make(
        page,
        base_url,
        restore,
        "/api/tasks",
        {"title": "Example live task", "workstream_id": ws["id"], "milestone_id": stone["id"]},
    )
    page.goto(base_url + "/#/manage")
    for kind, made in (("workstream", ws), ("milestone", stone)):
        row = page.locator(f"[data-item='{kind}'][data-id='{made['id']}']")
        row.get_by_role("button", name="Delete", exact=True).click()
        row.get_by_role("button", name="Confirm delete").click()
        expect(row.get_by_role("status")).to_contain_text("task")
        expect(row).to_have_count(1)
        expect(row.get_by_role("button", name="Delete", exact=True)).to_be_visible()


def test_manage_contacts_crud(page, base_url, restore):
    ws = make(page, base_url, restore, "/api/workstreams", {"name": "Example Contact Stream"})
    page.goto(base_url + "/#/manage")
    section = page.locator("[data-section='contacts']")
    add = section.locator("form[data-form='add-contact']")
    section.locator("details.mg-add-details > summary").click()
    add.get_by_label("Name").fill("Example Caterer")
    add.get_by_label("Organization").fill("Example Catering Co.")
    add.get_by_label("Role").fill("Food")
    add.get_by_label("Emails").fill("caterer@example.com, orders@example.org")
    add.get_by_label("Phone").fill("555-0100")
    add.get_by_label("Workstream").select_option(label="Example Contact Stream")
    add.get_by_label("Notes").fill("Fictional contact.")
    add.get_by_role("button", name="Add contact").click()

    row = section.locator("[data-item='contact'][data-name='Example Caterer']")
    expect(row).to_have_count(1)
    created = next(
        c for c in api_get(page, base_url, "/api/contacts") if c["name"] == "Example Caterer"
    )
    restore.append(f"/api/contacts/{created['id']}")
    assert created["emails"] == ["caterer@example.com", "orders@example.org"]
    assert (
        created["workstream_id"] == ws["id"] and created["organization"] == "Example Catering Co."
    )
    assert created["phone"] == "555-0100" and created["role"] == "Food"

    # Edit: the item opens from its summary; a bad email shows on the field and keeps the input.
    row.locator("summary").click()
    row.get_by_label("Emails").fill("not-an-email")
    row.get_by_role("button", name="Save").click()
    expect(row.locator("[data-field='emails'] .mg-error")).to_be_visible()
    expect(row.get_by_label("Emails")).to_have_value("not-an-email")
    row.get_by_label("Emails").fill("caterer@example.com")
    row.get_by_label("Phone").fill("")
    row.get_by_label("Workstream").select_option(label="No workstream")
    row.get_by_role("button", name="Save").click()
    expect(row.get_by_role("status")).to_have_text("Saved")
    updated = api_get(page, base_url, f"/api/contacts/{created['id']}")
    assert updated["emails"] == ["caterer@example.com"]
    assert updated["phone"] is None and updated["workstream_id"] is None

    row.get_by_role("button", name="Delete", exact=True).click()
    row.get_by_role("button", name="Confirm delete").click()
    expect(row).to_have_count(0)
