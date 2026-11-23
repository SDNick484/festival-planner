"""Desktop board view on the Workstream screen (Task 23).

Own server and data directory, like the Workstream/Task tests. Data is seeded through the service
layer; after every click, drag, reload or navigation the next check is an auto-retrying
`expect(...)`, never an immediate read.
"""

from __future__ import annotations

import itertools
import threading
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from festival_planner import db
from festival_planner.config import load_config
from festival_planner.service import Actor, tasks, workstreams
from festival_planner.web import create_app

ROOT = Path(__file__).resolve().parents[2]
ORGANIZER_A = "organizer-a@example.com"
ORGANIZER_B = "organizer-b@example.com"
ORGANIZERS = [ORGANIZER_A, ORGANIZER_B]
SYSTEM = Actor("system", "seed")
counter = itertools.count(1)
STATUS_IDS = ["todo", "doing", "waiting", "blocked", "done"]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("board-data")
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


def make_workstream(env):
    with connect(env) as con:
        return workstreams.create(
            con, SYSTEM, {"name": f"Example Board {next(counter)}"}, organizers=ORGANIZERS
        )


def make_task(env, ws, title, **extra):
    with connect(env) as con:
        return tasks.create(
            con, SYSTEM, {"workstream_id": ws["id"], "title": title, **extra}, organizers=ORGANIZERS
        )


def stored_task(env, task_id):
    with connect(env) as con:
        return tasks.get(con, task_id)


def open_workstream(page, env, ws):
    page.goto(f"{env.url}/#/ws/{ws['id']}")
    view = page.locator("#view [data-view='workstream']")
    expect(view.locator("h1")).to_have_text(ws["name"])
    return view


def choose(view, name):
    view.get_by_role("group", name="Task view").get_by_role("button", name=name, exact=True).click()


def column(view, status):
    return view.locator(f".board-column[data-status='{status}']")


def card(view, title):
    return view.locator(".board-card", has_text=title)


def test_board_hidden_on_phone(page, env):
    ws = make_workstream(env)
    make_task(env, ws, "Example phone task")
    # Even a remembered "board" choice must not draw it on a phone-width screen.
    page.add_init_script("try { localStorage.setItem('fp.ws-view', 'board'); } catch (e) {}")
    page.set_viewport_size({"width": 390, "height": 800})
    view = open_workstream(page, env, ws)
    expect(view.locator("[data-role='open'] .task-row")).to_have_count(1)
    expect(view.get_by_role("group", name="Task view")).to_be_hidden()
    expect(view.locator(".board")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    # Growing the window past 900px offers the toggle and the remembered Board.
    page.set_viewport_size({"width": 1200, "height": 800})
    expect(view.get_by_role("group", name="Task view")).to_be_visible()
    expect(view.locator(".board-column")).to_have_count(5)
    page.set_viewport_size({"width": 390, "height": 800})
    expect(view.locator(".board")).to_have_count(0)
    expect(view.locator("[data-role='open'] .task-row")).to_have_count(1)


def test_drag_changes_status(page, env):
    ws = make_workstream(env)
    task = make_task(env, ws, "Example drag task")
    view = open_workstream(page, env, ws)
    # The List is the default; Board offers one column per status, in order.
    expect(view.locator(".board")).to_have_count(0)
    choose(view, "Board")
    columns = view.locator(".board-column")
    expect(columns).to_have_count(5)
    for index, status in enumerate(STATUS_IDS):
        expect(columns.nth(index)).to_have_attribute("data-status", status)
    expect(column(view, "todo").locator(".board-card")).to_have_count(1)

    card(view, "Example drag task").drag_to(column(view, "doing"))
    expect(column(view, "doing").locator(".board-card")).to_have_count(1)
    expect(column(view, "todo").locator(".board-card")).to_have_count(0)
    assert stored_task(env, task["id"])["status"] == "doing"

    # Done is a column too.
    card(view, "Example drag task").drag_to(column(view, "done"))
    expect(column(view, "done").locator(".board-card")).to_have_count(1)
    stored = stored_task(env, task["id"])
    assert stored["status"] == "done" and stored["done_at"]


def test_drag_to_blocked_asks_for_a_reason_inline(page, env):
    ws = make_workstream(env)
    task = make_task(env, ws, "Example blocked task")
    view = open_workstream(page, env, ws)
    choose(view, "Board")
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))

    card(view, "Example blocked task").drag_to(column(view, "blocked"))
    prompt = view.locator(".blocked-prompt")
    expect(prompt).to_be_visible()
    assert stored_task(env, task["id"])["status"] == "todo"
    expect(column(view, "blocked").locator(".board-card")).to_have_count(0)

    # An empty reason is refused with the server's message; nothing changes.
    prompt.get_by_role("button", name="Save").click()
    expect(prompt.locator(".field-error")).to_contain_text("required")
    assert stored_task(env, task["id"])["status"] == "todo"

    prompt.get_by_label("Why is it blocked?").fill("Waiting for the example permit")
    prompt.get_by_role("button", name="Save").click()
    expect(column(view, "blocked").locator(".board-card")).to_have_count(1)
    expect(column(view, "blocked")).to_contain_text("Waiting for the example permit")
    stored = stored_task(env, task["id"])
    assert stored["status"] == "blocked"
    assert stored["blocked_reason"] == "Waiting for the example permit"
    assert dialogs == []


def test_blocked_prompt_can_be_cancelled(page, env):
    ws = make_workstream(env)
    task = make_task(env, ws, "Example cancel task")
    view = open_workstream(page, env, ws)
    choose(view, "Board")
    card(view, "Example cancel task").get_by_label("Status of Example cancel task").select_option(
        "blocked"
    )
    prompt = view.locator(".blocked-prompt")
    expect(prompt).to_be_visible()
    prompt.get_by_role("button", name="Cancel").click()
    expect(prompt).to_have_count(0)
    # The select goes back to the real status.
    expect(
        card(view, "Example cancel task").get_by_label("Status of Example cancel task")
    ).to_have_value("todo")
    assert stored_task(env, task["id"])["status"] == "todo"


def test_status_select_is_the_keyboard_alternative(page, env):
    ws = make_workstream(env)
    task = make_task(env, ws, "Example keyboard task")
    view = open_workstream(page, env, ws)
    choose(view, "Board")
    select = view.get_by_label("Status of Example keyboard task")
    expect(select).to_have_value("todo")
    select.select_option("waiting")
    expect(column(view, "waiting").locator(".board-card")).to_have_count(1)
    assert stored_task(env, task["id"])["status"] == "waiting"
    # Focus stays on the card's select after the board redraws.
    expect(view.get_by_label("Status of Example keyboard task")).to_be_focused()

    view.get_by_label("Status of Example keyboard task").select_option("blocked")
    prompt = view.locator(".blocked-prompt")
    expect(prompt).to_be_visible()
    prompt.get_by_label("Why is it blocked?").fill("Example reason")
    prompt.get_by_label("Why is it blocked?").press("Enter")
    expect(column(view, "blocked").locator(".board-card")).to_have_count(1)
    assert stored_task(env, task["id"])["blocked_reason"] == "Example reason"


def test_toggle_persists(page, env):
    ws = make_workstream(env)
    make_task(env, ws, "Example persist task")
    view = open_workstream(page, env, ws)
    expect(view.locator("[data-role='open'] .task-row")).to_have_count(1)
    expect(view.get_by_role("button", name="List", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    choose(view, "Board")
    expect(view.locator(".board-column")).to_have_count(5)
    expect(view.get_by_role("button", name="Board", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(view.locator("[data-role='open'] .task-row")).to_have_count(0)

    page.reload()
    view = page.locator("#view [data-view='workstream']")
    expect(view.locator(".board-column")).to_have_count(5)
    expect(view.get_by_role("button", name="Board", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )

    # The choice is remembered across workstreams, and List can be chosen again.
    other = make_workstream(env)
    page.goto(f"{env.url}/#/ws/{other['id']}")
    expect(page.locator("#view [data-view='workstream'] h1")).to_have_text(other["name"])
    expect(page.locator("#view .board-column")).to_have_count(5)
    choose(page.locator("#view [data-view='workstream']"), "List")
    expect(page.locator("#view .board")).to_have_count(0)
    page.reload()
    expect(page.locator("#view .task-list, #view [data-role='open'] p").first).to_be_visible()
    expect(page.locator("#view .board")).to_have_count(0)


def test_toggle_works_without_local_storage(page, env):
    ws = make_workstream(env)
    make_task(env, ws, "Example no-storage task")
    page.add_init_script(
        """
        Object.defineProperty(window, 'localStorage', {
          get() { throw new DOMException('blocked', 'SecurityError'); },
        });
        """
    )
    problems = []
    page.on("pageerror", lambda e: problems.append(str(e)))
    view = open_workstream(page, env, ws)
    expect(view.locator("[data-role='open'] .task-row")).to_have_count(1)
    choose(view, "Board")
    expect(view.locator(".board-column")).to_have_count(5)
    assert problems == []
