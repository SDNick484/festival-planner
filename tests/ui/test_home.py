"""Home dashboard (Task 10): section order, links, the collapsed Due this week list, the inbox
nudge, long and hostile titles.

Each test gets its own app, database and live server (see `app`), so the counts on Home are exactly
what the test seeds. Data goes in through the service layer as the web actor. After every
navigation or click the assertions use auto-retrying `expect(...)`, never an immediate read.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from festival_planner import clock, db
from festival_planner.config import Config, load_config
from festival_planner.mail.parse import ParsedEmail
from festival_planner.service import Actor, emails, tasks, workstreams
from festival_planner.web import create_app

ROOT = Path(__file__).resolve().parents[2]
ORGANIZER = "organizer-a@example.com"
WEB = Actor("web", ORGANIZER)
VIEWPORT = {"width": 390, "height": 800}
HOSTILE = "<img src=x onerror=alert(1)>"


@dataclass
class App:
    url: str
    cfg: Config
    monkeypatch: pytest.MonkeyPatch
    con: object

    @property
    def today(self) -> date:
        return clock.today(self.cfg)

    def advance(self, days: int) -> None:
        """Make the server's "now" `days` later than the real now (seeded activity stays old)."""
        later = datetime.now(UTC) + timedelta(days=days)
        self.monkeypatch.setattr(clock, "now", lambda: later)

    def workstream(self, name: str) -> int:
        return workstreams.create(self.con, WEB, {"name": name})["id"]

    def task(self, workstream_id: int, title: str, **fields) -> int:
        data = {"workstream_id": workstream_id, "title": title, **fields}
        return tasks.create(self.con, WEB, data)["id"]

    def due(self, days_from_today: int) -> str:
        return (self.today + timedelta(days=days_from_today)).isoformat()

    def email(self, n: int) -> None:
        parsed = ParsedEmail(
            message_id=f"<home-{n}@example.com>",
            thread_key=f"<home-{n}@example.com>",
            from_addr="rentals@example.org",
            to_addrs=["capture@example.com"],
            cc_addrs=[],
            subject=f"Message {n}",
            sent_at="2098-01-06T09:30:00Z",
            body_text="Body.",
        )
        assert emails.capture(self.con, self.cfg, parsed) is not None

    def open_home(self, page) -> None:
        page.set_viewport_size(VIEWPORT)
        page.goto(self.url + "/#/")
        expect(page.locator("#view h1")).to_have_text("Home")


@pytest.fixture
def app(tmp_path, monkeypatch):
    example = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        example.replace('data_dir = "data"', f'data_dir = "{tmp_path.as_posix()}"'),
        encoding="utf-8",
    )
    cfg = load_config(config_path, env={"FLASK_ENV": "development", "DEV_AUTH_EMAIL": ORGANIZER})
    flask_app = create_app(cfg)
    server = make_server("127.0.0.1", 0, flask_app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    con = db.connect(flask_app.config["DATABASE"])
    try:
        yield App(f"http://127.0.0.1:{server.server_port}", cfg, monkeypatch, con)
    finally:
        con.close()
        server.shutdown()
        thread.join(timeout=5)


def seed_everything(app: App) -> dict:
    """One of each: overdue, blocked, waiting, two due this week, two emails, a workstream."""
    app.advance(6)  # a task set to waiting now has been waiting 6 days when Home is opened
    ws = app.workstream("Tent Crew")
    ids = {
        "overdue": app.task(ws, "Sign the tent contract", due_date=app.due(-2)),
        "blocked": app.task(
            ws, "Book the sound check", status="blocked", blocked_reason="Venue not confirmed"
        ),
        "waiting": app.task(ws, "Get the print proof", status="waiting", waiting_on="Print Shop"),
        "due1": app.task(ws, "Collect volunteer forms", due_date=app.due(0)),
        "due2": app.task(ws, "Order bottled water", due_date=app.due(3)),
        "done": app.task(ws, "Reserve the field", status="done"),
    }
    app.email(1)
    app.email(2)
    return ids


def test_home_sections_order(app, page):
    seed_everything(app)
    app.open_home(page)

    # The h1 plus one heading per section, top to bottom. to_have_text on a list retries.
    expect(page.locator("#view h1, #view h2")).to_have_text(
        ["Home", "Needs attention", "Due this week 2", "Workstreams"]
    )
    sections = page.locator("#view [data-section]")
    expect(sections).to_have_count(5)
    assert sections.evaluate_all("els => els.map(e => e.dataset.section)") == [
        "header",
        "inbox",
        "attention",
        "due",
        "workstreams",
    ]

    # Header: the countdown label and the overall done % (1 of 6 tasks done).
    header = page.locator("[data-section='header']")
    expect(header.locator(".countdown")).to_have_text(re.compile(r"^\d+ days$"))
    expect(header.locator(".overall")).to_contain_text("17%")
    expect(header.get_by_role("link", name="Manage")).to_have_attribute("href", "#/manage")

    nudge = page.locator("[data-section='inbox']")
    expect(nudge).to_have_text("2 emails to file →")
    expect(nudge).to_have_attribute("href", "#/inbox")

    # Needs attention: overdue, blocked, waiting, with dots and right-aligned reasons.
    rows = page.locator("[data-section='attention'] a.row")
    expect(rows).to_have_count(3)
    expect(rows.locator(".reason")).to_have_text(["overdue 2d", "blocked", "waiting 6d"])
    expect(rows.locator(".title")).to_have_text(
        ["Sign the tent contract", "Book the sound check", "Get the print proof"]
    )
    expect(rows.locator(".dot")).to_have_class(
        [re.compile(r"dot-danger"), re.compile(r"dot-danger"), re.compile(r"dot-warn")]
    )

    # Workstreams: name, bar, and chips for its problems.
    ws_row = page.locator("[data-section='workstreams'] a.ws-row")
    expect(ws_row).to_have_count(8)  # the 7 from the example config, plus this one
    vendors = ws_row.filter(has_text="Tent Crew")
    expect(vendors.locator("progress")).to_have_attribute("value", "17")
    expect(vendors.locator(".chip")).to_have_text(["1 overdue", "1 blocked", "1 waiting"])


def test_attention_row_links_to_task(app, page):
    ids = seed_everything(app)
    app.open_home(page)
    row = page.locator("[data-section='attention'] a.row").filter(has_text="Sign the tent")
    expect(row).to_have_attribute("href", f"#/task/{ids['overdue']}")
    row.click()
    expect(page.locator("#view [data-view='task']")).to_have_attribute(
        "data-id", str(ids["overdue"])
    )
    expect(page).to_have_url(re.compile(rf"#/task/{ids['overdue']}$"))

    # A workstream row opens its workstream.
    app.open_home(page)
    page.locator("[data-section='workstreams'] a.ws-row").filter(has_text="Tent Crew").click()
    expect(page.locator("#view [data-view='workstream']")).to_have_attribute(
        "data-id", re.compile(r"^\d+$")
    )

    # The Manage link goes to #/manage.
    app.open_home(page)
    page.get_by_role("link", name="Manage").click()
    expect(page.locator("#view [data-view='manage']")).to_have_count(1)


def test_due_this_week_collapsed_then_expands(app, page):
    seed_everything(app)
    app.open_home(page)
    toggle = page.get_by_role("button", name=re.compile(r"^Due this week"))
    items = page.locator("[data-section='due'] a.row")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(toggle).to_contain_text("2")
    expect(items).to_have_count(2)  # in the DOM for a11y tools, but hidden
    expect(items.first).to_be_hidden()

    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(items.first).to_be_visible()
    expect(items.locator(".title")).to_have_text(["Collect volunteer forms", "Order bottled water"])
    expect(items.first).to_have_attribute("href", re.compile(r"^#/task/\d+$"))

    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(items.first).to_be_hidden()


def test_inbox_nudge_hidden_when_zero(app, page):
    app.open_home(page)
    # The view renders in one step, so once its sections are there the nudge is settled.
    expect(page.locator("#view h2")).to_have_text(
        ["Needs attention", "Due this week", "Workstreams"]
    )
    expect(page.locator("[data-section='inbox']")).to_have_count(0)
    # Empty states.
    expect(page.locator("[data-section='attention']")).to_contain_text("Nothing needs attention 🎉")
    expect(page.locator("[data-section='due']")).to_contain_text("Nothing due this week")
    expect(page.locator("[data-section='header'] .overall")).to_contain_text("0%")

    app.email(1)
    page.reload()
    nudge = page.locator("[data-section='inbox']")
    expect(nudge).to_have_text("1 email to file →")


def test_home_long_title_truncates(app, page):
    ws = app.workstream("W" * 200)
    emoji = "🎪" * 200  # the longest title the API accepts
    app.task(ws, emoji, due_date=app.due(-1))
    app.task(ws, "T" * 200, due_date=app.due(2))
    app.task(ws, "B" * 200, status="blocked", blocked_reason="R" * 200)
    app.open_home(page)

    row = page.locator("[data-section='attention'] a.row").first
    expect(row.locator(".title")).to_have_text(emoji)
    expect(row.locator(".reason")).to_have_text("overdue 1d")
    page.get_by_role("button", name=re.compile(r"^Due this week")).click()
    expect(page.locator("[data-section='due'] a.row").first).to_be_visible()

    no_scroll = "document.documentElement.scrollWidth <= window.innerWidth"
    assert page.evaluate(no_scroll)
    # Truncated with an ellipsis, not wrapped: every title stays on one line (an emoji line is
    # about 26px tall, two lines would be over 50).
    heights = page.locator("#view .title").evaluate_all(
        "els => els.map(e => e.getBoundingClientRect().height)"
    )
    assert len(heights) >= 7 and max(heights) < 40
    # And it holds on the two-column desktop layout.
    page.set_viewport_size({"width": 1000, "height": 800})
    expect(page.locator(".view-home")).to_have_css("display", "grid")
    assert page.evaluate(no_scroll)


def test_html_in_title_is_inert(app, page):
    dialogs: list[str] = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    ws = app.workstream(HOSTILE)
    app.task(ws, HOSTILE, due_date=app.due(-1))
    app.task(ws, HOSTILE, due_date=app.due(1))
    app.open_home(page)

    row = page.locator("[data-section='attention'] a.row")
    expect(row.locator(".title")).to_have_text(HOSTILE)
    page.get_by_role("button", name=re.compile(r"^Due this week")).click()
    expect(page.locator("[data-section='due'] a.row .title")).to_have_text(HOSTILE)
    expect(
        page.locator("[data-section='workstreams'] a.ws-row").filter(has_text=HOSTILE)
    ).to_have_count(1)
    expect(page.locator("#view img")).to_have_count(0)
    assert dialogs == []
