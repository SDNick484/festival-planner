-- Schema v4: budget tracker (Task 22).
-- Money is integer cents (hundredths of the configured currency unit), never a float.
-- `state` is estimated, pledged or received for both kinds; for a cost the UI labels the same
-- three values estimated, committed and paid. `event.net_goal_cents` is the net target (NULL =
-- no goal set).

CREATE TABLE budget_item (
    id INTEGER PRIMARY KEY,
    workstream_id INTEGER REFERENCES workstream (id),
    label TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('revenue', 'cost')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    state TEXT NOT NULL DEFAULT 'estimated' CHECK (state IN ('estimated', 'pledged', 'received')),
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at TEXT
);

CREATE INDEX budget_item_workstream_id ON budget_item (workstream_id);

ALTER TABLE event ADD COLUMN net_goal_cents INTEGER CHECK (net_goal_cents IS NULL OR net_goal_cents >= 0);
