"""Leak checker tests.

Anything that looks like a real email address or home path is built at runtime, so that the
repo-wide scan of this file stays clean.
"""

import subprocess

import leak_check

REAL_EMAIL = "a@" + "webmail.test"
HOME_PATH = "/ho" + "me/alice/x"


def run(tmp_path, text, *extra, denylist=None):
    target = tmp_path / "sample.txt"
    target.write_text(text, encoding="utf-8")
    deny = tmp_path / "denylist" if denylist is None else denylist
    return leak_check.main([*extra, "--denylist", str(deny), str(target)])


def test_flags_real_email(tmp_path, capsys):
    assert run(tmp_path, f"contact {REAL_EMAIL} today\n") == 1
    out = capsys.readouterr().out
    assert "email" in out
    assert "sample.txt:1:" in out
    assert REAL_EMAIL not in out


def test_allows_example_and_noreply(tmp_path):
    text = "organizer@example.com, b@example.org, c@example.net, x@users.noreply.github.com\n"
    assert run(tmp_path, text) == 0


def test_flags_home_path(tmp_path, capsys):
    assert run(tmp_path, f"see {HOME_PATH}\n") == 1
    assert "home path" in capsys.readouterr().out


def test_allows_home_placeholder(tmp_path):
    assert run(tmp_path, "see /home/<user>/project\n") == 0


def test_denylist_case_insensitive(tmp_path, capsys):
    deny = tmp_path / "deny.txt"
    deny.write_text("# comment\n\nSecret School\n", encoding="utf-8")
    assert run(tmp_path, "we met at secret school\n", denylist=deny) == 1
    out = capsys.readouterr().out
    assert "denylist" in out
    assert "secret" not in out.lower()  # the term itself is not echoed


def test_missing_denylist_is_ok(tmp_path):
    assert not (tmp_path / "denylist").exists()
    assert run(tmp_path, "nothing to see here\n") == 0


def test_commit_msg_mode(tmp_path, capsys):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(f"feat: thing\n\nthanks {REAL_EMAIL}\n# {HOME_PATH} is a comment\n")
    deny = tmp_path / "denylist"
    assert leak_check.main(["--commit-msg", str(msg), "--denylist", str(deny)]) == 1
    out = capsys.readouterr().out
    assert "email" in out and "COMMIT_EDITMSG:3:" in out
    assert "home path" not in out  # git comment lines are ignored


def test_commit_msg_clean(tmp_path):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("feat: thing\n\nClaude-Session: https://claude.example.com/s/1\n")
    assert leak_check.main(["--commit-msg", str(msg), "--denylist", str(tmp_path / "d")]) == 0


def test_skips_binary_files(tmp_path):
    target = tmp_path / "blob.bin"
    target.write_bytes(b"\x00\x01" + REAL_EMAIL.encode())
    assert leak_check.main(["--denylist", str(tmp_path / "d"), str(target)]) == 0


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # noqa: S603, S607


def make_repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.com")


def test_staged_mode_scans_added_lines_only(tmp_path, monkeypatch, capsys):
    make_repo(tmp_path)
    old = tmp_path / "old.txt"
    old.write_text(f"{REAL_EMAIL}\n")  # committed before the check existed
    git(tmp_path, "add", "old.txt")
    git(tmp_path, "commit", "-q", "-m", "init")
    old.write_text(f"{REAL_EMAIL}\nclean line\n")
    (tmp_path / "new.txt").write_text(f"ok\nsee {HOME_PATH}\n")
    git(tmp_path, "add", "old.txt", "new.txt")
    monkeypatch.chdir(tmp_path)
    assert leak_check.main(["--staged"]) == 1
    out = capsys.readouterr().out
    assert "new.txt:2: home path" in out
    assert "old.txt" not in out


def test_default_mode_scans_tracked_files(tmp_path, monkeypatch, capsys):
    make_repo(tmp_path)
    (tmp_path / "a.txt").write_text(f"{REAL_EMAIL}\n")
    (tmp_path / "uv.lock").write_text(f"{REAL_EMAIL}\n")  # skipped by name
    (tmp_path / "untracked.txt").write_text(f"{REAL_EMAIL}\n")
    git(tmp_path, "add", "a.txt", "uv.lock")
    monkeypatch.chdir(tmp_path)
    assert leak_check.main([]) == 1
    out = capsys.readouterr().out
    assert "a.txt:1:" in out
    assert "uv.lock" not in out and "untracked" not in out
