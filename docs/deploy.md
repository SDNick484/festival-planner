# Deploying Festival Planner

One deployment serves one event. This guide uses placeholders throughout: `festival.example.com`
for the public name, `<host>` for the machine that runs the container, and `<tunnel-token>` for the
Cloudflare Tunnel token. Keep your real values in `deploy/local/`, which is gitignored.

There are two supported ways to run it: Docker with Compose (sections 1 to 9, below) and a native
install on Alpine Linux with OpenRC and no Docker (the section "Alpine (native, no Docker)" at the
end). The Cloudflare, email capture, share-link and backup-restore guidance applies to both.

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
   `festival.example.com`. Its **service** URL is where the connector (`cloudflared`) can reach
   the web service, which depends on where each of them runs:

   | Web service | Connector | Service URL |
   |---|---|---|
   | Docker (this guide) | the optional `cloudflared` service in `compose.yml` (`TUNNEL_TOKEN=<tunnel-token>` in `.env`) | `http://web:8000` |
   | Docker | on the host (`cloudflared service install <tunnel-token>`) | `http://127.0.0.1:8000` |
   | Alpine, native (default bind `127.0.0.1:8000`) | native, on the same host (section "Alpine") | `http://127.0.0.1:8000` |
   | Alpine, native, with `FP_WEB_BIND='<lan-address>:8000'` | anywhere else, including a Docker container on the same host | `http://<lan-address>:8000` |

   A connector in a Docker container cannot use `127.0.0.1` to reach a service on its host:
   inside the container that address is the container itself. Use the last row (or run that
   container with `--network host`). Write `127.0.0.1`, not `localhost`, which may resolve to the
   IPv6 `::1`. A wrong service URL shows up as a Cloudflare **502** after you sign in.
2. **Access application.** Create a self-hosted Access application for `festival.example.com` with
   an **Allow** policy listing exactly the two organizer email addresses.
3. **Bypass policy.** Add a second policy, action **Bypass**, for these paths, ordered above the
   Allow policy (or as separate path-scoped applications):
   - `/mcp/*`: machine clients (Claude, Duet) cannot sign in through Access. The app authenticates
     them itself with the bearer or path tokens in `MCP_TOKENS`, and rejects everything else.
   - `/healthz`: uptime checks and the container health probe.
   - `/s/*`: volunteer share links (section 9). Volunteers have no Access identity; the link's
     own token is the credential, checked by the app.
   - `/static/*`: the stylesheet and scripts the share page loads. These are the app's public code,
     styles and icons (the same files as in this repository) and hold no event data.

   Do not bypass anything else: `/api/*` and the web pages accept only an Access identity.
4. **AUD tag.** Open the Access application, copy its **Application Audience (AUD) tag**, and set
   `CF_AUD` in `.env`. Set `CF_TEAM_DOMAIN` to your Zero Trust team domain (a bare host name such as
   `<team>.cloudflareaccess.com`). Restart with `docker compose up -d` (on Alpine, put them in
   `/etc/conf.d/festival-planner` and `rc-service festival-planner-web restart`).

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

## 9. Volunteer share links

Organizers create read-only links on the Manage screen, under **Share links**: a label, one
workstream or all of them, and how many days the link stays valid (1 to 365). The link
(`https://festival.example.com/s/<token>`) is shown **once**, right after it is created: copy it
then. The app stores only a SHA-256 hash of the token, so a lost link cannot be shown again; create
a new one and revoke the old.

A link shows task titles, statuses, due dates and owner display names, and milestone names and
dates, for its scope. It never shows notes, links, waiting or blocked reasons, contacts, email,
activity or budget. **Revoke** turns a link off at once; an expired, revoked or unknown link gets
the same "not available" page. Share pages are sent with `Cache-Control: no-store` and
`Referrer-Policy: no-referrer`, and the token is replaced by `redacted` in the app's access log
lines. Anyone holding a link can read what it shows until it expires or is revoked, so prefer short
lifetimes and one link per group.

This needs the `/s/*` and `/static/*` Bypass entries from section 4.

## Alpine (native, no Docker)

The same app, run as two OpenRC services on an Alpine Linux host (a small LXC or VM) with no
container runtime. It uses the same entry points as the image: `gunicorn --preload
festival_planner.wsgi:app` for the web service and `python -m festival_planner.worker` for the
worker, both as the unprivileged system user `festival`. The Docker path above stays supported;
pick one per host, never both against the same data. The CI `alpine` job installs exactly as below
and serves `/healthz` under musl.

```
phone / browser --> Cloudflare Access --> Cloudflare Tunnel --> cloudflared (same host)
                                                                      |
Claude / Duet   --> /mcp/* (Bypass, token auth in the app) ---> http://127.0.0.1:8000 (web)
```

### Prerequisites

- Alpine 3.24 or newer, with OpenRC (the default init), and root. Alpine ships Python 3.14, which
  satisfies the project's `>=3.12`. A host that is reachable only through the tunnel is fine.
- Cloudflare, mailbox and `sqlite3` needs as in section 1. `install.sh` installs `sqlite3`.

### Install

```sh
apk add git
git clone <repository-url> /opt/festival-planner
cd /opt/festival-planner
sh deploy/alpine/install.sh --with-cron
```

`install.sh` is idempotent (run it again after every upgrade), needs root, never starts a service
and never writes a secret. It:

- installs `python3`, `tzdata`, `sqlite` and `openrc` with `apk`, and `uv` from Alpine when the
  release packages it (otherwise the pinned uv release binary, checksum verified);
- creates the system user and group `festival` (no login shell, no password) and the directories
  `/var/lib/festival-planner` (data, mode 700), `/var/backups/festival-planner` (backups, mode 700),
  `/var/log/festival-planner` and `/etc/festival-planner` (settings, group-readable);
- runs `uv sync --frozen --no-dev` in the checkout, which creates `.venv` from the locked versions
  and the system Python. The code and `.venv` stay root-owned: the service user cannot change them;
- installs `/etc/init.d/festival-planner-web` and `/etc/init.d/festival-planner-worker`, and the
  example `/etc/conf.d/festival-planner` (root-owned, mode 600) **only if that file does not exist**;
- with `--with-cron`, installs the daily backup job (see Backups below);
- ends by running `deploy/alpine/check.sh` (below), and fails if the install cannot run as
  `festival`.

The checkout is the install root: the init scripts expect it at `/opt/festival-planner`. To put it
elsewhere, set `FP_ROOT='<path>'` in `/etc/conf.d/festival-planner`, and pick a path the service
user can reach: **not under `/root` or a home directory**, which `festival` cannot enter. If you
move a checkout, run `install.sh` again: the virtualenv's scripts name their interpreter by
absolute path, so `install.sh` rebuilds a `.venv` that was built somewhere else.

### Check the install

```sh
sh /opt/festival-planner/deploy/alpine/check.sh
```

`check.sh` runs as root and checks, as the `festival` user, that it can enter the install root and
run `.venv/bin/gunicorn`, that the virtualenv was built where it is, that it cannot write to its
own code (keep the checkout `root:root`), and that `data_dir` in
`/etc/festival-planner/config.toml` is an absolute path it can write to. It prints one line per
problem, with the fix. `install.sh` runs it at the end, and both services run it before they
start, so a broken install stops `rc-service ... start` with the reason instead of starting a
supervisor that cannot run the app. Run it again after editing the settings.

### Configure

```sh
cp /opt/festival-planner/config.example.toml /etc/festival-planner/config.toml
chown root:festival /etc/festival-planner/config.toml
chmod 640 /etc/festival-planner/config.toml
```

Edit `config.toml` as in section 2 (event, organizers, seeds, mailbox host and user), and set
`data_dir = "/var/lib/festival-planner"`. It must be an absolute path the service user owns: the
example's relative `data` would be inside the root-owned install root (`check.sh` refuses it).

Secrets go in `/etc/conf.d/festival-planner`, the same variables as `.env.example`: `CF_TEAM_DOMAIN`,
`CF_AUD` (section 4), `MCP_TOKENS`, `IMAP_PASSWORD`, `SMTP_PASSWORD`. Generate each machine token
as in section 2. The file is read by the shell: keep every value in single quotes. Keep it root-owned
with mode 600 (`install.sh` re-applies that), and do not set `DEV_AUTH_EMAIL` or `FLASK_ENV`.

The web service listens on `127.0.0.1:8000`, for a native `cloudflared` on the same host. To listen on
another address, set `FP_WEB_BIND='<lan-address>:8000'` in the conf.d file and keep the port closed to
everything but the connector. You need that when the connector runs on another machine, **or in a
Docker container on this host** (inside a container, `127.0.0.1` is the container itself). Point
the tunnel's service URL at the same address (the table in section 4).

### Start

```sh
rc-update add festival-planner-web default
rc-update add festival-planner-worker default
rc-service festival-planner-web start
rc-service festival-planner-worker start
wget -qO- http://127.0.0.1:8000/healthz     # {"last_email_poll":null,"ok":true} (or your FP_WEB_BIND)
```

Both services use `supervise-daemon`, so a crash is respawned after five seconds, and both need
networking. The worker starts after the web service, which applies migrations and the first-run
seed. Run exactly one worker. `rc-service festival-planner-web restart` and `... status` work as
usual; stopping the worker is clean (it exits 0 on SIGTERM). An invalid config makes the web
service exit at start; the supervisor keeps retrying every five seconds, so read the reason in
`web.log` and fix `config.toml` or the conf.d file.

### Logs

```sh
tail -f /var/log/festival-planner/web.log /var/log/festival-planner/worker.log
```

Both files are written by the supervisor (gunicorn's own messages and the worker's log lines; no
message bodies, no credentials). Rotate them with `apk add logrotate` and a file such as
`/etc/logrotate.d/festival-planner` (`/var/log/festival-planner/*.log { weekly rotate 4 compress
copytruncate missingok }`).

### Monitoring the worker

The worker serves no port. Watch `last_email_poll` in the web service's `/healthz`, as in section 3:
it is the UTC time of the last successful mailbox poll (`null` before the first, and always `null`
when capture is off). Alert when it is older than a few poll intervals. `rc-service
festival-planner-worker status` tells you only that the process is up.

### cloudflared as a native service

Run the tunnel connector on the same host and point the tunnel's public hostname
(`festival.example.com`) at `http://127.0.0.1:8000`; then set up Access and its Bypass policy as in
section 4. Alpine's stable repositories do not package `cloudflared` (the `testing` repository does,
if you accept packages from there), so use the official static release binary, which runs on musl:

```sh
version=<release-version>                     # the latest tag on the cloudflared releases page
arch=amd64                                    # arm64 on an aarch64 host
wget -O /usr/local/bin/cloudflared \
  https://github.com/cloudflare/cloudflared/releases/download/$version/cloudflared-linux-$arch
echo "<sha256-from-the-release-page>  /usr/local/bin/cloudflared" | sha256sum -c -
chmod 755 /usr/local/bin/cloudflared
addgroup -S cloudflared && adduser -S -D -H -s /sbin/nologin -G cloudflared cloudflared
```

Put the tunnel token in `/etc/conf.d/cloudflared` (`TUNNEL_TOKEN='<tunnel-token>'`, root-owned,
mode 600) and add `/etc/init.d/cloudflared` (mode 755):

```sh
#!/sbin/openrc-run
description="Cloudflare Tunnel connector"
conf=/etc/conf.d/cloudflared
if [ -r "$conf" ]; then set -a; . "$conf"; set +a; fi
supervisor="supervise-daemon"
command="/usr/local/bin/cloudflared"
command_args="tunnel --no-autoupdate run"
command_user="cloudflared:cloudflared"
output_log="/var/log/cloudflared.log"
error_log="/var/log/cloudflared.log"
respawn_delay=5
respawn_max=0
depend() {
	need net
	after festival-planner-web
}
```

then `rc-update add cloudflared default && rc-service cloudflared start`. To update the connector,
repeat the download with the new version (verify the checksum again) and restart the service.
`cloudflared` makes only outbound connections, so nothing needs to listen on the network.

### Backups

`scripts/backup.sh` takes a consistent copy with SQLite's `.backup`, checks it, and rotates it, as in
section 6. Here the database is a plain directory, so no volume lookup is needed:

```sh
su -s /bin/sh festival -c \
  '/opt/festival-planner/scripts/backup.sh /var/lib/festival-planner /var/backups/festival-planner'
```

`install.sh --with-cron` installs this as the crontab `/etc/crontabs/festival` (owned by root, which
busybox `crond` requires; the job runs as `festival`), daily at 03:17:

```
17 3 * * * /opt/festival-planner/scripts/backup.sh /var/lib/festival-planner /var/backups/festival-planner
```

Turn `crond` on with `rc-update add crond default && rc-service crond start`. To install the line by
hand, put it in that file yourself. The same cautions as in section 6 apply: backups hold email
bodies and the whole activity log, so keep the folder private and encrypt any off-box copy. To
restore: stop both services, copy a backup over `/var/lib/festival-planner/festival.db` (delete any
`festival.db-wal` and `festival.db-shm`; keep owner `festival`, mode 600), and start them again.

### Upgrades

```sh
cd /opt/festival-planner
su -s /bin/sh festival -c '/opt/festival-planner/scripts/backup.sh /var/lib/festival-planner /var/backups/festival-planner'
git fetch --tags && git checkout <release-tag>
UV_PYTHON=/usr/bin/python3 UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev
rc-service festival-planner-web restart
rc-service festival-planner-worker restart
```

Migrations run when the web service starts and are applied in order inside transactions. Running
`sh deploy/alpine/install.sh` again instead of the `uv sync` line does the same and also refreshes
the init scripts and Alpine packages. If the release changes `config.example.toml`, compare it with
your `/etc/festival-planner/config.toml`; the conf.d file is never overwritten.

### Instance specifics

Keep your real conf.d values, cron entries and notes in `deploy/local/` (gitignored), never in
`deploy/alpine/`, which holds only placeholders.
