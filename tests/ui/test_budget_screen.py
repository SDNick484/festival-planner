"""Budget screen (Task 22): the summary bar, items grouped by workstream, adding, editing and
deleting items, setting the goal, and the link from Home.

The live server's database is shared by the session, so everything a test creates is deleted
again and the goal is cleared (the `budget` fixture). Every figure is invented. After any
navigation, click or save the checks use Playwright's auto-retrying `expect`, never a read that
the previous state could satisfy, and there are no sleeps.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect


def api_send(page, base_url, method, path, body=None):
    res = page.request.fetch(
        base_url + path, method=method, data=body, headers={"Origin": base_url}
    )
    assert res.ok, (method, path, res.status, res.text())
    return None if res.status == 204 else res.json()


@pytest.fixture
def budget(page, base_url):
    """A workstream with two items and a goal; everything is removed afterwards."""
    ws = api_send(page, base_url, "POST", "/api/workstreams", {"name": "Budget UI Test"})
    sponsor = api_send(
        page,
        base_url,
        "POST",
        "/api/budget/items",
        {
            "label": "Example sponsor",
            "kind": "revenue",
            "amount_cents": 150_000,
            "state": "pledged",
            "workstream_id": ws["id"],
        },
    )
    tent = api_send(
        page,
        base_url,
        "POST",
        "/api/budget/items",
        {
            "label": "Example Tent Co.",
            "kind": "cost",
            "amount_cents": 40_000,
            "workstream_id": ws["id"],
        },
    )
    api_send(page, base_url, "PUT", "/api/event", {"net_goal_cents": 200_000})
    yield {"ws": ws, "sponsor": sponsor, "tent": tent}
    api_send(page, base_url, "PUT", "/api/event", {"net_goal_cents": None})
    for item in api_send(page, base_url, "GET", "/api/budget")["items"]:
        api_send(page, base_url, "DELETE", f"/api/budget/items/{item['id']}")
    api_send(page, base_url, "DELETE", f"/api/workstreams/{ws['id']}")


def test_ui_budget_bar(page, base_url, budget):
    page.goto(base_url + "/#/budget")
    view = page.locator("[data-view='budget']")
    summary = view.locator("[data-section='summary']")
    bar = summary.locator("progress")
    # Projected net 1,500.00 - 400.00 = 1,100.00 against a goal of 2,000.00 is 55 percent.
    expect(summary.locator("[data-role='net']")).to_have_text("$1,100.00")
    expect(summary.locator("[data-role='goal']")).to_have_text("$2,000.00")
    expect(bar).to_have_attribute("value", "55")
    expect(summary.locator("[data-role='pct']")).to_have_text("55% of goal")
    expect(summary.locator("[data-role='actual']")).to_have_text("$0.00")

    # Items sit under their workstream.
    group = view.locator("[data-group]", has=page.locator("h3", has_text="Budget UI Test"))
    expect(group.locator("[data-item]")).to_have_count(2)
    expect(group.locator("[data-item]", has_text="Example Tent Co.")).to_contain_text("$400.00")
    expect(group.locator("[data-item]", has_text="Example sponsor")).to_contain_text("pledged")
    expect(group.locator("[data-item]", has_text="Example Tent Co.")).to_contain_text("estimated")


def test_add_item_updates_summary(page, base_url, budget):
    page.goto(base_url + "/#/budget")
    view = page.locator("[data-view='budget']")
    summary = view.locator("[data-section='summary']")
    expect(summary.locator("[data-role='net']")).to_have_text("$1,100.00")

    form = view.locator("form[data-form='add-item']")
    form.locator("[name='label']").fill("Example raffle")
    form.locator("[name='kind']").select_option("revenue")
    form.locator("[name='amount']").fill("900.50")
    form.locator("[name='state']").select_option("received")
    form.locator("[name='workstream_id']").select_option(label="Budget UI Test")
    form.get_by_role("button", name="Add item").click()

    expect(view.locator("[data-item]", has_text="Example raffle")).to_be_visible()
    expect(summary.locator("[data-role='net']")).to_have_text("$2,000.50")
    expect(summary.locator("[data-role='actual']")).to_have_text("$900.50")
    expect(summary.locator("progress")).to_have_attribute("value", "100")
    expect(form.locator("[name='label']")).to_have_value("")


def test_cost_states_are_labelled_estimated_committed_paid(page, base_url, budget):
    page.goto(base_url + "/#/budget")
    form = page.locator("[data-view='budget'] form[data-form='add-item']")
    form.locator("[name='kind']").select_option("cost")
    expect(form.locator("[name='state'] option")).to_have_text(["Estimated", "Committed", "Paid"])
    form.locator("[name='kind']").select_option("revenue")
    expect(form.locator("[name='state'] option")).to_have_text(["Estimated", "Pledged", "Received"])
    # The value sent is always estimated, pledged or received.
    form.locator("[name='kind']").select_option("cost")
    form.locator("[name='label']").fill("Example printing")
    form.locator("[name='amount']").fill("25")
    form.locator("[name='state']").select_option(label="Paid")
    form.get_by_role("button", name="Add item").click()
    row = page.locator("[data-item]", has_text="Example printing")
    expect(row).to_contain_text("paid")
    expect(page.locator("[data-section='summary'] [data-role='actual']")).to_have_text("-$25.00")
    items = api_send(page, base_url, "GET", "/api/budget")["items"]
    assert [i["state"] for i in items if i["label"] == "Example printing"] == ["received"]


def test_bad_amount_is_rejected_without_losing_input(page, base_url, budget):
    page.goto(base_url + "/#/budget")
    view = page.locator("[data-view='budget']")
    form = view.locator("form[data-form='add-item']")
    form.locator("[name='label']").fill("Example bad")
    problems = {
        "-5": "Amounts cannot be negative.",
        "12.345": "Use at most two decimal places.",
        "abc": "Enter an amount such as 12.50.",
        "": "Enter an amount.",
    }
    for bad, words in problems.items():
        form.locator("[name='amount']").fill(bad)
        form.get_by_role("button", name="Add item").click()
        expect(form.locator("[data-field='amount'] .mg-error")).to_have_text(words)
        expect(form.locator("[name='label']")).to_have_value("Example bad")
    expect(view.locator("[data-item]", has_text="Example bad")).to_have_count(0)


def test_edit_and_delete_item(page, base_url, budget):
    page.goto(base_url + "/#/budget")
    view = page.locator("[data-view='budget']")
    summary = view.locator("[data-section='summary']")
    item = view.locator("[data-item]", has_text="Example Tent Co.")
    item.locator("summary").click()
    form = item.locator("form")
    form.locator("[name='amount']").fill("100")
    form.get_by_role("button", name="Save").click()
    expect(summary.locator("[data-role='net']")).to_have_text("$1,400.00")
    expect(view.locator("[data-item]", has_text="Example Tent Co.")).to_contain_text("$100.00")

    item = view.locator("[data-item]", has_text="Example Tent Co.")
    item.locator("summary").click()
    item.get_by_role("button", name="Delete").click()
    item.get_by_role("button", name="Confirm delete").click()
    expect(view.locator("[data-item]", has_text="Example Tent Co.")).to_have_count(0)
    expect(summary.locator("[data-role='net']")).to_have_text("$1,500.00")


def test_set_and_clear_goal(page, base_url, budget):
    page.goto(base_url + "/#/budget")
    view = page.locator("[data-view='budget']")
    summary = view.locator("[data-section='summary']")
    form = view.locator("form[data-form='goal']")
    form.locator("[name='goal']").fill("4400")
    form.get_by_role("button", name="Save goal").click()
    expect(summary.locator("[data-role='goal']")).to_have_text("$4,400.00")
    expect(summary.locator("progress")).to_have_attribute("value", "25")

    form.locator("[name='goal']").fill("")
    form.get_by_role("button", name="Save goal").click()
    expect(summary.locator("[data-role='goal']")).to_have_text("No goal set")
    expect(summary.locator("progress")).to_have_count(0)
    expect(summary.locator("[data-role='net']")).to_have_text("$1,100.00")


def test_hostile_label_is_text(page, base_url, budget):
    hostile = "<img src=x onerror=alert(1)>"
    api_send(
        page,
        base_url,
        "POST",
        "/api/budget/items",
        {"label": hostile, "kind": "cost", "amount_cents": 1, "workstream_id": budget["ws"]["id"]},
    )
    page.goto(base_url + "/#/budget")
    expect(page.locator("[data-item]", has_text=hostile)).to_be_visible()
    expect(page.locator("[data-view='budget'] img")).to_have_count(0)


def test_budget_link_on_home(page, base_url, budget):
    page.goto(base_url + "/")
    header = page.locator("[data-section='header']")
    expect(header.get_by_role("link", name="Manage")).to_be_visible()
    header.get_by_role("link", name="Budget").click()
    expect(page.locator("[data-view='budget']")).to_be_visible()
    expect(page).to_have_url(base_url + "/#/budget")
    expect(
        page.locator("[data-view='budget'] [data-section='summary'] [data-role='net']")
    ).to_have_text("$1,100.00")
