#!/bin/sh
# Consistent online backup of the SQLite database, with rotation.
#
# Usage: scripts/backup.sh [DATA_DIR] [BACKUP_DIR]
#   DATA_DIR    folder that holds festival.db (default: $FP_DATA_DIR, else ./data)
#   BACKUP_DIR  where to write festival-YYYYMMDD-HHMMSS.db (default: $FP_BACKUP_DIR, else ./backups)
# Backups older than $FP_BACKUP_KEEP_DAYS days (default 14) are deleted.
#
# Backups contain email bodies and every activity entry: keep BACKUP_DIR private (it is created
# with mode 700 and the files with 600) and store off-box copies accordingly.
# Needs the sqlite3 command-line tool.
set -eu

data_dir=${1:-${FP_DATA_DIR:-data}}
backup_dir=${2:-${FP_BACKUP_DIR:-backups}}
keep_days=${FP_BACKUP_KEEP_DAYS:-14}
db="$data_dir/festival.db"

if [ ! -f "$db" ]; then
    echo "backup: no database at $db" >&2
    exit 1
fi
case $keep_days in
    '' | *[!0-9]*)
        echo "backup: FP_BACKUP_KEEP_DAYS must be a whole number" >&2
        exit 1
        ;;
esac

umask 077
mkdir -p "$backup_dir"
stamp=$(date -u +%Y%m%d-%H%M%S)
target="$backup_dir/festival-$stamp.db"
partial="$target.partial"

# .backup copies through SQLite's own API, so it is consistent while the app is writing (WAL).
sqlite3 "$db" ".backup '$partial'"
check=$(sqlite3 "$partial" "PRAGMA integrity_check;")
if [ "$check" != "ok" ]; then
    rm -f "$partial"
    echo "backup: integrity check failed" >&2
    exit 1
fi
mv "$partial" "$target"
echo "backup: wrote $target"

# Rotation: only files this script names, only completed ones.
find "$backup_dir" -maxdepth 1 -type f -name 'festival-*.db' -mtime +"$keep_days" -delete
