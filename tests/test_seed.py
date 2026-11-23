from pathlib import Path

import pytest
from leak_check import main as leak_check_main

from festival_planner import db, seed
from festival_planner.config import load_config
from festival_planner.service import workstreams

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = ROOT / "config.example.toml"


@pytest.fixture
def cfg():
    return load_config(EXAMPLE_CONFIG, env={})


@pytest.fixture
def con(tmp_path):
    con = db.connect(tmp_path / "seed.db")
    db.migrate(con)
    yield con
    con.close()


def _count(con, table):
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608 (fixed names)


def test_bootstrap_once(con, cfg):
    assert seed.bootstrap(con, cfg) is True
    before = {t: _count(con, t) for t in ("event", "workstream", "milestone")}
    assert before == {"event": 1, "workstream": 7, "milestone": 7}

    assert seed.bootstrap(con, cfg) is False
    assert {t: _count(con, t) for t in before} == before


def test_bootstrap_from_example_config(con, cfg):
    seed.bootstrap(con, cfg)
    names = [w["name"] for w in workstreams.list(con)]
    assert len(names) == 7
    assert names == list(cfg.seed_workstreams)
    milestone_rows = con.execute(
        "SELECT name, days_before_event FROM milestone ORDER BY id"
    ).fetchall()
    assert [(r["name"], r["days_before_event"]) for r in milestone_rows] == list(
        cfg.seed_milestones
    )
    event_row = con.execute("SELECT name, event_date, timezone FROM event").fetchone()
    assert event_row["name"] == cfg.event_name
    assert event_row["event_date"] == cfg.event_date.isoformat()
    assert event_row["timezone"] == cfg.timezone.key


def test_seed_demo_is_fictional(con, cfg, tmp_path):
    seed.bootstrap(con, cfg)
    seed.seed_demo(con, cfg)

    assert _count(con, "task") >= 20
    statuses = {r[0] for r in con.execute("SELECT DISTINCT status FROM task")}
    assert statuses == {"todo", "doing", "waiting", "blocked", "done"}
    assert _count(con, "contact") == 3

    dump = tmp_path / "seeded-dump.sql"
    dump.write_text("\n".join(con.iterdump()) + "\n", encoding="utf-8")
    assert leak_check_main([str(dump)]) == 0
