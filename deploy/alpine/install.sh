#!/bin/sh
# Native install of Festival Planner on Alpine Linux (OpenRC, no Docker). Run as root, from the
# checkout that is to become the install root (by default /opt/festival-planner).
#
# Usage: sh deploy/alpine/install.sh [--with-cron]
#   --with-cron   also install a daily backup job for busybox crond (/etc/crontabs/festival)
#
# Idempotent: run it again after an upgrade. It never writes secrets, never overwrites an existing
# /etc/conf.d/festival-planner and never starts or enables a service. See docs/deploy.md.
#
# Environment overrides (all optional):
#   FP_DATA_DIR (/var/lib/festival-planner), FP_BACKUP_DIR (/var/backups/festival-planner),
#   FP_CONFIG_DIR (/etc/festival-planner), FP_UV_SOURCE=release (use the pinned uv release even
#   if Alpine packages uv). The service user is always festival: the init scripts name it.
set -eu

# The pinned uv release used when Alpine does not package uv (musl builds, the same version as the
# Dockerfile). Checksums are from the release's published .sha256 files.
UV_VERSION=0.9.30
UV_SHA256_X86_64=1caf8fe092e2005dd4c134ba515c1aa3eea3d3c143f8a1903bcb58fcdf169365
UV_SHA256_AARCH64=b658b56957bceea742ca14f3ef28fb3542adbcedfb8bd5bd718ae255394ccd09

fp_user=festival
data_dir=${FP_DATA_DIR:-/var/lib/festival-planner}
backup_dir=${FP_BACKUP_DIR:-/var/backups/festival-planner}
config_dir=${FP_CONFIG_DIR:-/etc/festival-planner}
log_dir=/var/log/festival-planner
confd=/etc/conf.d/festival-planner
default_root=/opt/festival-planner

with_cron=no
for arg in "$@"; do
    case $arg in
        --with-cron) with_cron=yes ;;
        -h | --help)
            sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "install.sh: unknown option: $arg" >&2
            exit 2
            ;;
    esac
done

say() { printf '==> %s\n' "$*"; }
die() {
    printf 'install.sh: %s\n' "$*" >&2
    exit 1
}

[ "$(id -u)" -eq 0 ] || die "run as root"
[ -f /etc/alpine-release ] || die "this script is for Alpine Linux"
command -v apk >/dev/null 2>&1 || die "apk not found"

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
[ -f "$root/uv.lock" ] && [ -f "$root/pyproject.toml" ] || die "$root is not a festival-planner checkout"

# --- packages -----------------------------------------------------------------------------------
say "installing packages (python3 tzdata sqlite openrc)"
apk add --no-cache python3 tzdata sqlite openrc

install_uv_release() {
    case $(uname -m) in
        x86_64) arch=x86_64 sum=$UV_SHA256_X86_64 ;;
        aarch64) arch=aarch64 sum=$UV_SHA256_AARCH64 ;;
        *) die "no pinned uv release for $(uname -m): install uv by hand" ;;
    esac
    name=uv-$arch-unknown-linux-musl
    tmp=$(mktemp -d)
    say "downloading uv $UV_VERSION ($name)"
    wget -q -O "$tmp/$name.tar.gz" \
        "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$name.tar.gz"
    echo "$sum  $tmp/$name.tar.gz" | sha256sum -c - >/dev/null || die "uv checksum mismatch"
    tar -xzf "$tmp/$name.tar.gz" -C "$tmp"
    install -m 755 "$tmp/$name/uv" /usr/local/bin/uv
    rm -rf "$tmp"
}

if [ "${FP_UV_SOURCE:-}" = release ]; then
    install_uv_release
elif command -v uv >/dev/null 2>&1; then
    say "uv already present: $(uv --version)"
elif apk add --no-cache uv >/dev/null 2>&1; then
    say "installed uv from Alpine"
else
    say "Alpine has no uv package for this release"
    install_uv_release
fi

# --- user and directories -----------------------------------------------------------------------
if ! getent group "$fp_user" >/dev/null 2>&1; then
    say "creating group $fp_user"
    addgroup -S "$fp_user"
fi
if ! id "$fp_user" >/dev/null 2>&1; then
    say "creating user $fp_user"
    adduser -S -D -H -h "$data_dir" -s /sbin/nologin -G "$fp_user" "$fp_user"
fi

say "creating directories"
install -d -m 700 -o "$fp_user" -g "$fp_user" "$data_dir"
install -d -m 700 -o "$fp_user" -g "$fp_user" "$backup_dir"
install -d -m 750 -o "$fp_user" -g "$fp_user" "$log_dir"
# The settings file goes here: root writes it, the service user reads it.
install -d -m 750 -o root -g "$fp_user" "$config_dir"

# --- the app's virtual environment --------------------------------------------------------------
# System Python only (never download one: uv's managed builds are not what Alpine runs), locked
# versions only, no dev tools. The files stay root-owned: the service user cannot change its code.
say "installing locked dependencies into $root/.venv"
(
    cd "$root"
    UV_PYTHON=/usr/bin/python3 UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 \
        uv sync --frozen --no-dev
)

# --- init scripts and conf.d --------------------------------------------------------------------
say "installing init scripts"
install -m 755 "$here/festival-planner-web.initd" /etc/init.d/festival-planner-web
install -m 755 "$here/festival-planner-worker.initd" /etc/init.d/festival-planner-worker

if [ -e "$confd" ]; then
    say "keeping existing $confd"
    chown root:root "$confd"
    chmod 600 "$confd"
else
    say "installing example $confd (placeholders only: edit it)"
    install -m 600 -o root -g root "$here/festival-planner.confd" "$confd"
fi

# --- backup job (optional) ----------------------------------------------------------------------
if [ "$with_cron" = yes ]; then
    say "installing the daily backup job in /etc/crontabs/$fp_user"
    install -d -m 755 /etc/crontabs
    {
        echo "# Installed by deploy/alpine/install.sh: daily backup of the SQLite database."
        echo "SHELL=/bin/sh"
        echo "17 3 * * * $root/scripts/backup.sh $data_dir $backup_dir"
    } >"/etc/crontabs/$fp_user.new"
    # busybox crond ignores a crontab that root does not own; the job runs as the user it is named for.
    chown root:root "/etc/crontabs/$fp_user.new"
    chmod 600 "/etc/crontabs/$fp_user.new"
    mv "/etc/crontabs/$fp_user.new" "/etc/crontabs/$fp_user"
fi

# --- next steps ---------------------------------------------------------------------------------
cat <<NEXT

Installed. Nothing has been started. Next:

  1. Settings:   cp $root/config.example.toml $config_dir/config.toml
                 then edit it and set  data_dir = "$data_dir"
                 chown root:$fp_user $config_dir/config.toml && chmod 640 $config_dir/config.toml
  2. Secrets:    edit $confd (root-owned, mode 600; it holds placeholders now)
NEXT
if [ "$root" != "$default_root" ]; then
    echo "                 and uncomment FP_ROOT there, set to '$root'"
fi
cat <<NEXT
  3. Start:      rc-update add festival-planner-web default
                 rc-update add festival-planner-worker default
                 rc-service festival-planner-web start
                 rc-service festival-planner-worker start
  4. Check:      wget -qO- http://127.0.0.1:8000/healthz
NEXT
if [ "$with_cron" = yes ]; then
    echo "  5. Backups:    rc-update add crond default && rc-service crond start"
else
    echo "  5. Backups:    rerun with --with-cron, or see the Backups part of docs/deploy.md"
fi
echo "See docs/deploy.md for cloudflared, monitoring and upgrades."
