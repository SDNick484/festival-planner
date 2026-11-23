"""Inbox screen and linked emails on the Task screen (Task 16).

Own server and data directory (see test_workstream_task.py). Emails and tasks are seeded through
the service layer, the only write path. After every click or navigation the next check is an
auto-retrying `expect(...)`, never a plain read.
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
from festival_planner.mail.parse import ParsedEmail
from festival_planner.service import Actor, activity, contacts, emails, tasks, workstreams
from festival_planner.web import create_app

ROOT = Path(__file__).resolve().parents[2]
ORGANIZER_A = "organizer-a@example.com"
ORGANIZER_B = "organizer-b@example.com"
ORGANIZERS = [ORGANIZER_A, ORGANIZER_B]
SYSTEM = Actor("system", "seed")
counter = itertools.count(1)
HOSTILE = "<img src=x onerror=window.__pwned=1>"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("inbox-data")
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
            url=f"http://127.0.0.1:{server.server_port}",
            db_path=data_dir / db.DB_FILENAME,
            cfg=cfg,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)


def connect(env):
    return closing(db.connect(env.db_path))


def make_workstream(env, name=None):
    name = name or f"Example Space {next(counter)}"
    with connect(env) as con:
        return workstreams.create(con, SYSTEM, {"name": name}, organizers=ORGANIZERS)


def make_task(env, ws, title=None):
    title = title or f"Example task {next(counter)}"
    with connect(env) as con:
        return tasks.create(
            con, SYSTEM, {"workstream_id": ws["id"], "title": title}, organizers=ORGANIZERS
        )


def make_email(env, subject=None, **fields):
    n = next(counter)
    values = {
        "message_id": f"<ui-inbox-{n}@example.com>",
        "thread_key": f"<ui-inbox-{n}@example.com>",
        "from_addr": "rentals@example.org",
        "to_addrs": ["capture@example.com"],
        "cc_addrs": [],
        "subject": subject or f"Example quote {n}",
        "sent_at": "2098-01-06T09:30:00Z",
        "body_text": f"Body of example message {n}.",
    }
    values.update(fields)
    with connect(env) as con:
        row = emails.capture(con, env.cfg, ParsedEmail(**values))
    assert row is not None
    return row


def stored_email(env, email_id):
    with connect(env) as con:
        return emails.get(con, email_id)


def card(page, subject):
    return page.locator(".inbox-card", has_text=subject)


def open_inbox(page, env, subject):
    page.goto(f"{env.url}/#/inbox")
    expect(page.locator("#view [data-view='inbox'] h1")).to_have_text("Inbox")
    expect(card(page, subject)).to_have_count(1)
    return card(page, subject)


def test_inbox_card_shows_forward_note(page, env):
    ws = make_workstream(env, "Example Vendors Desk")
    with connect(env) as con:
        contacts.create(
            con,
            SYSTEM,
            {
                "name": "Example Tent Co.",
                "workstream_id": ws["id"],
                "emails": ["tents@example.org"],
            },
        )
    body = "Quote details. " * 30
    row = make_email(
        env,
        "Example tent quote",
        from_addr=ORGANIZER_A,  # an organizer's forward: the original sender is kept
        forwarded_from="tents@example.org",
        body_text=body,
        forward_note="Please compare with last year",
        attachment_names=["quote.pdf", "terms.pdf"],
    )
    c = open_inbox(page, env, "Example tent quote")
    expect(c.locator(".inbox-subject")).to_have_text("Example tent quote")
    expect(c.locator(".email-meta")).to_contain_text("tents@example.org")
    note = c.locator(".forward-note")
    expect(note).to_contain_text("Please compare with last year")
    expect(note).to_contain_text(f"Note from {ORGANIZER_A}")
    # The note is a set-apart block, visibly different from the card around it.
    note_bg = note.evaluate("el => getComputedStyle(el).backgroundColor")
    card_bg = c.evaluate("el => getComputedStyle(el).backgroundColor")
    assert note_bg != card_bg
    # Only the first 200 characters of the body are on the page until it is opened.
    preview = c.locator(".inbox-preview")
    expect(preview).to_have_text(body[:200] + "…")
    assert "quote.pdf" in c.locator(".attachments").inner_text()
    expect(c.locator(".attachment")).to_have_text(["quote.pdf", "terms.pdf"])
    expect(c.locator(".suggest-chip")).to_have_text("Suggested: Example Vendors Desk")
    for name in ("File to task…", "New task", "Dismiss"):
        expect(c.get_by_role("button", name=name)).to_be_visible()
    toggle = c.get_by_role("button", name="Show full text")
    toggle.click()
    expect(c.locator(".email-body")).to_have_text(body)
    expect(c.get_by_role("button", name="Hide text")).to_have_attribute("aria-expanded", "true")
    c.get_by_role("button", name="Hide text").click()
    expect(c.locator(".email-body-panel")).to_be_hidden()
    assert stored_email(env, row["id"])["state"] == "inbox"


def test_email_body_html_is_inert(page, env):
    ws = make_workstream(env)
    task = make_task(env, ws)
    subject = f"Inert {HOSTILE}"
    row = make_email(
        env,
        subject,
        from_addr="rentals@example.org",
        body_text=f"{HOSTILE}\n<script>window.__pwned=2</script>" + " pad" * 80,
        forward_note=HOSTILE,
        attachment_names=[HOSTILE + ".pdf"],
    )
    c = open_inbox(page, env, "Inert")
    expect(c.locator(".inbox-subject")).to_have_text(subject)
    expect(c.locator(".forward-note-text")).to_have_text(HOSTILE)
    expect(c.locator(".inbox-preview")).to_contain_text(HOSTILE)
    c.get_by_role("button", name="Show full text").click()
    expect(c.locator(".email-body")).to_contain_text("<script>window.__pwned=2</script>")
    assert page.locator("#view img, #view script").count() == 0
    assert page.evaluate("window.__pwned ?? null") is None
    # Filed to a task, the same text stays text on the Task screen.
    with connect(env) as con:
        emails.file(con, Actor("web", ORGANIZER_A), row["id"], task["id"])
    page.goto(f"{env.url}/#/task/{task['id']}")
    item = page.locator(".email-item")
    expect(item).to_have_count(1)
    item.get_by_role("button", name="Show email text").click()
    expect(item.locator(".email-body")).to_contain_text(HOSTILE)
    assert page.locator("#view img, #view script").count() == 0
    assert page.evaluate("window.__pwned ?? null") is None


def test_new_task_from_email_prefills_subject(page, env):
    ws = make_workstream(env, "Example Supplies")
    other = make_workstream(env, "Example Stage")
    subject = f"Example supplies order {next(counter)}"
    with connect(env) as con:
        contacts.create(
            con,
            SYSTEM,
            {
                "name": "Example Supply Co.",
                "workstream_id": ws["id"],
                "emails": ["orders@example.org"],
            },
        )
    row = make_email(env, subject, from_addr="orders@example.org", forward_note="Needs a task")
    c = open_inbox(page, env, subject)
    c.get_by_role("button", name="New task").click()
    title = c.get_by_label("Task title")
    expect(title).to_have_value(subject)
    # The suggested workstream is already chosen.
    select = c.get_by_label("Workstream")
    expect(select).to_have_value(str(ws["id"]))
    select.select_option(str(other["id"]))
    c.get_by_role("button", name="Create task").click()
    expect(card(page, subject)).to_have_count(0)
    expect(page.locator(".inbox-status")).to_contain_text(f"Created task {subject}")
    stored = stored_email(env, row["id"])
    assert stored["state"] == "filed"
    with connect(env) as con:
        task = tasks.get(con, stored["task_id"])
        log = activity.list_activity(con, entity_type="task", entity_id=task["id"])
    assert (task["title"], task["workstream_id"]) == (subject, other["id"])
    assert {(e["action"], e["source"], e["actor"]) for e in log} >= {
        ("file_email", "web", ORGANIZER_A)
    }
    assert any(e["note"] == "Needs a task" for e in log)
    # The status line links to the new task.
    page.locator(".inbox-status a").click()
    expect(page.locator("#view [data-view='task'] h1")).to_have_text(subject)


def test_new_task_with_edited_title_and_missing_workstream(page, env):
    ws = make_workstream(env)
    subject = f"Example unsorted {next(counter)}"
    row = make_email(env, subject)
    c = open_inbox(page, env, subject)
    c.get_by_role("button", name="New task").click()
    c.get_by_label("Task title").fill("Call the supplier")
    c.get_by_role("button", name="Create task").click()
    expect(c.locator(".field-error")).to_have_text("Choose a workstream for the task.")
    assert stored_email(env, row["id"])["state"] == "inbox"
    c.get_by_label("Workstream").select_option(str(ws["id"]))
    c.get_by_role("button", name="Create task").click()
    expect(card(page, subject)).to_have_count(0)
    with connect(env) as con:
        assert (
            tasks.get(con, stored_email(env, row["id"])["task_id"])["title"] == "Call the supplier"
        )


def test_file_to_task_with_search(page, env):
    ws = make_workstream(env, "Example Signage")
    target = make_task(env, ws, "Order banners")
    make_task(env, ws, "Print flyers")
    subject = f"Example banner proof {next(counter)}"
    row = make_email(env, subject, forward_note="For the banner task")
    c = open_inbox(page, env, subject)
    c.get_by_role("button", name="File to task…").click()
    search = c.get_by_label("Search tasks")
    search.fill("banners")
    results = c.get_by_role("list", name="Matching tasks").get_by_role("button")
    expect(results).to_have_count(1)
    expect(results).to_contain_text("Order banners")
    expect(results).to_contain_text("Example Signage")
    search.fill("zzz-no-such-task")
    expect(c.locator(".picker-results")).to_have_text("No matching tasks.")
    search.fill("banners")
    expect(results).to_have_count(1)
    results.click()
    expect(card(page, subject)).to_have_count(0)
    expect(page.locator(".inbox-status")).to_contain_text("Filed to Order banners")
    stored = stored_email(env, row["id"])
    assert (stored["state"], stored["task_id"]) == ("filed", target["id"])
    with connect(env) as con:
        entry = activity.list_activity(con, entity_type="task", entity_id=target["id"])[0]
    assert (entry["action"], entry["source"], entry["actor"]) == ("file_email", "web", ORGANIZER_A)
    assert entry["note"] == "For the banner task"


def test_dismiss_removes_card(page, env):
    subject = f"Example newsletter {next(counter)}"
    row = make_email(env, subject)
    c = open_inbox(page, env, subject)
    c.get_by_role("button", name="Dismiss").click()
    expect(card(page, subject)).to_have_count(0)
    expect(page.locator(".inbox-status")).to_contain_text("Dismissed")
    assert stored_email(env, row["id"])["state"] == "dismissed"
    page.reload()
    expect(page.locator("#view [data-view='inbox'] h1")).to_have_text("Inbox")
    expect(card(page, subject)).to_have_count(0)


def test_empty_inbox_says_so(page, env):
    with connect(env) as con:
        for row in emails.list_inbox(con):
            emails.dismiss(con, SYSTEM, row["id"])
    page.goto(f"{env.url}/#/inbox")
    expect(page.locator(".inbox-empty")).to_be_visible()
    expect(page.locator(".inbox-card")).to_have_count(0)
    # Dismissing the last card brings the message back.
    row = make_email(env)
    page.reload()
    expect(page.locator(".inbox-card")).to_have_count(1)
    expect(page.locator(".inbox-empty")).to_be_hidden()
    page.get_by_role("button", name="Dismiss").click()
    expect(page.locator(".inbox-card")).to_have_count(0)
    expect(page.locator(".inbox-empty")).to_be_visible()
    assert stored_email(env, row["id"])["state"] == "dismissed"


def test_task_shows_linked_emails(page, env):
    ws = make_workstream(env)
    task = make_task(env, ws, "Example linked task")
    bare = make_task(env, ws, "Example task without email")
    body = "Line one of the email.\nLine two of the email. " + "More text. " * 40
    row = make_email(env, "Example filed quote", body_text=body, forward_note="Keep this one")
    with connect(env) as con:
        emails.file(con, Actor("web", ORGANIZER_A), row["id"], task["id"])
    page.goto(f"{env.url}/#/task/{task['id']}")
    expect(page.locator("#view [data-view='task'] h1")).to_have_text("Example linked task")
    section = page.locator("[data-section='linked-emails']")
    expect(section).to_be_visible()
    item = section.locator(".email-item")
    expect(item).to_have_count(1)
    expect(item.locator(".email-subject")).to_have_text("Example filed quote")
    expect(item.locator(".email-meta")).to_contain_text("rentals@example.org")
    expect(item.locator(".forward-note")).to_contain_text("Keep this one")
    expect(item.locator(".email-body")).to_have_count(0)  # collapsed until asked for
    item.get_by_role("button", name="Show email text").click()
    expect(item.locator(".email-body")).to_have_text(body)
    item.get_by_role("button", name="Hide text").click()
    expect(item.locator(".email-body-panel")).to_be_hidden()
    # The email's filing also shows in the task's activity, with the note.
    expect(page.get_by_role("list", name="Activity").locator("li").first).to_contain_text("web")
    # A task with no emails keeps the section hidden.
    page.goto(f"{env.url}/#/task/{bare['id']}")
    expect(page.locator("#view [data-view='task'] h1")).to_have_text("Example task without email")
    expect(page.locator("[data-section='linked-emails']")).to_be_hidden()


def test_task_keeps_emails_after_a_save(page, env):
    ws = make_workstream(env)
    task = make_task(env, ws)
    row = make_email(env, "Example kept quote")
    with connect(env) as con:
        emails.file(con, Actor("web", ORGANIZER_A), row["id"], task["id"])
    page.goto(f"{env.url}/#/task/{task['id']}")
    expect(page.locator(".email-item")).to_have_count(1)
    title = page.get_by_label("Title")
    title.fill("Example renamed with email")
    title.blur()
    expect(page.locator("#view [data-view='task'] h1")).to_have_text("Example renamed with email")
    expect(page.locator(".email-item")).to_have_count(1)
    expect(page.locator("[data-section='linked-emails']")).to_be_visible()


def test_inbox_has_no_horizontal_scroll_with_long_subject(page, env):
    subject = "🎪" * 150
    make_email(env, subject, from_addr="a" * 120 + "@example.org", forward_note="N" * 300)
    page.set_viewport_size({"width": 390, "height": 800})
    page.goto(f"{env.url}/#/inbox")
    expect(page.locator(".inbox-card", has_text="🎪").first).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
