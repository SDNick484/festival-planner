# Festival Planner

A small, self-hosted, mobile-first planning tracker for two co-organizers of a volunteer-run event.
Changes made on the web, by email and by Claude or Duet all land in one model with one activity log
that records where each change came from. It shows what is due, what is stuck, how each workstream
is going and how many days are left.

The project is generic: one deployment serves one event, and everything specific to it lives in
configuration. See `docs/specs/festival-planner-design.md` for the design and
`docs/plans/festival-planner-plan.md` for the build plan.

> Screenshot placeholder: the phone home screen will go here once the UI exists.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer.

```sh
uv sync
cp config.example.toml config.toml
make dev
```

Then open http://127.0.0.1:5000/healthz, which returns `{"ok": true}`. `config.toml` and `.env`
are gitignored; `.env.example` lists the secrets that belong in the environment.

## Deploying

`Dockerfile` and `compose.example.yml` run the app in a container behind a Cloudflare Tunnel and
Access. See [`docs/deploy.md`](docs/deploy.md) for the full guide (Tunnel, Access bypass for
`/mcp/*` and `/healthz`, Gmail capture, backups with `scripts/backup.sh`, upgrades).

To run it natively on Alpine Linux instead (OpenRC services, no Docker), see
[Alpine (native, no Docker)](docs/deploy.md#alpine-native-no-docker) and `deploy/alpine/`.

## Claude and Duet

The app serves an MCP endpoint at `/mcp` (JSON-RPC over HTTP POST, no sessions). It is reached with
an MCP token from `config.toml`: each token has a name (recorded as the source of every change it
makes, for example `claude`) and a scope. A `read` token sees only the read tools; a `write` token
also sees the write tools (`create_task`, `update_task`, `log_note`, `file_email`,
`dismiss_email`, `upsert_contact`). There is no delete tool. Keep tokens secret: anyone holding
one can act as that connector.

- **claude.ai custom connector.** Add a custom connector and give it the URL
  `https://festival.example.com/mcp/<token>`, with your own host name and the token's secret in
  place of the placeholders. The connector form has no header field, so the secret travels in the
  path; the server removes it before logging.
- **Claude Code.** Send the token as a header instead:

  ```sh
  claude mcp add --transport http festival https://festival.example.com/mcp \
    --header "Authorization: Bearer <token>"
  ```

- **Duet.** Duet's connection method is still to be confirmed. Use a `read` token for it unless
  it needs to make changes.
- **HTTP fallback.** If a client cannot speak MCP, `GET /mcp/api/digest` with the same token
  (either the `Authorization` header or the path form
  `https://festival.example.com/mcp/<token>/api/digest`) returns the same digest as the
  `get_digest` tool as JSON.

The `/mcp/*` paths bypass Cloudflare Access, so the token is their only protection (see
[`docs/deploy.md`](docs/deploy.md)).

## Leak check and git hook

This repository is public, so it must never contain real names, organizations, email addresses
(other than `example.com`, `example.org`, `example.net` and GitHub noreply addresses), dates,
vendors, money figures or domains. `scripts/leak_check.py` fails on:

- email addresses outside those domains,
- `/home/<name>/` paths,
- any case-insensitive term listed in `.leak-denylist` (one term per line, `#` comments).

Install the hooks once per clone:

```sh
make hooks
```

This installs a `pre-commit` hook (scans the staged diff) and a `commit-msg` hook (scans the
commit message). Keep your real terms in the gitignored `.leak-denylist`. The private denylist
only protects machines where the hook is installed: CI has no access to it and only applies the
email and path rules.

## Checks

| Check | Local command |
|---|---|
| Tests | `uv run pytest -q` |
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format --check .` |
| Leak check | `uv run python scripts/leak_check.py` |
| Secrets in history | `gitleaks git .` (CI runs a pinned release) |

`make check` runs the first four. CI runs the same commands, with the tests on Python 3.12 and
3.13.

## License

MIT. See `LICENSE`.
