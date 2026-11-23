#!/bin/sh
# Check that a native Alpine install of Festival Planner can actually run as its service user.
#
# Usage: sh deploy/alpine/check.sh [ROOT] [CONFIG]
#   ROOT    the install root (default: the checkout this script is in)
#   CONFIG  the settings file (default: $FP_CONFIG, else /etc/festival-planner/config.toml);
#           when it does not exist yet, only the code is checked
#
# install.sh runs this at the end and the init scripts run it before starting, so a broken install
# fails with a reason instead of a service that says "started" and then cannot run. Run it as root
# (it switches to the service user with su), or as the service user itself. FP_USER overrides the
# user (default festival). Exit 0 when everything is fine; 1 with one line per problem otherwise.
set -eu

fp_user=${FP_USER:-festival}
here=$(cd "$(dirname "$0")" && pwd)
root=${1:-$(cd "$here/../.." && pwd)}
config=${2:-${FP_CONFIG:-/etc/festival-planner/config.toml}}

problems=0
problem() {
    printf 'check.sh: %s\n' "$*" >&2
    problems=$((problems + 1))
}

# Quote one argument for sh -c.
q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

# Run a shell command as the service user (directly when we already are that user).
as_user() {
    if [ "$(id -un)" = "$fp_user" ]; then
        sh -c "$1"
    elif [ "$(id -u)" -eq 0 ]; then
        su -s /bin/sh "$fp_user" -c "$1"
    else
        echo "check.sh: run as root or as $fp_user" >&2
        exit 2
    fi
}

# The first directory on the way to $1 that the service user cannot enter, if any.
first_closed_dir() {
    path=$1
    walked=""
    old_ifs=$IFS
    IFS=/
    # shellcheck disable=SC2086 # split the path on / on purpose
    set -- $path
    IFS=$old_ifs
    for part in "$@"; do
        [ -n "$part" ] || continue
        walked="$walked/$part"
        [ -d "$walked" ] || break
        if ! as_user "test -x $(q "$walked")"; then
            printf '%s\n' "$walked"
            return 0
        fi
    done
    return 0
}

venv_bin="$root/.venv/bin"
gunicorn="$venv_bin/gunicorn"

# 1. The code is there and reachable.
closed=$(first_closed_dir "$root")
if [ -n "$closed" ]; then
    problem "the $fp_user user cannot enter $closed, so it cannot run the app in $root." \
        "Install under /opt/festival-planner (not under /root or a home directory)."
elif [ ! -x "$gunicorn" ]; then
    problem "$gunicorn not found: run deploy/alpine/install.sh"
else
    # 2. The virtualenv was built where it is: its scripts name their interpreter by absolute path.
    shebang=$(head -n 1 "$gunicorn")
    case $shebang in
        "#!$venv_bin/"*) ;;
        *)
            problem "the virtualenv was built at another path (${shebang#\#!})." \
                "Remove $root/.venv and rerun deploy/alpine/install.sh."
            ;;
    esac
    # 3. The service user can actually start it.
    if [ "$problems" -eq 0 ] && ! as_user "$(q "$gunicorn") --version >/dev/null 2>&1"; then
        problem "the $fp_user user cannot run $gunicorn --version"
    fi
fi

# 4. The service user cannot change its own code.
if [ -d "$root" ] && as_user "test -w $(q "$root")"; then
    problem "the $fp_user user can write to its own code in $root: chown -R root:root $root"
fi

# 5. The settings file names an absolute data_dir that the service user can write to.
if [ -f "$config" ]; then
    python="$venv_bin/python"
    [ -x "$python" ] || python=python3
    if ! data_dir=$("$python" - "$config" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as f:
    print(tomllib.load(f).get("event", {}).get("data_dir", "data"))
PY
    ); then
        problem "cannot read $config as TOML"
    else
        case $data_dir in
            /*)
                if ! as_user "test -d $(q "$data_dir") && test -w $(q "$data_dir") && test -x $(q "$data_dir")"; then
                    problem "the $fp_user user cannot write to data_dir $data_dir (from $config):" \
                        "create it owned by $fp_user (install.sh makes /var/lib/festival-planner)"
                fi
                ;;
            *)
                problem "data_dir in $config must be an absolute path (it is \"$data_dir\")," \
                    "for example data_dir = \"/var/lib/festival-planner\""
                ;;
        esac
    fi
else
    echo "check.sh: no settings file at $config yet: checked the code only"
fi

if [ "$problems" -gt 0 ]; then
    exit 1
fi
echo "check.sh: ok ($root runs as $fp_user)"
