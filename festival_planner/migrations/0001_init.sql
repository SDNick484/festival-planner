-- Schema v1 (spec section 3). Organizers live in config, so owners are stored as the
-- organizer's email address (the service layer validates it). Email capture arrives in 0002.
-- Timestamps are UTC ISO-8601; the service layer sets them, the defaults are a safety net.

CREATE TABLE event (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL,
    event_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    goal_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE workstream (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    owner TEXT,
    color TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at TEXT
);

CREATE TABLE milestone (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    days_before_event INTEGER NOT NULL CHECK (days_before_event >= 0),
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at TEXT
);

CREATE TABLE task (
    id INTEGER PRIMARY KEY,
    workstream_id INTEGER NOT NULL REFERENCES workstream (id),
    title TEXT NOT NULL,
    owner TEXT,
    status TEXT NOT NULL DEFAULT 'todo'
        CHECK (status IN ('todo', 'doing', 'waiting', 'blocked', 'done')),
    due_date TEXT,
    milestone_id INTEGER REFERENCES milestone (id),
    waiting_on TEXT,
    blocked_reason TEXT,
    notes TEXT NOT NULL DEFAULT '',
    links TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(links)),
    done_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at TEXT,
    CHECK (status != 'blocked' OR blocked_reason IS NOT NULL)
);

CREATE INDEX task_workstream_id ON task (workstream_id);
CREATE INDEX task_due_date ON task (due_date);

CREATE TABLE contact (
    id INTEGER PRIMARY KEY,
    workstream_id INTEGER REFERENCES workstream (id),
    name TEXT NOT NULL,
    organization TEXT,
    role TEXT,
    emails TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(emails)),
    phone TEXT,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at TEXT
);

CREATE INDEX contact_workstream_id ON contact (workstream_id);

-- Append-only: the triggers below reject any UPDATE or DELETE. `source` is `web`, `email`,
-- `system` or a configured token name; it is not constrained here because token names are config.
CREATE TABLE activity (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    source TEXT NOT NULL,
    actor TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    diff TEXT CHECK (diff IS NULL OR json_valid(diff)),
    note TEXT
);

CREATE INDEX activity_entity ON activity (entity_type, entity_id);
CREATE INDEX activity_at ON activity (at);

CREATE TRIGGER activity_no_update BEFORE UPDATE ON activity
BEGIN
    SELECT RAISE(ABORT, 'activity is append-only');
END;

CREATE TRIGGER activity_no_delete BEFORE DELETE ON activity
BEGIN
    SELECT RAISE(ABORT, 'activity is append-only');
END;
