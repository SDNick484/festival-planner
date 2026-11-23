.PHONY: dev test ui-test lint format check leak-check seed-demo hooks

dev:
	FLASK_ENV=development uv run flask --app festival_planner.web:create_app run --debug

test:
	uv run pytest -q

# Browser tests only. First time: uv run playwright install chromium
ui-test:
	uv run pytest tests/ui -q

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .

leak-check:
	uv run python scripts/leak_check.py

# Development only: adds fictional demo tasks and contacts to the database named in config.toml.
seed-demo:
	FLASK_ENV=development uv run python -m festival_planner.seed

# The same commands as the CI jobs (gitleaks runs in CI only).
check: test lint leak-check

# Installs git hooks that run the leak checker. Run once per clone.
hooks:
	@hooks=$$(git rev-parse --git-path hooks) && \
	printf '%s\n' '#!/bin/sh' 'exec python3 scripts/leak_check.py --staged' > "$$hooks/pre-commit" && \
	printf '%s\n' '#!/bin/sh' 'exec python3 scripts/leak_check.py --commit-msg "$$1"' > "$$hooks/commit-msg" && \
	chmod +x "$$hooks/pre-commit" "$$hooks/commit-msg" && \
	echo "installed pre-commit and commit-msg hooks in $$hooks"
