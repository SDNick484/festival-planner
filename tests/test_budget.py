"""Budget tracker (Task 22): service math and validation, the API, the MCP tools, the digest, the
CSV export and the `currency` setting.

Money is integer cents everywhere. Every figure here is invented. Tokens never get a delete tool
(see `test_mcp_write_tools`), and every change records its source and actor from the identity.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
from access_keys import ORGANIZER_A, READ_SECRET, WRITE_SECRET, AccessKeys, make_config

import festival_planner.mcp.tools  # noqa: F401  (registers the tools)
from festival_planner import db, views
from festival_planner.config import ConfigError, load_config
from festival_planner.mcp.server import TOOLS
from festival_planner.service import Actor, NotFound, ValidationError, activity, budget, event
from festival_planner.service import workstreams as ws_service
from festival_planner.web import create_app

WEB = Actor("web", ORGANIZER_A)
JWT_HEADER = "Cf-Access-Jwt-Assertion"
ORIGIN = "http://localhost"
READ = {"Authorization": f"Bearer {READ_SECRET}"}
WRITE = {"Authorization": f"Bearer {WRITE_SECRET}"}
EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path, event_date=date(2099, 10, 10))


@pytest.fixture
def keys(monkeypatch):
    return AccessKeys(monkeypatch)


@pytest.fixture
def app(cfg, keys):
    app = create_app(cfg)
    app.config.update(TESTING=True)
    return app


@pytest.fixture
def con(app):
    with closing(db.connect(app.config["DATABASE"])) as con:
        yield con


@pytest.fixture
def ws(con):
    return ws_service.create(con, WEB, {"name": "Vendors"})["id"]


def add(con, label, kind, cents, state="estimated", **extra):
    data = {"label": label, "kind": kind, "amount_cents": cents, "state": state, **extra}
    return budget.create(con, WEB, data)


def log(con, entity_id, entity_type="budget_item"):
    return activity.list_activity(con, entity_type=entity_type, entity_id=entity_id)


# --- summary -------------------------------------------------------------------------------


def test_summary_math(con):
    add(con, "Example Tent Co. rental", "cost", 40_000, "estimated")
    add(con, "Sound hire", "cost", 30_000, "pledged")  # committed
    add(con, "Printing", "cost", 10_000, "received")  # paid
    add(con, "Ticket sales", "revenue", 100_000, "estimated")
    add(con, "Sponsor gift", "revenue", 50_000, "pledged")
    add(con, "Raffle", "revenue", 20_000, "received")
    gone = add(con, "Dropped idea", "cost", 999_999, "estimated")
    budget.delete(con, WEB, gone["id"])  # a deleted item never counts
    event.update(con, WEB, {"net_goal_cents": 80_000})

    assert budget.summary(con) == {
        "revenue": {"estimated": 100_000, "pledged": 50_000, "received": 20_000},
        "cost": {"estimated": 40_000, "pledged": 30_000, "received": 10_000},
        # projected: every revenue and cost line; actual: only revenue received less cost paid
        "net_projected": 170_000 - 80_000,
        "net_actual": 20_000 - 10_000,
        "goal": 80_000,
        "pct_of_goal": 112.5,
    }
    assert isinstance(budget.summary(con)["net_projected"], int)


def test_summary_empty_and_negative_net(con):
    assert budget.summary(con) == {
        "revenue": {"estimated": 0, "pledged": 0, "received": 0},
        "cost": {"estimated": 0, "pledged": 0, "received": 0},
        "net_projected": 0,
        "net_actual": 0,
        "goal": None,
        "pct_of_goal": None,
    }
    add(con, "Venue", "cost", 5_000)
    event.update(con, WEB, {"net_goal_cents": 20_000})
    summary = budget.summary(con)
    assert summary["net_projected"] == -5_000
    assert summary["pct_of_goal"] == -25.0


def test_goal_none_pct_none(con):
    add(con, "Ticket sales", "revenue", 10_000)
    assert budget.summary(con)["goal"] is None
    assert budget.summary(con)["pct_of_goal"] is None
    event.update(con, WEB, {"net_goal_cents": 0})  # a goal of zero has no percentage either
    assert budget.summary(con)["goal"] == 0
    assert budget.summary(con)["pct_of_goal"] is None
    event.update(con, WEB, {"net_goal_cents": 20_000})
    assert budget.summary(con)["pct_of_goal"] == 50.0
    event.update(con, WEB, {"net_goal_cents": None})  # null clears the goal
    assert budget.summary(con)["goal"] is None and budget.summary(con)["pct_of_goal"] is None


def test_summary_without_an_event_row(tmp_path):
    with closing(db.connect(tmp_path / "bare.db")) as bare:
        db.migrate(bare)
        assert budget.summary(bare)["goal"] is None


# --- validation ----------------------------------------------------------------------------


def test_negative_amount_rejected(con):
    for bad in (-1, -10_000, 12.5, "100", None, True, 10**13):
        with pytest.raises(ValidationError) as exc:
            add(con, "Bad", "cost", bad)
        assert set(exc.value.errors) == {"amount_cents"}, bad
    item = add(con, "Fine", "cost", 0)  # zero is allowed
    with pytest.raises(ValidationError):
        budget.update(con, WEB, item["id"], {"amount_cents": -5})
    assert budget.get(con, item["id"])["amount_cents"] == 0
    assert con.execute("SELECT count(*) FROM budget_item").fetchone()[0] == 1


def test_amount_is_checked_in_sql_too(con):
    bad_rows = (
        "('x', 'cost', -1, 'estimated')",
        "('x', 'gift', 1, 'estimated')",
        "('x', 'cost', 1, 'maybe')",
    )
    insert = "INSERT INTO budget_item (label, kind, amount_cents, state) VALUES "
    for values in bad_rows:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(insert + values)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE event SET net_goal_cents = -1")
    con.rollback()


def test_validation_of_other_fields(con, ws):
    with pytest.raises(ValidationError) as exc:
        budget.create(con, WEB, {"label": "", "kind": "gift", "amount_cents": 1, "state": "x"})
    assert set(exc.value.errors) == {"label", "kind", "state"}
    with pytest.raises(ValidationError) as exc:
        budget.create(con, WEB, {"label": "A"})
    assert set(exc.value.errors) == {"kind", "amount_cents"}
    with pytest.raises(ValidationError) as exc:
        budget.create(con, WEB, {"label": "A", "kind": "cost", "amount_cents": 1, "extra": 1})
    assert exc.value.errors == {"extra": "unknown field"}
    with pytest.raises(ValidationError) as exc:
        add(con, "A", "cost", 1, workstream_id=ws + 99)
    assert exc.value.errors == {"workstream_id": "no such workstream"}
    for bad in (-1, 1.5, True, "5"):
        with pytest.raises(ValidationError) as exc:
            event.update(con, WEB, {"net_goal_cents": bad})
        assert set(exc.value.errors) == {"net_goal_cents"}, bad


def test_defaults_and_get(con, ws):
    item = budget.create(
        con, WEB, {"label": " Tent ", "kind": "cost", "amount_cents": 2_500, "workstream_id": ws}
    )
    assert item["label"] == "Tent"
    assert item["state"] == "estimated" and item["notes"] == ""
    assert budget.get(con, item["id"]) == item
    assert [i["id"] for i in budget.list(con)] == [item["id"]]
    assert budget.list(con, workstream_id=ws + 1) == []


def test_workstream_with_budget_items_cannot_be_deleted(con, ws):
    item = add(con, "Tent", "cost", 100, workstream_id=ws)
    with pytest.raises(ValidationError) as exc:
        ws_service.delete(con, WEB, ws)
    assert "budget" in exc.value.errors
    budget.delete(con, WEB, item["id"])
    ws_service.delete(con, WEB, ws)


# --- activity ------------------------------------------------------------------------------


def test_budget_activity_logged(con, ws):
    item = add(con, "Tent", "cost", 12_000, workstream_id=ws)
    created = log(con, item["id"])[0]
    assert (created["source"], created["actor"], created["action"]) == (
        "web",
        ORGANIZER_A,
        "create",
    )
    assert created["diff"]["amount_cents"] == [None, 12_000]
    assert created["diff"]["kind"] == [None, "cost"]

    budget.update(con, Actor("claude", "claude"), item["id"], {"state": "pledged"})
    updated = log(con, item["id"])[0]
    assert (updated["source"], updated["actor"], updated["action"]) == (
        "claude",
        "claude",
        "update",
    )
    assert updated["diff"] == {"state": ["estimated", "pledged"]}

    n = len(log(con, item["id"]))
    budget.update(con, WEB, item["id"], {"state": "pledged"})  # no change, nothing logged
    assert len(log(con, item["id"])) == n

    event.update(con, WEB, {"net_goal_cents": 5_000})
    goal = log(con, 1, "event")[0]
    assert goal["action"] == "update" and goal["diff"] == {"net_goal_cents": [None, 5_000]}

    budget.delete(con, WEB, item["id"])
    assert log(con, item["id"])[0]["action"] == "delete"
    assert budget.list(con) == []
    with pytest.raises(NotFound):
        budget.get(con, item["id"])


# --- API -----------------------------------------------------------------------------------


class Api:
    def __init__(self, app, keys):
        self.client = app.test_client()
        self.keys = keys

    def call(self, method, path, body=None):
        headers = {JWT_HEADER: self.keys.sign(ORGANIZER_A), "Origin": ORIGIN}
        return self.client.open(path, method=method, headers=headers, json=body)

    def token(self, method, path, secret, body=None):
        headers = {"Authorization": f"Bearer {secret}"}
        return self.client.open(path, method=method, headers=headers, json=body)


@pytest.fixture
def api(app, keys):
    return Api(app, keys)


def test_api_crud_and_overview(api, ws):
    r = api.call(
        "POST",
        "/api/budget/items",
        {"label": "Tent", "kind": "cost", "amount_cents": 25_000, "workstream_id": ws},
    )
    assert r.status_code == 201, r.json
    item = r.json
    assert item["amount_cents"] == 25_000 and item["state"] == "estimated"
    assert api.call("GET", f"/api/budget/items/{item['id']}").json == item
    r = api.call("PATCH", f"/api/budget/items/{item['id']}", {"state": "received"})
    assert r.status_code == 200 and r.json["state"] == "received"
    assert api.call("PUT", "/api/event", {"net_goal_cents": 100_000}).status_code == 200

    got = api.call("GET", "/api/budget").json
    assert set(got) == {"currency", "summary", "items"}
    assert got["currency"] == "USD"
    assert got["summary"]["goal"] == 100_000 and got["summary"]["cost"]["received"] == 25_000
    assert got["summary"]["net_projected"] == -25_000
    assert [i["id"] for i in got["items"]] == [item["id"]]

    assert api.call("DELETE", f"/api/budget/items/{item['id']}").status_code == 204
    assert api.call("GET", f"/api/budget/items/{item['id']}").status_code == 404
    assert api.call("GET", "/api/budget").json["items"] == []


def test_api_bad_input_is_400_not_500(api):
    for body in (
        {"label": "A", "kind": "cost", "amount_cents": -1},
        {"label": "A", "kind": "cost", "amount_cents": 1.5},
        {"label": "A", "kind": "cost", "amount_cents": "1"},
        {"label": "A", "kind": "cost"},
        {"label": "A", "kind": "cost", "amount_cents": 1, "bogus": 1},
        [],
    ):
        r = api.call("POST", "/api/budget/items", body)
        assert r.status_code == 400, (body, r.json)
        assert "error" in r.json
    assert api.call("PATCH", "/api/budget/items/999", {"state": "paid"}).status_code == 400
    assert api.call("PATCH", "/api/budget/items/999", {"state": "pledged"}).status_code == 404
    assert api.call("GET", "/api/budget?x=1").status_code == 400


def test_budget_is_mirrored_for_tokens_with_their_scopes(api, con):
    body = {"label": "Tent", "kind": "cost", "amount_cents": 100}
    assert api.token("POST", "/mcp/api/budget/items", READ_SECRET, body).status_code == 403
    assert api.token("POST", "/mcp/api/budget/items", WRITE_SECRET, body).status_code == 201
    got = api.token("GET", "/mcp/api/budget", READ_SECRET).json
    assert got["summary"]["cost"]["estimated"] == 100
    assert activity.list_activity(con)[0]["source"] == "claude"
    assert api.client.get("/api/budget").status_code == 403  # no credentials at all


# --- MCP tools -----------------------------------------------------------------------------


@pytest.fixture
def client(app):
    return app.test_client()


def call(client, name, arguments=None, *, headers=WRITE):
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    return client.post("/mcp", json=msg, headers=headers).json


def result(client, name, arguments=None, **kw):
    reply = call(client, name, arguments, **kw)
    assert "error" not in reply, reply
    res = reply["result"]
    assert not res.get("isError"), res
    assert json.loads(res["content"][0]["text"]) == res["structuredContent"]
    return res["structuredContent"]


def test_mcp_tools_registered_with_scopes():
    assert TOOLS["get_budget"].scope == "read"
    assert TOOLS["upsert_budget_item"].scope == "write"
    for name in ("get_budget", "upsert_budget_item"):
        assert TOOLS[name].schema["additionalProperties"] is False
        assert not {"actor", "source"} & TOOLS[name].schema["properties"].keys()
    assert TOOLS["upsert_budget_item"].schema["properties"]["amount_cents"]["minimum"] == 0


def test_get_budget_tool(client, con, ws):
    add(con, "Tent", "cost", 4_000, workstream_id=ws)
    event.update(con, WEB, {"net_goal_cents": 1_000})
    for headers in (READ, WRITE):
        got = result(client, "get_budget", headers=headers)
        assert got["currency"] == "USD"
        assert got["summary"]["cost"]["estimated"] == 4_000
        assert got["summary"]["goal"] == 1_000
        assert [i["label"] for i in got["items"]] == ["Tent"]


def test_upsert_budget_item_tool(client, con, ws):
    made = result(
        client,
        "upsert_budget_item",
        {"label": "Sponsor gift", "kind": "revenue", "amount_cents": 7_500, "workstream_id": ws},
    )["item"]
    assert made["state"] == "estimated"
    changed = result(client, "upsert_budget_item", {"id": made["id"], "state": "pledged"})["item"]
    assert changed["state"] == "pledged" and changed["amount_cents"] == 7_500
    rows = log(con, made["id"])
    assert len(rows) == 2
    assert {(a["source"], a["actor"]) for a in rows} == {("claude", "claude")}


def test_upsert_budget_item_tool_refusals(client, con):
    ok = {"label": "X", "kind": "cost", "amount_cents": 1}
    reply = call(client, "upsert_budget_item", ok, headers=READ)
    assert reply["error"]["code"] == -32001
    for args in (
        {"label": "X", "kind": "cost", "amount_cents": -1},
        {"label": "X", "kind": "cost", "amount_cents": 1.5},
        {"label": "X", "kind": "cost"},  # create needs an amount
        {"label": "X", "kind": "gift", "amount_cents": 1},
        {**ok, "source": "web"},
        {"id": 99, "label": "X"},
    ):
        reply = call(client, "upsert_budget_item", args)
        assert "error" in reply or reply["result"]["isError"] is True, args
    assert budget.list(con) == []


# --- digest --------------------------------------------------------------------------------


def test_digest_budget_only_when_items_exist(con, cfg):
    assert "budget" not in views.build_digest(con, cfg)
    item = add(con, "Tent", "cost", 4_000)
    event.update(con, WEB, {"net_goal_cents": 10_000})
    digest = views.build_digest(con, cfg)
    assert digest["budget"] == {**budget.summary(con), "currency": "USD"}
    json.dumps(digest)
    budget.delete(con, WEB, item["id"])
    assert "budget" not in views.build_digest(con, cfg)


def test_digest_uses_configured_currency(con, tmp_path):
    cfg = make_config(tmp_path, currency="EUR")
    add(con, "Tent", "cost", 1)
    assert views.build_digest(con, cfg)["budget"]["currency"] == "EUR"


# --- CSV export ----------------------------------------------------------------------------

BUDGET_COLUMNS = [
    "id",
    "workstream_id",
    "label",
    "kind",
    "amount_cents",
    "state",
    "notes",
    "created_at",
    "updated_at",
]


def parse(body: bytes) -> list[dict[str, str]]:
    header, *rows = list(csv.reader(io.StringIO(body.decode("utf-8"), newline="")))
    assert header == BUDGET_COLUMNS
    return [dict(zip(header, row, strict=True)) for row in rows]


def test_budget_csv_export(api, con, ws):
    add(con, "Example Tent Co.", "cost", 12_345, "pledged", workstream_id=ws, notes="deposit paid")
    gone = add(con, "Dropped", "cost", 1)
    budget.delete(con, WEB, gone["id"])
    r = api.call("GET", "/api/export/budget.csv")
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "text/csv; charset=utf-8"
    assert r.headers["Content-Disposition"] == 'attachment; filename="budget.csv"'
    assert r.headers["Cache-Control"] == "no-store"
    (row,) = parse(r.get_data())
    assert row["label"] == "Example Tent Co." and row["kind"] == "cost"
    assert row["amount_cents"] == "12345" and row["state"] == "pledged"
    assert row["workstream_id"] == str(ws) and row["notes"] == "deposit paid"


@pytest.mark.parametrize("payload", ["=HYPERLINK(1)", "+1+1", "-2+3", "@SUM(A1)", "\tTAB", "\rCR"])
def test_budget_csv_injection_escaped(api, con, payload):
    # Notes are kept verbatim; the label is trimmed, so only the printable payloads reach it.
    add(con, "Row", "cost", 1, notes=payload)
    if payload[0] not in "\t\r":
        add(con, payload, "revenue", 2)
    rows = parse(api.call("GET", "/api/export/budget.csv").get_data())
    assert rows[0]["notes"] == "'" + payload
    if payload[0] not in "\t\r":
        assert "'" + payload in [r["label"] for r in rows]
    assert rows[0]["amount_cents"] == "1"  # numbers are untouched


def test_budget_export_is_web_only(api):
    for secret in (WRITE_SECRET, READ_SECRET):
        assert api.token("GET", "/mcp/api/export/budget.csv", secret).status_code == 404
    assert api.client.get("/api/export/budget.csv").status_code == 403


# --- config: currency ----------------------------------------------------------------------


def toml(tmp_path, extra=""):
    path = tmp_path / "config.toml"
    path.write_text(
        '[event]\nname = "Example Fall Festival"\ndate = 2099-10-10\ntimezone = "UTC"\n'
        f"{extra}\n"
        '[[organizers]]\nemail = "organizer-a@example.com"\ndisplay_name = "Organizer A"\n'
    )
    return path


def test_currency_defaults_to_usd(tmp_path):
    assert load_config(toml(tmp_path), env={}).currency == "USD"
    assert load_config(EXAMPLE, env={}).currency == "USD"
    assert 'currency = "USD"' in EXAMPLE.read_text()


def test_currency_is_validated(tmp_path):
    assert load_config(toml(tmp_path, 'currency = "eur"'), env={}).currency == "EUR"
    assert load_config(toml(tmp_path, 'currency = "JPY"'), env={}).currency == "JPY"
    for bad in ('"US"', '"USDX"', '"U$D"', '"12A"', '""', '"US D"', "5", "true"):
        with pytest.raises(ConfigError, match="currency"):
            load_config(toml(tmp_path, f"currency = {bad}"), env={})
