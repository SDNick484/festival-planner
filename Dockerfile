# Festival Planner image: web (and, from Task 15, the worker) from the same image.
FROM python:3.12-slim

# The tz database: ZoneInfo needs it, and slim images are not guaranteed to ship one (the current
# base happens to; this keeps the image correct if that changes). Installing the OS package keeps
# the runtime dependencies to the four the project allows (no `tzdata` wheel).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# uv, pinned, only to install the locked dependencies.
COPY --from=ghcr.io/astral-sh/uv:0.9.30@sha256:538e0b39736e7feae937a65983e49d2ab75e1559d35041f9878b7b7e51de91e4 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Unprivileged user; it owns /data and nothing else.
RUN useradd --system --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin app \
    && mkdir /data \
    && chown app:app /data

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# The code and the SQL migrations (festival_planner/migrations/) ship inside the package.
COPY festival_planner ./festival_planner

# The example config says `data_dir = "data"` (relative to /app): point that at the volume.
RUN ln -s /data /app/data

USER app
VOLUME /data
EXPOSE 8000

# python, not curl: the slim image has no curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]

# --preload loads the config and runs migrations and seeding once, before the workers fork.
# --no-control-socket: gunicorn's control socket wants a writable home directory, and the app
# user has none (it would log a permission error at every start).
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "--preload", "--no-control-socket", \
     "festival_planner.wsgi:app"]
