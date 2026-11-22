# Festival Planner — Design

- **Status:** Awaiting approval
- **Scope of this document:** product and technical design for a small self-hosted planning tracker. No application code exists yet.

## 1. Intent

Two co-organizers (co-chairs) of a volunteer-run event need **one place where they can both see where planning stands**. Work happens across several channels: email (including from one organizer's organization account), Claude chats, and Claude Duet. It also happens across separate accounts. Nothing has gone wrong yet. The goal is a single shared picture: what's due, what's stuck, how each workstream is progressing, and how many days are left.

The repo is public and generic. Any volunteer group running any event should be able to deploy it with its own config. **No real names, organizations, addresses, dates, vendors, dollar figures, or domains appear anywhere in the repo** (see §9).

### Users

- **Organizer A:** technical. Works through the web UI, Claude (with a paid plan), and Claude Duet.
- **Organizer B:** non-technical and mostly on a phone. Coordinates by email and may use free Claude. **Nothing Organizer B needs may depend on Claude.** Their main input is forwarding or CC'ing email to a capture address. Their main output is the phone home screen.

Only these two people have access in the MVP. There are no in-app roles: both organizers have full read and write access.

### Success looks like

- Either organizer can open the home screen on a phone and, in one scroll, answer "what needs me?" and "where are we overall?"
- Organizer B gets an update into the system just by forwarding an email, optionally with a one-line note on top. Organizer A (or Claude/Duet) files it.
- Organizer A can ask Claude or Duet "what's overdue?" or "mark the tent quote as waiting on the vendor" and the app reflects it.
- Every change, from any channel, is visible in an activity log with its source and author.

### Goals

- Mobile-first, very simple web UI.
- Three input channels (web, email, Claude/Duet) writing into one model and one activity log.
- Countdown to the event, plus milestones defined relative to the event date.
- Generic and reusable, with all instance specifics kept in config.

### Non-goals

- Retrieval or vector search (RAG). The data is small and structured.
- Multiple events per deployment. One deployment serves one event.
- Real-time collaboration (live cursors, websockets).
- Native mobile apps (a PWA "add to home screen" is enough).
- Two-way sync with Google Sheets or Docs.

### Anti-goals (failure even if it "works")

- The app becomes a second job: a triage inbox nobody clears, or forms with many required fields.
- Organizer B has to learn a tool or change habits to keep the picture current.
- Anything identifying leaks into the public repo.
- Email contents from third parties are exposed beyond the two organizers and their own Claude clients.

## 2. Decisions

| Topic | Decision | Whose call |
|---|---|---|
| Primary input for Organizer B | Email forward/CC to a capture address; plus quick taps on the phone UI | Agreed |
| Google Sheet as interface | No | Agreed |
| Email capture mechanism | One Gmail account, plus-address (for example `capture+festival@example.com`), Gmail filter applies a label; the app polls that label | Agreed (Gmail + label); polling via IMAP is the designer's call |
| Email storage | **Full plain-text body** plus metadata; attachments stored as filenames only | Agreed (full body); filenames-only is the designer's call |
| Email → task matching | Triage inbox with suggestions; subject tag `[T-<id>]` auto-files as a bonus | Agreed |
| Who clears the inbox | Organizer A (or Claude/Duet); Organizer B's note above a forward is kept and shown | Agreed |
| Volunteer/read-only views | Not in MVP (later: share links that exclude email content) | Agreed |
| Contacts list | MVP | Agreed |
| Budget tracker | After integrations (Polish milestone) | Agreed to defer; the milestone placement is the designer's call |
| MCP | Embedded `/mcp` endpoint inside the app, modeled on the owner's other self-hosted app. Not a separate MCP server | Agreed |
| Machine auth | Named static tokens with per-token scope; URL-token form may carry write scope | Agreed |
| Home screen | Hybrid: attention list, then collapsed "due this week", then workstream progress bars | Agreed (mockup) |
| Workstream, task, inbox, timeline screens | As in §6 | Agreed (mockups) |
| Push notifications | Weekly digest email to both organizers | Agreed in principle; the form (weekly, email) is the designer's call |
| Stack | Flask + gunicorn, SQLite, vanilla JS PWA, Docker | Python is the owner's; the rest is the designer's call |

## 3. Domain model

All tables have `id` (integer PK), `created_at` and `updated_at` (UTC ISO-8601). Soft delete (`deleted_at`) applies to tasks, workstreams, milestones and contacts so the activity log never points at nothing.

- **Event** (exactly one row): `name`, `event_date` (local date), `timezone`, `goal_note` (free text). The row is created from config on first start and editable in the UI afterwards.
- **Organizer**: `email` (must match the Cloudflare Access identity), `aliases` (other addresses the person sends or forwards from, such as an organization account), `display_name`. These come from config and are not editable in the UI. Alias addresses are used when recognizing `captured_by` and forwarded senders, never for login. *(Designer's call.)*
- **Workstream**: `name`, `owner_id` (nullable organizer), `color`, `sort_order`, `archived`.
- **Task**: `workstream_id`, `title`, `owner_id` (nullable organizer = unassigned), `status`, `due_date` (nullable), `milestone_id` (nullable), `waiting_on` (text, nullable), `blocked_reason` (text, required when status=blocked), `notes` (markdown text), `links` (JSON array of `{label, url}`; `http`/`https` URLs only).
  - **Statuses:** `todo`, `doing`, `waiting`, `blocked`, `done`. `waiting` means the ball is in someone else's court; `blocked` means it can't proceed. The `done_at` timestamp is set and cleared automatically.
- **Milestone**: `name`, `days_before_event` (integer ≥ 0), `notes`. The date is always derived (`event_date − days_before_event`), so moving the event date moves every milestone.
- **Contact**: `workstream_id` (nullable), `name`, `organization`, `role`, `emails` (JSON array), `phone`, `notes`. Contacts are only for organizing; they are never exposed outside the app.
- **EmailItem**: `message_id` (unique, RFC 5322 Message-ID), `thread_key`, `from_addr`, `to_addrs`, `cc_addrs`, `subject`, `sent_at`, `body_text`, `attachment_names` (JSON), `captured_by` (organizer id if the forwarder or sender is an organizer, else null), `forward_note` (text the organizer typed above a forwarded message, nullable), `state` (`inbox`, `filed`, `dismissed`), `task_id` (nullable), `suggested_workstream_id` (nullable), `body_purged_at` (nullable).
- **ActivityLog** (append-only, never updated or deleted by the app): `at`, `source` (`web`, `email`, `claude`, `duet`, `system`, or any configured token name), `actor` (organizer email, token name, or `system`), `entity_type`, `entity_id`, `action` (`create`, `update`, `delete`, `note`, `file_email`, …), `diff` (JSON `{field: [before, after]}`), `note` (free text, used by "log activity").

**Derived views** are computed at query time, not stored:
- `effective_due`: `due_date` if set, otherwise the milestone's derived date if `milestone_id` is set, otherwise none. **Every derived view below uses `effective_due`.** An explicit `due_date` always wins. *(Designer's call.)*
- `overdue`: not done, `effective_due` before today, in the event timezone.
- `due_this_week`: not done, due within the next 7 days.
- `needs_attention`: overdue, plus blocked, plus waiting for more than N days (configurable, default 5). "Waiting since" is the last activity that set status=waiting.
- Workstream progress: done / (total − deleted).
- Countdown: days until `event_date`.

## 4. Channels

Every write goes through **one service layer**: functions like `update_task(actor, source, task_id, changes)`. That layer validates input, applies the change and writes the ActivityLog row in the same transaction. The HTTP routes, MCP tools and email poller are thin adapters over it. No adapter writes SQL directly.

### 4.1 Web

- Cloudflare Access sits in front of the whole hostname **except `/mcp/*` and `/healthz`**. Authentication is decided **by path**: `/api/*` and the UI accept only an Access JWT. Everything under `/mcp/*` (the MCP endpoint and the machine REST mirror at `/mcp/api/*`) accepts only tokens. *(Designer's call: one bypassed prefix keeps the Access policy a single rule, and a web route can never be reached with a token alone.)*
- The app independently verifies the `Cf-Access-Jwt-Assertion` header on every non-machine request:
  - RS256 signature against the team JWKS, with keys cached and refreshed on an unknown `kid` at most once per interval;
  - `aud` = configured AUD, `iss` = `https://<team-domain>`, `exp` and `iat` required, 30 s of clock skew allowed;
  - the email must be in the configured organizer list.
- If verification fails, the request gets 403 with a generic message, and the details go only to the server log.
- `source=web`, `actor=<access email>`.
- Writes also require a same-origin `Origin` header (CSRF defense).
- For local development only, an explicit `DEV_AUTH_EMAIL` env var bypasses Access. Startup refuses to run with it set unless `FLASK_ENV=development` and the bind address is loopback.

### 4.2 Email ingest

- **Transport:** IMAP over TLS to the configured mailbox, using an app password. The app opens the configured label read-only (`EXAMINE`) every `EMAIL_POLL_SECONDS` (default 300) and fetches only UIDs above the last seen UID for that label (stored in the DB, with UIDVALIDITY tracked). It never modifies the mailbox. The Gmail API with OAuth is a documented alternative and is not implemented in the first pass.
- **Parsing:** prefer the `text/plain` part. Otherwise convert HTML to text with a stdlib-based stripper; never render HTML. Keep the full plain-text body, record attachment filenames only, and enforce a maximum body size (default 200 KB, truncated with a marker).
- **Forwarded messages:** when an organizer forwards a message, `captured_by` = that organizer. The original sender is parsed from the forwarded header block when it is recognizable; otherwise the forwarder is the sender. Messages that merely CC the capture address are recorded as-is.
- **Forward note:** when an organizer forwards, any text they wrote above the forwarded-message marker is stored as `forward_note`. It is shown prominently on the inbox card and copied into the activity note when the email is filed.
- **Who triages:** Organizer A (directly or through Claude/Duet) clears the inbox. Organizer B only forwards and reads the home screen, so the inbox UI may be desktop-friendly as well as phone-usable, but it is not on B's critical path. *(Agreed.)*
- **Dedup:** by Message-ID. A message seen again is ignored.
- **Filing:**
  1. If the subject contains `[T-<id>]` and that task exists, auto-file to it (`state=filed`).
  2. Otherwise the message goes to the inbox (`state=inbox`) with `suggested_workstream_id`. The suggestion comes first from a contact whose email matches any participant, then from the workstream of the most recent filed email in the same thread, then from none.
  3. Inbox actions: file to an existing task, create a task (prefilled with the subject, in the suggested workstream) and file to it, or dismiss.
- **Activity:** each ingest logs `source=email`, `actor=<captured_by email or "external">`, `action=capture`. Each filing logs with whichever channel performed it.
- **Retention:** a daily job sets `body_text=NULL` and `body_purged_at` for every EmailItem when today > `event_date + EMAIL_RETENTION_DAYS` (default 30). Metadata is kept.
- **Failure handling:** IMAP errors are logged with credentials redacted and retried with backoff. `/healthz` reports the time of the last successful poll so a monitor can alert.

### 4.3 Claude and Duet: the `/mcp` endpoint

- **Shape:** minimal stateless JSON-RPC over HTTP POST, compatible with MCP streamable HTTP. There are no SSE sessions. It supports `initialize`, `tools/list`, `tools/call` and `ping`, accepts protocol versions 2025-06-18, 2025-03-26 and 2024-11-05, and limits batches to 20. GET and DELETE return 405.
- **Tokens:** configured as `MCP_TOKENS="name:scope:secret,..."` where scope ∈ {`read`, `write`}. Several entries may share a name to allow rotation. Comparison uses `hmac.compare_digest` against every candidate.
  - The token is accepted either as `Authorization: Bearer <secret>` or in the URL path `/mcp/<secret>`, because hosted connectors often cannot set headers.
  - A WSGI middleware moves a path token into the environ and rewrites the path before any logging, so tokens never appear in access logs.
  - The **token name is the activity `source` and `actor`** (for example `claude`, `duet`), so the client cannot spoof provenance.
  - A missing or bad token returns 401 for a header token or 404 for a path token. There is no anonymous access.
- **Tools.** Write tools are refused with a JSON-RPC error when scope=`read`.

| Tool | Scope | Purpose |
|---|---|---|
| `get_digest` | read | Countdown, needs-attention list, due this week, inbox count, per-workstream progress. One call answers "what needs attention?" |
| `list_workstreams` | read | With progress counts |
| `list_tasks` | read | Filters: workstream, status, owner, due_before, overdue, text query; paginated |
| `get_task` | read | Includes linked emails (bodies included) and recent activity |
| `list_inbox` | read | Untriaged EmailItems with suggestions |
| `get_email` | read | Full EmailItem |
| `list_milestones` | read | With derived dates and attached task counts |
| `list_contacts` | read | Optional workstream filter |
| `get_activity` | read | Recent log entries; filters: since, entity, source |
| `create_task` | write | |
| `update_task` | write | Partial update; validates status transitions (for example, blocked requires a reason) |
| `log_note` | write | Adds a note activity on a task or workstream ("called vendor, confirmed") |
| `file_email` | write | To an existing task, or creates a task from it |
| `dismiss_email` | write | |
| `upsert_contact` | write | |

- **Instructions string:** tells the model what the app is, that dates are in the event timezone, and that it should call `get_digest` first when asked for status.
- **Duet:** this design assumes Duet connects to the same remote MCP URL with its own `duet` token. If Duet can only make plain HTTP calls, the REST API (§5) is mirrored at `/mcp/api/*` with the same tokens, and `GET /mcp/api/digest` returns the same payload as `get_digest`. *Open item for the builder: confirm how Duet connects during the MCP issue; neither path requires a design change.*

### 4.4 Weekly digest email (Integrations)

On a configured weekday and hour (event timezone), the app sends a plain-text email to both organizers containing the `get_digest` content with deep links. It sends over SMTP with the same account and app password. Before the event it sends weekly; in the final 14 days it may send twice weekly if `DIGEST_FINAL_STRETCH=1` (off by default). Sending can be turned off entirely.

## 5. HTTP API

JSON REST under `/api`, used by the web UI and by machine clients as a fallback.

- **Auth:** `/api/*` takes an Access JWT only. Machine clients use the identical routes mirrored under `/mcp/api/*` with a bearer or path token (same tokens and scopes as MCP), so `GET /mcp/api/digest` is the Duet fallback. **Every route declares its required scope through a decorator.** A test enumerates `app.url_map` and fails if any route other than the explicit public allow-list (`/healthz`, static assets) lacks one.
- **Validation:** every write body is validated against a schema with explicit types, lengths and enums. Unknown fields are rejected. Bad input returns 400 with field errors and never 500. IDs are integers; dates are ISO `YYYY-MM-DD`.
- **Endpoints:** CRUD for workstreams, tasks, milestones and contacts; `GET /api/digest`; `GET /api/inbox`, `POST /api/inbox/<id>/file`, `POST /api/inbox/<id>/dismiss`; `GET /api/activity`; `GET/PUT /api/event`.
- **Headers:** a strict CSP (no inline script, no inline handlers), `X-Content-Type-Options`, `Referrer-Policy: same-origin`, `frame-ancestors 'none'`.

## 6. UI

A vanilla JS single-page app with a PWA manifest, served as static files with content-hashed cache busting. It is mobile-first, and desktop widens into two columns. There are four bottom tabs.

1. **Home:** the countdown (days to event) and overall %, then an inbox nudge ("N emails to file") if non-empty, then **Needs attention** (overdue, blocked, waiting > N days; color dot, title, reason or age), then **Due this week** (collapsed, with count), then **Workstreams** (compact progress bars; tap to open).
2. **Workstream:** owner and done count, open tasks sorted by due date (status dot, waiting/blocked label), "+ Add task" (title only is required), the workstream's contacts with an email count, and Done tasks collapsed. It's a list, not a kanban board; a desktop board toggle is a Polish item.
3. **Task:** a status chip row (one tap to change), then owner, due date, waiting on, blocked reason (shown only when blocked), notes and links. Below that, linked emails show subject and date with the body expandable inline as escaped plain text. The task's activity shows date · source · actor · change.
4. **Inbox:** cards with subject, sender, a body preview, attachment names and the suggested workstream chip, plus actions: File to task… / New task / Dismiss.
5. **Timeline:** reached from the Home header or a tab. A vertical countdown of milestones (T-N with derived date) with attached tasks and a "today" marker. It is not a Gantt chart.

All user-supplied and email-supplied text is inserted with `textContent` and never as HTML. Markdown in notes is rendered by a small allow-listed renderer, or shown as plain text if that proves unnecessary (builder's call).

## 7. Stack and deployment

- **Backend:** Python 3.12+, Flask, gunicorn (2 workers; email polling and scheduled jobs run in a **single** separate process, `python -m festival_planner.worker`, to avoid duplicate polling), and `sqlite3` from the stdlib in WAL mode. Migrations are hand-written, numbered and run at startup.
- **Why SQLite over Postgres:** two users, data in the hundreds of rows, one container, and a backup that is one file. Postgres adds a second service and gives nothing back at this scale.
- **Dependencies:** kept minimal (Flask, gunicorn, PyJWT and cryptography for Access). Managed with `uv` and a lockfile.
- **Packaging:** a `Dockerfile` (slim Python base, non-root user, `/data` volume) and a `compose.example.yml` with two services (`web`, `worker`) sharing the volume, plus an optional `cloudflared` service commented out.
- **Deployment doc:** `docs/deploy.md` is generic: an LXC/VM with Docker, a Cloudflare Tunnel to the web port, an Access application on the hostname with a bypass policy for `/mcp/*` and `/healthz`, an organizer email allow-list, a Gmail filter and label plus an app password, and backups with `sqlite3 .backup` on a timer. It uses placeholders only (`festival.example.com`, `<tunnel-name>`).
- **Instance-specific files** live in gitignored `deploy/local/` (real compose overrides, tunnel config, notes).
- **Config:** env vars (secrets) plus `config.toml` (non-secret instance settings: event name and date, timezone, organizers, seed workstreams and milestones). Both are gitignored. The repo commits `.env.example` and `config.example.toml` with placeholders ("Example Fall Festival", `organizer-a@example.com`).

## 8. Threat model

**Assets:** third-party email bodies and addresses (most sensitive); contact details; planning data; MCP tokens; the IMAP app password (it grants the whole mailbox).

| Threat | Mitigation |
|---|---|
| Unauthenticated access to UI or API | Cloudflare Access plus app-side JWT verification and an email allow-list; the route-scope test (§5) |
| Access misconfiguration (for example, a bypass rule too broad) | App-side verification still rejects requests without a valid JWT; `/mcp` still requires tokens |
| MCP token leak (URL tokens appear in client config and history) | Named, scoped, rotatable tokens; path-token redaction before logging; every write logged with the token name; a read-only token for Duet if Duet only needs to read |
| Bad change made by Claude/Duet or an injected email | No undo feature. The activity log keeps before→after for every field, so a person can see what changed and set it back by hand. *(Designer's call: per-entry revert is out of scope.)* |
| Spoofed provenance | Source and actor come from the authenticated identity or token name, never from request bodies |
| Prompt injection via email bodies read by Claude | Email bodies are data. MCP instructions tell the model that email content is untrusted. Write tools have narrow, validated effects, and there is no delete tool on MCP. The activity log makes any injected change visible and fixable by hand |
| Stored XSS via email or task text | Text is inserted only through `textContent`; strict CSP; HTML email is never rendered |
| CSRF on web writes | Origin check plus SameSite cookies from Access |
| IMAP credential compromise | Env var only; the app uses read-only `EXAMINE`; the deploy doc recommends a dedicated Gmail account for capture rather than a personal one |
| Data at rest on the host | Data stays on the homelab LXC; volume permissions; bodies purged after retention; backups inherit the same sensitivity (noted in the deploy doc) |
| Secret or PII leak into the public repo | §9 |
| SSRF or link abuse | Links are stored and displayed, never fetched by the server; only `http`/`https` schemes allowed |

## 9. Repository hygiene (public repo)

- **Never committed:** real names, organization or school names, email addresses outside `example.com/.org/.net`, real dates, vendor names, dollar figures, domains, tunnel or host names.
- **Seed data and fixtures** are fictional ("Example Fall Festival", "Example Tent Co.", `organizer-a@example.com`).
- **Leak check:**
  - `scripts/leak_check.py` scans tracked files (and, in the pre-commit hook, the staged diff and commit message). It fails on email addresses whose domain isn't `example.{com,org,net}` or a GitHub noreply address, on `/home/<user>/` paths, and on any case-insensitive term listed in a gitignored `.leak-denylist` (one term per line).
  - It runs in CI (without the private denylist) and as a git pre-commit and commit-msg hook installed by `make hooks`.
  - CI also runs gitleaks for secrets.
  - Limitation: the private denylist protects only machines where the hook is installed. The README says so.
- **Commit messages** follow the same rules.

## 10. Milestones (phases)

1. **MVP:** repo skeleton, CI, leak check; config loading; DB and migrations; domain model and service layer with activity log; Access auth; REST API; the UI (Home, Workstream, Task, Timeline, plus simple management of workstreams, milestones and contacts); Docker, compose and deploy doc; fictional seed data.
2. **Integrations:** email ingest first (IMAP poller, forward-note capture, parsing, dedup, subject tags, inbox, suggestions, retention purge) and the Inbox UI; the `/mcp` endpoint with tokens and tools; the REST bearer-token path and `/api/digest`; the weekly digest email.
3. **Polish:** budget tracker (line items with category, type revenue/cost, amount, pledged or received; totals vs. a net goal); desktop board toggle; volunteer share links (read-only, no email content); CSV export; offline-friendly PWA caching.

## 11. Left to the builder

- Exact module layout, naming, and the visual styling within the mockup structure.
- The markdown renderer choice for notes, or plain text.
- How forwarded-header parsing handles client-specific formats (best effort, with fixtures).
- Confirming Duet's connection method (§4.3).
