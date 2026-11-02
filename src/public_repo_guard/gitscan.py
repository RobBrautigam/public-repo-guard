"""What a push would publish: the commits GitHub does not hold yet, read as messages, added
lines and added file paths. Binary files are skipped (git prints no lines for them)."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .denylist import Finding, Pattern, scan_lines, scan_name, scan_text
from .visibility import NO_WINDOW

ZERO_SHA = "0" * 40
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_START = "\x1epublic-repo-guard commit "
_END = "\x1epublic-repo-guard end"
# git writes the 0x1e separators itself (%x1e): a control character in argv does not survive
# every platform's command line
_FORMAT = "--format=%x1epublic-repo-guard commit %H%n%B%n%x1epublic-repo-guard end"


class GitError(Exception):
    """A git command failed; the caller decides whether that refuses (public) or passes."""


def git(cwd: str | Path, *args: str, check: bool = True, timeout: int = 60) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "-c", "core.quotePath=false", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {' '.join(args[:2])} could not run: {exc}") from None
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} failed: {result.stderr.strip()[:300]}")
    return result.stdout


def toplevel(cwd: str | Path) -> Path | None:
    try:
        out = git(cwd, "rev-parse", "--show-toplevel").strip()
    except GitError:
        return None
    return Path(out) if out else None


def current_branch(top: Path) -> str | None:
    out = git(top, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip()
    return out or None


def config(top: Path, key: str) -> str | None:
    out = git(top, "config", "--get", key, check=False).strip()
    return out or None


def push_remote(top: Path) -> str:
    """The remote a bare `git push` uses: pushRemote, pushDefault, the branch's remote, origin."""
    branch = current_branch(top)
    if branch:
        for key in (f"branch.{branch}.pushRemote", "remote.pushDefault", f"branch.{branch}.remote"):
            value = config(top, key)
            if value and value != ".":
                return value
    remotes = git(top, "remote", check=False).split()
    if len(remotes) == 1:
        return remotes[0]
    return "origin"


def remote_url(top: Path, remote: str) -> str | None:
    """The push URL of a named remote, or the argument itself when it is already a URL or path."""
    url = config(top, f"remote.{remote}.pushurl") or config(top, f"remote.{remote}.url")
    if url:
        return url
    if "/" in remote or ":" in remote or "\\" in remote:
        return remote
    return None


def remote_shas(top: Path, remote: str) -> set[str]:
    """Every commit the remote holds, read from the remote itself (tracking refs go stale)."""
    out = git(top, "ls-remote", remote, timeout=60)
    return {line.split("\t", 1)[0] for line in out.splitlines() if "\t" in line}


def present_locally(top: Path, shas: set[str]) -> list[str]:
    """The subset of `shas` this clone has, so `--not` can exclude them."""
    if not shas:
        return []
    result = subprocess.run(
        ["git", "-C", str(top), "cat-file", "--batch-check"],
        input="\n".join(sorted(shas)) + "\n", capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60, creationflags=NO_WINDOW,
    )
    have = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] != "missing":
            have.append(parts[0])
    return have


def scan_commits(top: Path, tips: list[str], exclude: list[str], patterns: list[Pattern]) -> list[Finding]:
    """Scan every commit reachable from `tips` and not from `exclude`."""
    if not tips:
        return []
    args = ["log", "-p", "-U0", "--no-color", "--no-ext-diff", "--no-renames",
            _FORMAT, *tips, "--not", *exclude, "--"]
    try:
        # remerge: a merge shows only what its resolution added beyond the automatic merge
        out = git(top, args[0], "--diff-merges=remerge", *args[1:], timeout=300)
    except GitError:
        out = git(top, *args, timeout=300)  # git older than 2.36
    return parse_log(out, patterns)


def parse_log(out: str, patterns: list[Pattern]) -> list[Finding]:
    findings: list[Finding] = []
    sha = ""
    message: list[str] = []
    in_message = False
    path: str | None = None
    prev_minus_null = False
    new_line = 0
    in_hunk = False

    # split on newlines only: str.splitlines() also breaks on 0x1e, the marker's own first byte
    for raw in out.split("\n"):
        if raw.startswith(_START):
            sha = raw[len(_START):].strip()
            message, in_message, path, in_hunk = [], True, None, False
            continue
        if in_message:
            if raw == _END:
                in_message = False
                findings += scan_text("\n".join(message), patterns, f"commit {sha[:7]} message")
            else:
                message.append(raw)
            continue
        if raw.startswith("diff --git "):
            path, in_hunk, prev_minus_null = None, False, False
            continue
        if not in_hunk and raw.startswith("--- "):
            prev_minus_null = raw == "--- /dev/null"
            continue
        if not in_hunk and raw.startswith("+++ "):
            target = raw[4:]
            path = None if target == "/dev/null" else target[2:] if target.startswith("b/") else target
            if path and prev_minus_null:
                findings += scan_name(path, patterns, f"{path} (file name, commit {sha[:7]})")
            continue
        hunk = _HUNK.match(raw)
        if hunk:
            new_line, in_hunk = int(hunk.group(1)), True
            continue
        if in_hunk and raw.startswith("+"):
            findings += scan_lines(
                [(new_line, raw[1:])], patterns, f"{path} (commit {sha[:7]})", path=path
            )
            new_line += 1
    return findings


def scan_tree(root: Path, patterns: list[Pattern]) -> list[Finding]:
    """Every tracked text file under a repository (or every file under a plain folder)."""
    top = toplevel(root)
    if top:
        names = [n for n in git(top, "ls-files", "-z").split("\0") if n]
        base = top
    else:
        base = root
        names = [str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file()]
    findings: list[Finding] = []
    for name in names:
        findings += scan_name(name, patterns, f"{name} (file name)")
        try:
            data = (base / name).read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue  # binary, the same test git uses
        findings += scan_text(data.decode("utf-8", errors="replace"), patterns, name, path=name)
    return findings
