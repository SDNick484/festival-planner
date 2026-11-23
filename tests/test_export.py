"""CSV export (Task 25): organizer downloads of tasks, contacts and the activity log.

Web auth only: `/api/export/<kind>.csv` is mounted under `/api` alone, so a machine token gets a
404 on `/mcp/api/export/*`. Every text cell that starts with `= + - @`, a tab or a carriage return
gets a single quote in front, so a spreadsheet shows it as text. Activity rows carry the names of
the fields they changed, never their values, and never an email's subject, body or forward note.
The budget export is covered in `test_budget`.
"""

from __future__ import annotations

import csv
import io

import pytest
from access_keys import ORGANIZER_A, READ_SECRET, WRITE_SECRET, AccessKeys, make_config

from festival_planner import db
from festival_planner.api import export
from festival_planner.mail.parse import ParsedEmail
from festival_planner.service import Actor, emails
from festival_planner.web import create_app

JWT_HEADER = "Cf-Access-Jwt-Assertion"
ORIGIN = "http://localhost"
ACTOR = Actor("web", ORGANIZER_A)

TASK_COLUMNS = [
    "id",
    "workstream_id",
    "title",
    "owner",
    "status",
    "due_date",
    "effective_due",
    "milestone_id",
    "waiting_on",
    "blocked_reason",
    "notes",
    "links",
    "done_at",
    "created_at",
    "updated_at",
]
CONTACT_COLUMNS = [
    "id",
    "workstream_id",
    "name",
    "organization",
    "role",
    "emails",
    "phone",
    "notes",
    "created_at",
    "updated_at",
]
ACTIVITY_COLUMNS = [
    "id",
    "at",
    "source",
    "actor",
    "entity_type",
    "entity_id",
    "action",
    "changed_fields",
    "note",
]
# Cells that a spreadsheet would read as a formula, or that it strips or splits on.
PAYLOADS = ["=HYPERLINK(1)", "+1+1", "-2+3", "@SUM(A1)", "\tTAB", "\rCR"]


@pytest.fixture
def keys(monkeypatch):
    return AccessKeys(monkeypatch)


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path)


@pytest.fixture
def app(cfg, keys):
    app = create_app(cfg)
    app.config.update(TESTING=True)
    return app


@pytest.fixture
def con(app):
    con = db.connect(app.config["DATABASE"])
    yield con
    con.close()


class Api:
    def __init__(self, app, keys):
        self.client = app.test_client()
        self.keys = keys

    def call(self, method, path, body=None):
        headers = {JWT_HEADER: self.keys.sign(ORGANIZER_A), "Origin": ORIGIN}
        return self.client.open(path, method=method, headers=headers, json=body)

    def make(self, path, body):
        r = self.call("POST", path, body)
        assert r.status_code == 201, r.json
        return r.json

    def export(self, kind):
        r = self.call("GET", f"/api/export/{kind}.csv")
        assert r.status_code == 200, r.get_data()
        return parse(r.get_data())


@pytest.fixture
def api(app, keys):
    return Api(app, keys)


@pytest.fixture
def public(app):
    """A client with no credentials at all."""
    return app.test_client()


def parse(body: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(body.decode("utf-8"), newline="")))


def as_dicts(rows: list[list[str]]) -> list[dict[str, str]]:
    header, *data = rows
    return [dict(zip(header, row, strict=True)) for row in data]


def test_tasks_csv_columns(api):
    ws = api.make("/api/workstreams", {"name": "Food"})["id"]
    task = api.make(
        "/api/tasks",
        {
            "workstream_id": ws,
            "title": "Order tents",
            "owner": ORGANIZER_A,
            "due_date": "2099-10-01",
            "notes": "Call before noon",
        },
    )
    r = api.call("GET", "/api/export/tasks.csv")
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "text/csv; charset=utf-8"
    assert r.headers["Content-Disposition"] == 'attachment; filename="tasks.csv"'
    assert r.headers["Cache-Control"] == "no-store"
    rows = parse(r.get_data())
    assert rows[0] == TASK_COLUMNS
    assert len(rows) == 2
    row = as_dicts(rows)[0]
    assert row["id"] == str(task["id"])
    assert row["workstream_id"] == str(ws)
    assert row["title"] == "Order tents" and row["owner"] == ORGANIZER_A
    assert row["status"] == "todo" and row["due_date"] == "2099-10-01"
    assert row["effective_due"] == "2099-10-01" and row["notes"] == "Call before noon"


def test_contacts_and_activity_csv_columns(api):
    ws = api.make("/api/workstreams", {"name": "Food"})["id"]
    api.make(
        "/api/contacts",
        {
            "name": "Pat Example",
            "workstream_id": ws,
            "emails": ["pat@example.com", "p@example.org"],
        },
    )
    contact_rows = api.export("contacts")
    assert contact_rows[0] == CONTACT_COLUMNS
    assert as_dicts(contact_rows)[0]["emails"] == "pat@example.com, p@example.org"
    activity_rows = api.export("activity")
    assert activity_rows[0] == ACTIVITY_COLUMNS
    created = [r for r in as_dicts(activity_rows) if r["action"] == "create"]
    assert {r["entity_type"] for r in created} >= {"workstream", "contact"}
    contact_created = [r for r in created if r["entity_type"] == "contact"]
    assert "name" in contact_created[0]["changed_fields"].split(", ")


@pytest.mark.parametrize("payload", PAYLOADS)
def test_csv_injection_escaped(api, payload):
    # Notes are kept verbatim, so they carry the leading tab and carriage return; the title and
    # name fields are trimmed by the service, so they carry the printable payloads.
    ws = api.make("/api/workstreams", {"name": "Food"})["id"]
    api.make("/api/tasks", {"workstream_id": ws, "title": "Row", "notes": payload})
    api.make("/api/contacts", {"name": "Row", "notes": payload})
    expected = "'" + payload
    assert as_dicts(api.export("tasks"))[0]["notes"] == expected
    assert as_dicts(api.export("contacts"))[0]["notes"] == expected
    if payload[0] not in "\t\r":
        api.make("/api/tasks", {"workstream_id": ws, "title": payload})
        titles = [r["title"] for r in as_dicts(api.export("tasks"))]
        assert expected in titles


def test_csv_injection_leaves_other_cells_alone(api):
    ws = api.make("/api/workstreams", {"name": "Food"})["id"]
    api.make("/api/tasks", {"workstream_id": ws, "title": "a=b", "notes": "x-y and 1+1 inside"})
    row = as_dicts(api.export("tasks"))[0]
    assert row["title"] == "a=b"
    assert row["notes"] == "x-y and 1+1 inside"
    assert row["id"] == "1"  # numbers are written as they are


def test_export_requires_auth(app, public):
    for kind in ("tasks", "contacts", "activity", "budget"):
        assert public.get(f"/api/export/{kind}.csv").status_code == 403
        bearer = {"Authorization": f"Bearer {WRITE_SECRET}"}
        assert public.get(f"/api/export/{kind}.csv", headers=bearer).status_code == 401
        for secret in (WRITE_SECRET, READ_SECRET):
            r = public.get(
                f"/mcp/api/export/{kind}.csv", headers={"Authorization": f"Bearer {secret}"}
            )
            assert r.status_code == 404
    rules = [rule.rule for rule in app.url_map.iter_rules()]
    assert "/api/export/<kind>.csv" in rules
    assert not [r for r in rules if r.startswith("/mcp") and "export" in r]


def test_unknown_kind_is_404(api):
    for kind in ("nope", "tasks.csv", "budgets"):
        r = api.call("GET", f"/api/export/{kind}.csv")
        assert r.status_code == 404, kind
        assert r.json == {"error": "not found", "fields": {}}


def test_export_excludes_deleted_rows(api):
    ws = api.make("/api/workstreams", {"name": "Food"})["id"]
    keep = api.make("/api/tasks", {"workstream_id": ws, "title": "Keep"})["id"]
    gone = api.make("/api/tasks", {"workstream_id": ws, "title": "Gone"})["id"]
    contact = api.make("/api/contacts", {"name": "Gone too"})["id"]
    assert api.call("DELETE", f"/api/tasks/{gone}").status_code == 204
    assert api.call("DELETE", f"/api/contacts/{contact}").status_code == 204
    assert [r["id"] for r in as_dicts(api.export("tasks"))] == [str(keep)]
    assert as_dicts(api.export("contacts")) == []


def test_export_pages_through_every_row(api, con, monkeypatch):
    monkeypatch.setattr(export, "PAGE", 2)
    ws = api.make("/api/workstreams", {"name": "Food"})["id"]
    ids = [api.make("/api/tasks", {"workstream_id": ws, "title": f"T{n}"})["id"] for n in range(5)]
    assert [r["id"] for r in as_dicts(api.export("tasks"))] == [str(i) for i in ids]
    activity_rows = as_dicts(api.export("activity"))
    (logged,) = con.execute("SELECT count(*) FROM activity").fetchone()
    assert len(activity_rows) == logged > 5
    assert [int(r["id"]) for r in activity_rows] == sorted(int(r["id"]) for r in activity_rows)


def test_activity_excludes_email_text(api, con, cfg):
    ws = api.make("/api/workstreams", {"name": "Food"})["id"]
    task_id = api.make("/api/tasks", {"workstream_id": ws, "title": "Order tents"})["id"]
    parsed = ParsedEmail(
        message_id="<export-1@example.com>",
        thread_key="<export-1@example.com>",
        from_addr="rentals@example.org",
        to_addrs=["capture@example.com"],
        cc_addrs=[],
        subject="Secret subject line",
        sent_at="2098-01-06T09:30:00Z",
        body_text="Secret body text.",
        forward_note="Secret forward note",
    )
    stored = emails.capture(con, cfg, parsed)
    emails.file(con, ACTOR, stored["id"], task_id)

    activity_rows = as_dicts(api.export("activity"))
    assert not [r for r in activity_rows if r["entity_type"] == "email_item"]
    filed = [r for r in activity_rows if r["action"] == "file_email"]
    assert len(filed) == 1 and filed[0]["note"] == "" and filed[0]["changed_fields"] == "email_item"
    for kind in ("tasks", "contacts", "activity"):
        body = api.call("GET", f"/api/export/{kind}.csv").get_data().decode("utf-8")
        for secret in ("Secret subject line", "Secret body text.", "Secret forward note"):
            assert secret not in body, (kind, secret)
    # Values are never exported in the activity log, only the names of the fields they changed.
    body = api.call("GET", "/api/export/activity.csv").get_data().decode("utf-8")
    assert "Order tents" not in body
    created = [r for r in activity_rows if r["action"] == "create" and r["entity_type"] == "task"]
    assert "title" in created[0]["changed_fields"].split(", ")


def test_csv_escape_unit():
    assert export.escape_cell("=1") == "'=1"
    assert export.escape_cell("\tx") == "'\tx"
    assert export.escape_cell("\rx") == "'\rx"
    assert export.escape_cell("plain") == "plain"
    assert export.escape_cell(7) == 7
    assert export.escape_cell(None) is None
