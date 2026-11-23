import pytest

from festival_planner import db
from festival_planner.service import (
    Actor,
    NotFound,
    ValidationError,
    activity,
    contacts,
    event,
    milestones,
    tasks,
    workstreams,
)

ORGANIZERS = ("organizer-a@example.com", "organizer-b@example.com")
WEB = Actor("web", "organizer-a@example.com")


@pytest.fixture
def con(tmp_path):
    con = db.connect(tmp_path / "test.db")
    db.migrate(con)
    yield con
    con.close()


def log(con, entity_type, entity_id):
    return activity.list_activity(con, entity_type=entity_type, entity_id=entity_id)


# --- event ---------------------------------------------------------------------------------


@pytest.fixture
def with_event(con):
    with con:  # the first-run bootstrap (a later task) creates this row
        con.execute(
            "INSERT INTO event (id, name, event_date, timezone)"
            " VALUES (1, 'Example Fall Festival', '2099-10-10', 'UTC')"
        )


def test_event_create_once(con):
    data = {"name": "Example Fall Festival", "event_date": "2099-10-10", "timezone": "UTC"}
    ev = event.create(con, WEB, data)
    assert ev["name"] == "Example Fall Festival"
    assert ev["goal_note"] == ""
    assert event.get(con) == ev
    entry = log(con, "event", 1)[0]
    assert entry["action"] == "create"
    assert entry["diff"] == {
        "name": [None, "Example Fall Festival"],
        "event_date": [None, "2099-10-10"],
        "timezone": [None, "UTC"],
    }
    with pytest.raises(ValidationError):  # a second event row is refused
        event.create(con, WEB, data)
    assert len(log(con, "event", 1)) == 1


def test_event_create_validates(con):
    with pytest.raises(ValidationError) as exc:
        event.create(
            con, WEB, {"name": "Example", "event_date": "soon", "timezone": "Nowhere/City"}
        )
    assert set(exc.value.errors) == {"event_date", "timezone"}
    with pytest.raises(ValidationError):
        event.create(con, WEB, {"name": "Example", "event_date": "2099-10-10"})


def test_event_get_missing_raises(con):
    with pytest.raises(NotFound):
        event.get(con)


def test_event_update_logs_diff(con, with_event):
    ev = event.update(con, WEB, {"event_date": "2099-10-17", "goal_note": "Have fun"})
    assert ev["event_date"] == "2099-10-17"
    assert event.get(con) == ev
    entry = log(con, "event", 1)[0]
    assert entry["action"] == "update"
    assert entry["diff"] == {
        "event_date": ["2099-10-10", "2099-10-17"],
        "goal_note": ["", "Have fun"],
    }
    event.update(con, WEB, {"name": "Example Fall Festival"})
    assert len(log(con, "event", 1)) == 1  # no-op


def test_event_validation(con, with_event):
    for changes in (
        {"timezone": "Mars/Base"},
        {"event_date": "soon"},
        {"name": ""},
        {"id": 2},
        {"goal_note": "x" * 20_001},
    ):
        with pytest.raises(ValidationError):
            event.update(con, WEB, changes)
    assert event.update(con, WEB, {"timezone": "Europe/Berlin"})["timezone"] == "Europe/Berlin"


# --- workstreams ---------------------------------------------------------------------------


def test_workstream_crud(con):
    ws = workstreams.create(
        con,
        WEB,
        {"name": "Vendors", "owner": ORGANIZERS[0], "color": "#aa3300", "sort_order": 2},
        organizers=ORGANIZERS,
    )
    assert ws["archived"] is False
    assert workstreams.get(con, ws["id"]) == ws
    updated = workstreams.update(con, WEB, ws["id"], {"archived": True, "name": "Vendor row"})
    assert updated["archived"] is True
    assert log(con, "workstream", ws["id"])[0]["diff"] == {
        "archived": [False, True],
        "name": ["Vendors", "Vendor row"],
    }
    other = workstreams.create(con, WEB, {"name": "Marketing", "sort_order": 1})
    assert [w["id"] for w in workstreams.list(con)] == [other["id"], ws["id"]]
    workstreams.delete(con, WEB, other["id"])
    assert [w["id"] for w in workstreams.list(con)] == [ws["id"]]
    assert log(con, "workstream", other["id"])[0]["action"] == "delete"


def test_workstream_validation(con):
    for data in (
        {},
        {"name": "x" * 201},
        {"name": "A", "color": "red; background: url(x)"},
        {"name": "A", "archived": 1},
        {"name": "A", "sort_order": 1.5},
        {"name": "A", "owner": "stranger@example.net"},
        {"name": "A", "bogus": True},
    ):
        with pytest.raises(ValidationError):
            workstreams.create(con, WEB, data, organizers=ORGANIZERS)


def test_workstream_with_live_tasks_cannot_be_deleted(con):
    ws = workstreams.create(con, WEB, {"name": "Vendors"})
    task = tasks.create(con, WEB, {"workstream_id": ws["id"], "title": "Book tent"})
    with pytest.raises(ValidationError):
        workstreams.delete(con, WEB, ws["id"])
    tasks.delete(con, WEB, task["id"])
    workstreams.delete(con, WEB, ws["id"])
    with pytest.raises(NotFound):
        workstreams.get(con, ws["id"])
    with pytest.raises(ValidationError):  # a deleted workstream takes no new tasks
        tasks.create(con, WEB, {"workstream_id": ws["id"], "title": "Late"})


# --- milestones ----------------------------------------------------------------------------


def test_milestone_crud(con):
    m = milestones.create(con, WEB, {"name": "T-14", "days_before_event": 14})
    assert m["notes"] == ""
    assert log(con, "milestone", m["id"])[0]["action"] == "create"
    m2 = milestones.update(con, WEB, m["id"], {"days_before_event": 21, "name": "T-21"})
    assert m2["days_before_event"] == 21
    early = milestones.create(con, WEB, {"name": "T-60", "days_before_event": 60})
    assert [x["id"] for x in milestones.list(con)] == [early["id"], m["id"]]  # earliest first
    for bad in ({"days_before_event": -1}, {"days_before_event": "3"}, {"name": ""}):
        with pytest.raises(ValidationError):
            milestones.update(con, WEB, m["id"], bad)
    with pytest.raises(ValidationError):
        milestones.create(con, WEB, {"name": "No days"})
    ws = workstreams.create(con, WEB, {"name": "Vendors"})
    t = tasks.create(con, WEB, {"workstream_id": ws["id"], "title": "x", "milestone_id": m["id"]})
    with pytest.raises(ValidationError):
        milestones.delete(con, WEB, m["id"])
    tasks.update(con, WEB, t["id"], {"milestone_id": None})
    milestones.delete(con, WEB, m["id"])
    assert [x["id"] for x in milestones.list(con)] == [early["id"]]


# --- contacts ------------------------------------------------------------------------------


def test_contact_crud(con):
    ws = workstreams.create(con, WEB, {"name": "Vendors"})
    c = contacts.create(
        con,
        WEB,
        {
            "name": "Pat Example",
            "organization": "Example Tent Co.",
            "role": "Sales",
            "emails": ["sales@example.com"],
            "workstream_id": ws["id"],
        },
    )
    assert c["emails"] == ["sales@example.com"]
    c2 = contacts.update(con, WEB, c["id"], {"emails": ["sales@example.com", "pat@example.org"]})
    assert log(con, "contact", c["id"])[0]["diff"] == {
        "emails": [["sales@example.com"], ["sales@example.com", "pat@example.org"]]
    }
    assert c2["emails"][1] == "pat@example.org"
    other = contacts.create(con, WEB, {"name": "Lee Example"})
    assert [x["id"] for x in contacts.list(con, workstream_id=ws["id"])] == [c["id"]]
    assert {x["id"] for x in contacts.list(con)} == {c["id"], other["id"]}
    contacts.delete(con, WEB, other["id"])
    assert [x["id"] for x in contacts.list(con)] == [c["id"]]


def test_contact_validation(con):
    for data in (
        {"name": "A", "emails": "a@example.com"},
        {"name": "A", "emails": ["not an email"]},
        {"name": "A", "emails": ["a@example.com"] * 21},
        {"name": "A", "workstream_id": 999},
        {"name": "A", "phone": "1" * 51},
        {"name": ""},
        {"name": "A", "unknown": 1},
    ):
        with pytest.raises(ValidationError):
            contacts.create(con, WEB, data)
