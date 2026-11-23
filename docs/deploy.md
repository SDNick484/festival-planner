# Deploying Festival Planner

One deployment serves one event. This guide uses placeholders throughout: `festival.example.com`
for the public name, `<host>` for the machine that runs the container, and `<tunnel-token>` for the
Cloudflare Tunnel token. Keep your real values in `deploy/local/`, which is gitignored.

```
phone / browser --> Cloudflare Access --> Cloudflare Tunnel --> http://<host>:8000 (web)
Claude / Duet   --> /mcp/* (Bypass, token auth in the app) ---^
```

## 1. Prerequisites

- An LXC container or VM with Docker and the Compose plugin, reachable by the machine that runs
  `cloudflared` (the same host is fine).
- A Cloudflare account with a domain, for the Tunnel and Access.
- A dedicated mailbox for email capture (optional, see section 5).
- The `sqlite3` command-line tool on the host, for backups.

## 2. Configure

```sh
git clone <repository-url> festival-planner && cd festival-planner
cp config.example.toml config.toml
cp .env.example .env
chmod 600 .env
```

Edit `config.toml`: event name, date and time zone, the two organizers and their aliases, the
workstreams and milestones to seed, and the mailbox host and user. Leave `data_dir = "data"`: in
the container that path is the `/data` volume.

Generate each machine token (and keep each one unique, 24 characters or more):

```sh
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

Put them in `.env` as `MCP_TOKENS=claude:write:<secret>,duet:read:<secret>`. `.env` also holds
`CF_TEAM_DOMAIN`, `CF_AUD` (section 4) and the mailbox passwords. Do not set `DEV_AUTH_EMAIL` or
`FLASK_ENV` in production: the app refuses to start with `DEV_AUTH_EMAIL` outside development.

## 3. Run

```sh
docker build -t festival-planner:local .
cp compose.example.yml compose.yml
docker compose up -d
curl http://127.0.0.1:8000/healthz     # {"ok":true}
```

The container runs as the unprivileged user `app` (uid 10001). The SQLite database lives in the
`fp-data` volume at `/data`; `config.toml` is mounted read-only. Migrations and first-time seeding
run when the web container starts, and an invalid config stops it with an error in
`docker compose logs web`.

The `worker` service (mailbox polling and the daily jobs) runs `python -m festival_planner.worker`
from the same image. Run exactly one. It has no web port, so its container healthcheck is off;
monitor `last_email_poll` in the web service's `/healthz` instead (the UTC time of the last
successful poll, `null` before the first). Stopping it (`docker compose stop worker`) is clean.

## 4. Cloudflare Tunnel and Access

1. **Tunnel.** In Cloudflare Zero Trust, create a tunnel and add a public hostname
   `festival.example.com` with service `http://<host>:8000`. Run the connector on the host
   (`cloudflared service install <tunnel-token>`), or enable the optional `cloudflared` service in
   `compose.yml`, with `TUNNEL_TOKEN=<tunnel-token>` in `.env` and the service URL `http://web:8000`.
2. **Access application.** Create a self-hosted Access application for `festival.example.com` with
   an **Allow** policy listing exactly the two organizer email addresses.
3. **Bypass policy.** Add a second policy, action **Bypass**, for these paths, ordered above the
   Allow policy (or as separate path-scoped applications):
   - `/mcp/*`: machine clients (Claude, Duet) cannot sign in through Access. The app authenticates
     them itself with the bearer or path tokens in `MCP_TOKENS`, and rejects everything else.
   - `/healthz`: uptime checks and the container health probe.

   Do not bypass anything else: `/api/*` and the web pages accept only an Access identity.
4. **AUD tag.** Open the Access application, copy its **Application Audience (AUD) tag**, and set
   `CF_AUD` in `.env`. Set `CF_TEAM_DOMAIN` to your Zero Trust team domain (a bare host name such as
   `<team>.cloudflareaccess.com`). Restart with `docker compose up -d`.

The app verifies the Access JWT itself, so a request that reaches port 8000 without going through
Access is still refused. Even so, do not expose port 8000 to the internet: bind it to the LAN or
loopback, and let only the tunnel reach it.

## 5. Email capture (Gmail)

Optional: skip it by omitting `imap_host` in `config.toml`.

1. Use a **dedicated** Google account for the planner, never an organizer's own mailbox.
2. Create a Gmail filter that matches mail addressed to the `+festival` alias
   (`planner+festival@example.com`) and applies the label named in `imap_label`. Organizers forward
   or cc that address.
3. Enable IMAP in the account's settings, turn on 2-step verification and create an **app
   password**. Put it in `.env` as `IMAP_PASSWORD`; the host and user are in `config.toml`.
   The same applies to `SMTP_PASSWORD` if you use the digest.

Capture reads only the labelled mail, read-only, and stores a text version of the body (capped at
200 KB). Attachments are not stored, only their names.

## 6. Backups

`scripts/backup.sh` takes a consistent copy of the live database with SQLite's `.backup`, checks it,
and deletes copies older than 14 days (`FP_BACKUP_KEEP_DAYS` changes that). The database is in the
Docker volume, so point the script at the volume's directory on the host:

```sh
docker volume inspect fp-data --format '{{ .Mountpoint }}'     # the data directory
FP_DATA_DIR=<volume-mountpoint> FP_BACKUP_DIR=/srv/backups/festival scripts/backup.sh
```

Run it daily from cron (`17 3 * * * ...`) or a systemd timer. **Backups contain email bodies** and
the whole activity log: keep the folder private (the script creates it with mode 700), encrypt any
off-site copy, and delete backups you no longer need. To restore, stop the stack, copy a backup over
`festival.db` in the volume (remove any `festival.db-wal` and `festival.db-shm` files), start again.

## 7. Upgrades

```sh
git pull
docker build -t festival-planner:local .
docker compose up -d
```

Migrations run on start and are applied in order inside transactions. Take a backup first.

## 8. Instance specifics

Keep anything particular to your deployment (your compose file, host names, cron entries, notes) in
`deploy/local/`. It is gitignored, so it never reaches the public repository.
