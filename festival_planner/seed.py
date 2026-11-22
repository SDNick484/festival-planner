"""First-run bootstrap and fictional demo data.

`bootstrap` creates the event, workstreams and milestones from config the first time the app
starts; it is a no-op once the event exists. `seed_demo` (`make seed-demo`) adds fictional tasks and
contacts for development. Every write goes through the service layer as the system actor.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

from festival_planner import clock, db
from festival_planner.config import Config, load_config
from festival_planner.service import Actor, contacts, event, milestones, tasks, workstreams
from festival_planner.service._common import transaction

SYSTEM = Actor("system", "system")

# (title, owner index or None, days from today or None, status, milestone index or None)
_DEMO_TASKS = (
    ("Book tent rental for the main field", 0, 14, "todo", 1),
    ("Confirm generator delivery window", 1, 5, "doing", None),
    ("Draft volunteer shift sheet", 0, 9, "todo", 2),
    ("Order display stands for the silent auction", None, 21, "done", None),
    ("Sign sponsorship thank-you letters", 1, -3, "waiting", 3),
    ("Print wayfinding signs", 0, 30, "todo", 4),
    ("Reserve training room for ride operators", 1, 2, "blocked", None),
    ("Collect item donations for the auction", 0, 45, "doing", 0),
    ("Set up volunteer check-in form", 1, 7, "todo", None),
    ("Email the vendor map draft", 0, 0, "waiting", 5),
    ("Review insurance certificate", 1, -1, "todo", 2),
    ("Pick up cash box from storage", 0, 1, "todo", 6),
    ("Schedule walkthrough of the field", 1, 60, "done", 0),
    ("Post event date on the community board", 0, 40, "doing", None),
    ("Reconcile ticket sales spreadsheet", 1, None, "todo", None),
    ("Recruit parking marshals", 0, 12, "blocked", 3),
    ("Order bottled water for volunteers", 1, 8, "todo", 4),
    ("Confirm power hookups for food trucks", 0, 3, "doing", 5),
    ("Approve banner artwork", 1, -6, "done", 6),
    ("Update Example Tent Co. quote", None, 18, "waiting", None),
)

_BLOCKED_REASON = "Waiting on the venue to confirm the permit"
_WAITING_ON = "Example Print Shop proof"

_DEMO_CONTACTS = (
    {
        "name": "Example Tent Co.",
        "organization": "Example Tent Co.",
        "role": "Tent rental",
        "emails": ["orders@example.com"],
        "phone": None,
        "notes": "Fictional demo contact.",
    },
    {
        "name": "Example Rides LLC",
        "organization": "Example Rides LLC",
        "role": "Ride operator",
        "emails": ["bookings@example.org"],
        "phone": None,
        "notes": "Fictional demo contact.",
    },
    {
        "name": "Example Print Shop",
        "organization": "Example Print Shop",
        "role": "Printing",
        "emails": ["proofs@example.net"],
        "phone": None,
        "notes": "Fictional demo contact.",
    },
)


def _organizer_emails(cfg: Config) -> list[str]:
    return [o.email for o in cfg.organizers]


def bootstrap(con: sqlite3.Connection, cfg: Config) -> bool:
    """Create the event, workstreams and milestones from config if there is no event yet.

    Returns True when it created them, False when the event already existed. Runs in one
    transaction, so two processes starting together cannot both seed.
    """
    with transaction(con):
        if con.execute("SELECT 1 FROM event WHERE id = 1").fetchone():
            return False
        event.create(
            con,
            SYSTEM,
            {
                "name": cfg.event_name,
                "event_date": cfg.event_date.isoformat(),
                "timezone": cfg.timezone.key,
            },
        )
        for position, name in enumerate(cfg.seed_workstreams):
            workstreams.create(
                con,
                SYSTEM,
                {"name": name, "sort_order": position},
                organizers=_organizer_emails(cfg),
            )
        for name, days_before in cfg.seed_milestones:
            milestones.create(con, SYSTEM, {"name": name, "days_before_event": days_before})
        return True


def seed_demo(con: sqlite3.Connection, cfg: Config) -> None:
    """Add fictional tasks (all statuses) and contacts. Development only; no-op if tasks exist."""
    bootstrap(con, cfg)
    if tasks.list(con, limit=1):
        return
    today = clock.today(cfg)
    spaces = workstreams.list(con)
    stones = milestones.list(con)
    organizers = _organizer_emails(cfg)
    for position, (title, owner_index, offset, status, milestone_index) in enumerate(_DEMO_TASKS):
        data: dict = {
            "workstream_id": spaces[position % len(spaces)]["id"],
            "title": title,
            "status": status,
            "due_date": None if offset is None else (today + timedelta(days=offset)).isoformat(),
            "milestone_id": None
            if milestone_index is None or milestone_index >= len(stones)
            else stones[milestone_index]["id"],
            "owner": None if owner_index is None else organizers[owner_index % len(organizers)],
            "notes": "Fictional demo task.",
            "links": [{"label": "Example page", "url": "https://example.com/festival"}]
            if position % 4 == 0
            else [],
        }
        if status == "blocked":
            data["blocked_reason"] = _BLOCKED_REASON
        if status == "waiting":
            data["waiting_on"] = _WAITING_ON
        tasks.create(con, SYSTEM, data, organizers=organizers)
    for contact in _DEMO_CONTACTS:
        contacts.create(con, SYSTEM, contact)


def main() -> None:
    """`python -m festival_planner.seed`: migrate, then add the demo data (make seed-demo)."""
    cfg = load_config()
    con = db.connect(Path(cfg.data_dir) / db.DB_FILENAME)
    try:
        db.migrate(con)
        seed_demo(con, cfg)
    finally:
        con.close()


if __name__ == "__main__":
    main()
