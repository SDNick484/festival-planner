#!/usr/bin/env python3
"""Leak check for a public repository (standard library only).

Fails on:
  * email addresses whose domain is not example.com/.org/.net or a GitHub noreply domain,
  * /home/<name>/ paths (the literal placeholder /home/<user>/ is fine),
  * any case-insensitive term listed in the gitignored ``.leak-denylist`` (one term per line,
    ``#`` comments). The denylist is optional: CI runs without it.

Usage: leak_check.py [--staged] [--commit-msg FILE] [--denylist PATH] [PATH ...]

With no mode flag it scans every tracked file (``git ls-files``). ``--staged`` scans only the
lines added in the staged diff; ``--commit-msg FILE`` scans a commit message (``#`` comment lines
are ignored); explicit PATHs are scanned as given. Exit 0 means clean. Exit 1 prints one
``path:line: reason`` per finding. Matched text is never echoed, so the output is safe to
publish in CI logs.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
ALLOWED_EMAIL_DOMAINS = frozenset(
    {"example.com", "example.org", "example.net", "users.noreply.github.com"}
)
HOME_PATH_RE = re.compile(r"/home/(?!<user>/)[A-Za-z_][A-Za-z0-9_.-]*/")
SKIP_NAMES = frozenset({"uv.lock"})
DEFAULT_DENYLIST = ".leak-denylist"

Finding = tuple[str, int, str]


def load_denylist(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    terms = (line.strip() for line in text.splitlines())
    return [t.lower() for t in terms if t and not t.startswith("#")]


def scan_line(line: str, denylist: Iterable[str]) -> Iterator[str]:
    """Yield a reason for each problem found in one line."""
    for match in EMAIL_RE.finditer(line):
        if match.group(1).lower() not in ALLOWED_EMAIL_DOMAINS:
            yield "non-example email address"
    if HOME_PATH_RE.search(line):
        yield "home path"
    lowered = line.lower()
    if any(term in lowered for term in denylist):
        yield "denylisted term"


def scan_lines(
    name: str, numbered: Iterable[tuple[int, str]], denylist: list[str]
) -> list[Finding]:
    return [
        (name, number, reason) for number, line in numbered for reason in scan_line(line, denylist)
    ]


def read_text(path: Path) -> str | None:
    """File text, or None when the file is missing, unreadable or binary."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    return data.decode("utf-8", errors="replace")


def scan_file(path: Path, denylist: list[str], display: str | None = None) -> list[Finding]:
    if path.name in SKIP_NAMES:
        return []
    text = read_text(path)
    if text is None:
        return []
    return scan_lines(display or str(path), enumerate(text.splitlines(), start=1), denylist)


def git(*args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout


def tracked_files() -> list[str]:
    return [p for p in git("ls-files", "-z").split("\0") if p]


HUNK_RE = re.compile(r"^@@ -\S+ \+(\d+)(?:,\d+)? @@")


def scan_staged(denylist: list[str]) -> list[Finding]:
    """Scan only the lines added by the staged diff."""
    diff = git(
        "-c", "core.quotepath=off", "diff", "--cached", "-U0", "--no-color", "--diff-filter=ACMR"
    )
    findings: list[Finding] = []
    path = ""
    number = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].removeprefix("b/")
        elif line.startswith("@@"):
            hunk = HUNK_RE.match(line)
            number = int(hunk.group(1)) if hunk else 0
        elif line.startswith("+") and path and Path(path).name not in SKIP_NAMES:
            findings += scan_lines(path, [(number, line[1:])], denylist)
            number += 1
    return findings


def scan_commit_msg(path: Path, denylist: list[str]) -> list[Finding]:
    text = read_text(path) or ""
    numbered = [
        (n, line) for n, line in enumerate(text.splitlines(), start=1) if not line.startswith("#")
    ]
    return scan_lines(path.name, numbered, denylist)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail on identifying data in a public repo.")
    parser.add_argument("--staged", action="store_true", help="scan the staged diff")
    parser.add_argument("--commit-msg", metavar="FILE", help="scan a commit message file")
    parser.add_argument("--denylist", default=DEFAULT_DENYLIST, help="denylist file")
    parser.add_argument("paths", nargs="*", help="scan these files instead of tracked files")
    args = parser.parse_args(argv)

    denylist = load_denylist(Path(args.denylist))
    findings: list[Finding] = []
    if args.commit_msg:
        findings += scan_commit_msg(Path(args.commit_msg), denylist)
    if args.staged:
        findings += scan_staged(denylist)
    if args.paths:
        for name in args.paths:
            findings += scan_file(Path(name), denylist)
    if not (args.commit_msg or args.staged or args.paths):
        for name in tracked_files():
            findings += scan_file(Path(name), denylist, display=name)

    for name, number, reason in findings:
        print(f"{name}:{number}: {reason}")
    if findings:
        return 1
    print("leak-check: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
