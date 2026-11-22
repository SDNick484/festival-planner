#!/bin/sh
# Interim leak check (replaced by scripts/leak_check.py in issue #1).
# Fails on email addresses outside example.{com,org,net} / GitHub noreply,
# /home/<user>/ paths, and any term in the gitignored .leak-denylist.
# Usage: leak-check.sh [--staged | --commit-msg FILE]
set -eu
mode=${1:-all}
denylist=${LEAK_DENYLIST:-.leak-denylist}
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT

case "$mode" in
  --staged) git diff --cached -U0 --no-color | grep '^+' | grep -v '^+++' > "$tmp" || true ;;
  --commit-msg) grep -v '^#' "$2" > "$tmp" || true ;;
  *) git ls-files -z | xargs -0 grep -nIH '' > "$tmp" 2>/dev/null || true ;;
esac

status=0
emails=$(grep -oE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' "$tmp" \
  | grep -viE '@(example\.(com|org|net)|users\.noreply\.github\.com)$' || true)
if [ -n "$emails" ]; then echo "leak-check: non-example email address(es):"; echo "$emails" | sort -u; status=1; fi

if grep -nE '/home/[a-z_][a-z0-9_-]*/' "$tmp" | grep -v '/home/<user>/' > /dev/null; then
  echo "leak-check: /home/<user>/ path found"; status=1
fi

if [ -f "$denylist" ]; then
  while IFS= read -r term; do
    case "$term" in ''|'#'*) continue ;; esac
    if grep -qiF -- "$term" "$tmp"; then echo "leak-check: denylisted term found (see $denylist)"; status=1; fi
  done < "$denylist"
fi

[ "$status" -eq 0 ] && echo "leak-check: clean"
exit "$status"
