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
