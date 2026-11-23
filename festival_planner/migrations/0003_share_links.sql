-- Schema v3: volunteer read-only share links (Task 24).
-- Only the SHA-256 of a link's token is stored (hex, looked up by equality); the raw token is
-- shown once, when the link is created. `workstream_id` NULL means every workstream.
-- `expires_at` and `revoked_at` are UTC ISO-8601 like every other timestamp.

CREATE TABLE share_link (
    id INTEGER PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
    label TEXT NOT NULL,
    workstream_id INTEGER REFERENCES workstream (id),
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
