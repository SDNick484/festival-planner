-- Schema v2: email capture (spec section 3, "EmailItem") and the poller's bookkeeping.
-- List-valued columns (addresses, attachment names) are JSON text, like `task.links`.
-- `captured_by` is the organizer's email address (organizers live in config), or NULL.
-- `body_text` and `body_purged_at` support the retention purge: metadata outlives the body.

CREATE TABLE email_item (
    id INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL,
    thread_key TEXT NOT NULL,
    from_addr TEXT NOT NULL DEFAULT '',
    to_addrs TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(to_addrs)),
    cc_addrs TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(cc_addrs)),
    subject TEXT NOT NULL DEFAULT '',
    sent_at TEXT,
    body_text TEXT,
    attachment_names TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(attachment_names)),
    captured_by TEXT,
    forward_note TEXT,
    state TEXT NOT NULL DEFAULT 'inbox' CHECK (state IN ('inbox', 'filed', 'dismissed')),
    task_id INTEGER REFERENCES task (id),
    suggested_workstream_id INTEGER REFERENCES workstream (id),
    body_purged_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (message_id)
);

CREATE INDEX email_item_state ON email_item (state);
CREATE INDEX email_item_thread_key ON email_item (thread_key);
CREATE INDEX email_item_task_id ON email_item (task_id);

-- One row per mailbox label the poller reads: the UIDVALIDITY it saw and the last UID handled.
CREATE TABLE imap_state (
    label TEXT PRIMARY KEY,
    uidvalidity INTEGER,
    last_uid INTEGER
);

-- Small key/value store (for example `last_email_poll`, shown by /healthz).
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
