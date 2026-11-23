"""Workstream and Task screens (Task 11).

These tests run against their own server and data directory, not the shared session server, so the
lists they count are not affected by other UI test modules. Data is seeded through the service
layer, the only write path. After every click, blur or navigation the next check is an
auto-retrying `expect(...)`: a plain read could be satisfied by the screen from before the change.
"""

from __future__ import annotations

import itertools
import threading
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from festival_planner import db
from festival_planner.config import load_config
from festival_planner.service import Actor, contacts, event, milestones, tasks, workstreams
from festival_planner.web import create_app

ROOT = Path(__file__).resolve().parents[2]
ORGANIZER_A = "organizer-a@example.com"
ORGANIZER_B = "organizer-b@example.com"
ORGANIZERS = [ORGANIZER_A, ORGANIZER_B]
SYSTEM = Actor("system", "seed")
counter = itertools.count(1)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("ws-task-data")
    example = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    assert 'data_dir = "data"' in example
    config_path = data_dir / "config.toml"
    config_path.write_text(
        example.replace('data_dir = "data"', f'data_dir = "{data_dir.as_posix()}"'),
        encoding="utf-8",
    )
    cfg = load_config(config_path, env={"FLASK_ENV": "development", "DEV_AUTH_EMAIL": ORGANIZER_A})
    app = create_app(cfg)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{server.server_port}", db_path=data_dir / db.DB_FILENAME
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)


def connect(env):
    return closing(db.connect(env.db_path))


def make_workstream(env, **extra):
    name = extra.pop("name", f"Example Space {next(counter)}")
    with connect(env) as con:
        return workstreams.create(con, SYSTEM, {"name": name, **extra}, organizers=ORGANIZERS)


def make_task(env, ws, title=None, *, actor=SYSTEM, **extra):
    title = title or f"Example task {next(counter)}"
    with connect(env) as con:
        return tasks.create(
            con, actor, {"workstream_id": ws["id"], "title": title, **extra}, organizers=ORGANIZERS
        )


def stored_task(env, task_id):
    with connect(env) as con:
        return tasks.get(con, task_id)


def open_task(page, env, task):
    page.goto(f"{env.url}/#/task/{task['id']}")
    expect(page.locator("#view [data-view='task'] h1")).to_have_text(task["title"])


def chip(page, name):
    return page.get_by_role("group", name="Status").get_by_role("button", name=name, exact=True)


# --- workstream screens ----------------------------------------------------------------------


def test_workstream_list_shows_owner_and_counts(page, env):
    ws = make_workstream(env, name="Example Rides List", owner=ORGANIZER_B)
    make_task(env, ws, status="done")
    make_task(env, ws, status="doing")
    make_task(env, ws)
    page.goto(f"{env.url}/#/ws")
    expect(page.locator("#view h1")).to_have_text("Workstreams")
    row = page.locator(".ws-card", has_text="Example Rides List")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("Organizer B")
    expect(row).to_contain_text("1 of 3 done")
    row.get_by_role("link").click()
    expect(page.locator("#view [data-view='workstream'] h1")).to_have_text("Example Rides List")


def test_workstream_sorts_open_tasks_by_effective_due_and_collapses_done(page, env):
    with connect(env) as con:
        stone = next(m for m in milestones.list(con) if m["name"] == "T-30")
        derived = date.fromisoformat(event.get(con)["event_date"]) - timedelta(
            days=stone["days_before_event"]
        )
    ws = make_workstream(env, owner=ORGANIZER_A)
    later = make_task(env, ws, "Later task", due_date=(derived + timedelta(days=5)).isoformat())
    undated = make_task(env, ws, "Undated task")
    via_milestone = make_task(env, ws, "Milestone task", milestone_id=stone["id"])
    sooner = make_task(env, ws, "Sooner task", due_date=(derived - timedelta(days=5)).isoformat())
    make_task(env, ws, "Finished task", status="done")
    with connect(env) as con:
        contacts.create(
            con,
            SYSTEM,
            {
                "name": "Example Print Shop",
                "workstream_id": ws["id"],
                "emails": ["proofs@example.net", "sales@example.net"],
            },
        )

    page.goto(f"{env.url}/#/ws/{ws['id']}")
    view = page.locator("#view [data-view='workstream']")
    expect(view.locator("h1")).to_have_text(ws["name"])
    expect(view).to_contain_text("Organizer A")
    expect(view).to_contain_text("1 of 5 done")
    rows = view.locator("[data-role='open'] .task-row")
    expect(rows).to_have_count(4)
    ids = [str(t["id"]) for t in (sooner, via_milestone, later, undated)]
    for index, task_id in enumerate(ids):
        expect(rows.nth(index)).to_have_attribute("data-id", task_id)
    # The milestone-only task is dated by its milestone, through the API's effective_due.
    expect(rows.nth(1)).to_have_attribute("data-due", derived.isoformat())
    expect(rows.nth(3)).to_have_attribute("data-due", "")

    done = view.locator("details[data-role='done']")
    expect(done).not_to_have_attribute("open", "")
    expect(done.locator("summary")).to_have_text("Done (1)")
    expect(done.get_by_role("link", name="Finished task")).to_be_hidden()
    done.locator("summary").click()
    expect(done.get_by_role("link", name="Finished task")).to_be_visible()

    contact = view.locator("[data-role='contacts'] li")
    expect(contact).to_have_count(1)
    expect(contact).to_contain_text("Example Print Shop")
    expect(contact).to_contain_text("2 emails")


def test_add_task_title_only(page, env):
    ws = make_workstream(env)
    page.goto(f"{env.url}/#/ws/{ws['id']}")
    view = page.locator("#view [data-view='workstream']")
    expect(view.locator("h1")).to_have_text(ws["name"])
    expect(view.locator("[data-role='open'] .task-row")).to_have_count(0)

    title = view.get_by_label("New task title")
    title.fill("Book the example tent")
    view.get_by_role("button", name="+ Add task").click()

    row = view.locator("[data-role='open'] .task-row", has_text="Book the example tent")
    expect(row).to_have_count(1)
    expect(title).to_have_value("")
    with connect(env) as con:
        created = tasks.list(con, workstream_id=ws["id"])
    assert len(created) == 1
    task = created[0]
    assert task["title"] == "Book the example tent" and task["status"] == "todo"
    assert task["due_date"] is None and task["owner"] is None

    row.get_by_role("link").click()
    expect(page.locator("#view [data-view='task'] h1")).to_have_text("Book the example tent")


def test_add_task_error_keeps_input(page, env):
    ws = make_workstream(env)
    page.goto(f"{env.url}/#/ws/{ws['id']}")
    view = page.locator("#view [data-view='workstream']")
    title = view.get_by_label("New task title")
    long_title = "x" * 201
    title.fill(long_title)
    view.get_by_role("button", name="+ Add task").click()
    expect(view.locator(".add-task .field-error")).to_contain_text("at most 200 characters")
    expect(title).to_have_value(long_title)
    with connect(env) as con:
        assert tasks.list(con, workstream_id=ws["id"]) == []


def test_unknown_workstream_and_task_show_not_found(page, env):
    page.goto(f"{env.url}/#/ws/999999")
    expect(page.locator("#view [data-view='workstream'] h1")).to_have_text("Workstream not found")
    page.goto(f"{env.url}/#/task/999999")
    expect(page.locator("#view [data-view='task'] h1")).to_have_text("Task not found")


# --- task screen -----------------------------------------------------------------------------


def test_status_chip_one_tap(page, env):
    ws = make_workstream(env)
    task = make_task(env, ws)
    open_task(page, env, task)
    expect(chip(page, "To do")).to_have_attribute("aria-pressed", "true")
    expect(chip(page, "Doing")).to_have_attribute("aria-pressed", "false")

    chip(page, "Doing").click()
    expect(chip(page, "Doing")).to_have_attribute("aria-pressed", "true")
    expect(chip(page, "To do")).to_have_attribute("aria-pressed", "false")
    assert stored_task(env, task["id"])["status"] == "doing"

    chip(page, "Done").click()
    expect(chip(page, "Done")).to_have_attribute("aria-pressed", "true")
    stored = stored_task(env, task["id"])
    assert stored["status"] == "done" and stored["done_at"]
    # The change is on the page's own activity list.
    expect(page.get_by_role("list", name="Activity").locator("li").first).to_contain_text(
        "status: doing → done"
    )


def test_blocked_requires_reason_inline(page, env):
    ws = make_workstream(env)
    task = make_task(env, ws)
    open_task(page, env, task)
    reason_field = page.get_by_label("Blocked reason")
    expect(reason_field).to_be_hidden()

    chip(page, "Blocked").click()
    prompt = page.locator(".blocked-prompt")
    expect(prompt).to_be_visible()
    expect(chip(page, "Blocked")).to_have_attribute("aria-pressed", "false")
    assert stored_task(env, task["id"])["status"] == "todo"

    # An empty reason is refused: the server's message shows inline and nothing changes.
    prompt.get_by_role("button", name="Save").click()
    expect(prompt.locator(".field-error")).to_contain_text("required")
    expect(chip(page, "Blocked")).to_have_attribute("aria-pressed", "false")
    assert stored_task(env, task["id"])["status"] == "todo"

    prompt.get_by_label("Why is it blocked?").fill("Waiting for the example permit")
    prompt.get_by_role("button", name="Save").click()
    expect(chip(page, "Blocked")).to_have_attribute("aria-pressed", "true")
    expect(prompt).to_be_hidden()
    expect(reason_field).to_have_value("Waiting for the example permit")
    stored = stored_task(env, task["id"])
    assert stored["status"] == "blocked"
    assert stored["blocked_reason"] == "Waiting for the example permit"

    # Leaving Blocked clears the reason and hides its field.
    chip(page, "Doing").click()
    expect(chip(page, "Doing")).to_have_attribute("aria-pressed", "true")
    expect(reason_field).to_be_hidden()
    assert stored_task(env, task["id"])["blocked_reason"] is None


def test_blocked_prompt_can_be_cancelled(page, env):
    ws = make_workstream(env)
    task = make_task(env, ws)
    open_task(page, env, task)
    chip(page, "Blocked").click()
    prompt = page.locator(".blocked-prompt")
    expect(prompt).to_be_visible()
    prompt.get_by_role("button", name="Cancel").click()
    expect(prompt).to_be_hidden()
    assert stored_task(env, task["id"])["status"] == "todo"


def test_task_fields_save_on_blur(page, env):
    ws = make_workstream(env)
    task = make_task(env, ws, "Example field task")
    open_task(page, env, task)
    activity = page.get_by_role("list", name="Activity").locator("li")
    due = (date.today() + timedelta(days=30)).isoformat()

    page.get_by_label("Owner").select_option(ORGANIZER_B)
    expect(activity.first).to_contain_text("owner")
    assert stored_task(env, task["id"])["owner"] == ORGANIZER_B

    page.get_by_label("Due date").fill(due)
    expect(activity.first).to_contain_text(f"due_date: none → {due}")
    assert stored_task(env, task["id"])["due_date"] == due

    page.get_by_label("Waiting on").fill("Example Tent Co.")
    page.get_by_label("Waiting on").blur()
    expect(activity.first).to_contain_text("waiting_on")
    assert stored_task(env, task["id"])["waiting_on"] == "Example Tent Co."

    page.get_by_label("Notes").fill("Plain <b>text</b> only\nsecond line")
    page.get_by_label("Notes").blur()
    expect(activity.first).to_contain_text("notes")
    assert stored_task(env, task["id"])["notes"] == "Plain <b>text</b> only\nsecond line"

    # Links: add one (it is a safe, clickable link), then remove it.
    page.get_by_label("Link label").fill("Example page")
    page.get_by_label("Link URL").fill("https://example.com/tent")
    page.get_by_role("button", name="Add link").click()
    link = page.get_by_role("link", name="Example page")
    expect(link).to_have_attribute("href", "https://example.com/tent")
    assert stored_task(env, task["id"])["links"] == [
        {"label": "Example page", "url": "https://example.com/tent"}
    ]
    page.get_by_role("button", name="Remove link Example page").click()
    expect(link).to_have_count(0)
    assert stored_task(env, task["id"])["links"] == []

    # A reload shows what was saved.
    page.reload()
    expect(page.get_by_label("Owner")).to_have_value(ORGANIZER_B)
    expect(page.get_by_label("Due date")).to_have_value(due)
    expect(page.get_by_label("Waiting on")).to_have_value("Example Tent Co.")


def test_bad_link_shows_error_and_keeps_input(page, env):
    ws = make_workstream(env)
    task = make_task(env, ws)
    open_task(page, env, task)
    page.get_by_label("Link URL").fill("example.com/no-scheme")
    page.get_by_role("button", name="Add link").click()
    expect(page.locator(".field-links .field-error")).to_contain_text("http")
    expect(page.get_by_label("Link URL")).to_have_value("example.com/no-scheme")
    assert stored_task(env, task["id"])["links"] == []


def test_activity_shows_source(page, env):
    ws = make_workstream(env)
    task = make_task(env, ws, "Example activity task", actor=Actor("email", ORGANIZER_B))
    with connect(env) as con:
        tasks.update(
            con,
            Actor("example-duet", "example-duet"),
            task["id"],
            {"status": "doing"},
            organizers=ORGANIZERS,
        )
    open_task(page, env, task)
    items = page.get_by_role("list", name="Activity").locator("li")
    expect(items).to_have_count(2)
    # Newest first: date · source · actor · summary of the change.
    expect(items.nth(0)).to_contain_text("example-duet")
    expect(items.nth(0)).to_contain_text("status: todo → doing")
    expect(items.nth(1)).to_contain_text("email")
    expect(items.nth(1)).to_contain_text(ORGANIZER_B)
    expect(items.nth(1)).to_contain_text("created")
    assert items.nth(0).locator("time").get_attribute("datetime")

    # A change made here is attributed to the web and the signed-in organizer.
    chip(page, "Waiting").click()
    expect(items).to_have_count(3)
    expect(items.nth(0)).to_contain_text("web")
    expect(items.nth(0)).to_contain_text(ORGANIZER_A)
    expect(items.nth(0)).to_contain_text("status: doing → waiting")


def test_failed_save_keeps_input(page, env):
    ws = make_workstream(env)
    task = make_task(env, ws, "Example kept title")
    open_task(page, env, task)
    title = page.get_by_label("Title", exact=True)
    long_title = "y" * 201
    title.fill(long_title)
    title.blur()
    expect(page.locator(".field-title .field-error")).to_contain_text("at most 200 characters")
    expect(title).to_have_value(long_title)
    assert stored_task(env, task["id"])["title"] == "Example kept title"

    # Fixing the value saves it and clears the message.
    title.fill("Example renamed title")
    title.blur()
    expect(page.locator("#view h1")).to_have_text("Example renamed title")
    expect(page.locator(".field-title .field-error")).to_have_text("")
    assert stored_task(env, task["id"])["title"] == "Example renamed title"


def test_task_page_is_inert_and_has_hidden_email_slot(page, env):
    ws = make_workstream(env)
    hostile = "<img src=x onerror=window.__pwned=1>"
    task = make_task(env, ws, hostile, notes=hostile, blocked_reason=None)
    page.goto(f"{env.url}/#/task/{task['id']}")
    expect(page.locator("#view [data-view='task'] h1")).to_have_text(hostile)
    expect(page.get_by_label("Notes")).to_have_value(hostile)
    assert page.locator("#view img").count() == 0
    assert page.evaluate("window.__pwned ?? null") is None
    # Task 16 fills this section in; until then it is not shown.
    slot = page.locator("[data-section='linked-emails']")
    expect(slot).to_have_count(1)
    expect(slot).to_be_hidden()


def test_no_horizontal_scroll_with_long_title(page, env):
    ws = make_workstream(env)
    task = make_task(env, ws, "🎪" * 100)
    page.set_viewport_size({"width": 390, "height": 800})
    page.goto(f"{env.url}/#/ws/{ws['id']}")
    expect(page.locator("#view .task-row")).to_have_count(1)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    open_task(page, env, task)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
