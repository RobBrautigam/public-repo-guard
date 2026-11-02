"""What a push would publish: the commits GitHub does not hold yet, read as messages, added
lines and added file paths. The contents of binary files are skipped (git prints no lines for
them); their names are read."""
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


def git(cwd: str | Path, *args: str, check: bool = True, timeout: int = 60, input: str | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "-c", "core.quotePath=false", *args],
            input=input, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {' '.join(args[:2])} could not run: {exc}") from None
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} failed: {result.stderr.strip()[:300]}")
    return result.stdout


def is_zero(sha: str) -> bool:
    """git's id for "no commit" (40 zeros, or 64 in a SHA-256 repository)."""
    return bool(sha) and set(sha) == {"0"}


def toplevel(cwd: str | Path) -> Path | None:
    """The working tree's top folder, or the repository folder itself when it is bare."""
    try:
        out = git(cwd, "rev-parse", "--show-toplevel").strip()
    except GitError:
        out = ""
    if out:
        return Path(out)
    try:
        if git(cwd, "rev-parse", "--is-bare-repository").strip() == "true":
            return Path(git(cwd, "rev-parse", "--absolute-git-dir").strip())
    except GitError:
        pass
    return None


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


def push_urls(top: Path, remote: str) -> list[str]:
    """Every URL a push to `remote` goes to, as git resolves them (insteadOf and pushInsteadOf
    applied, every pushurl), or the argument itself when it is already a URL or path."""
    out = git(top, "remote", "get-url", "--push", "--all", remote, check=False)
    urls = [line.strip() for line in out.splitlines() if line.strip()]
    if urls:
        return urls
    if "/" in remote or ":" in remote or "\\" in remote:
        return [remote]
    return []


def remote_url(top: Path, remote: str) -> str | None:
    """The first push URL of a remote (see push_urls)."""
    urls = push_urls(top, remote)
    return urls[0] if urls else None


def gh_default_remote(top: Path) -> str | None:
    """The remote `gh repo set-default` chose (git config remote.<name>.gh-resolved = base)."""
    out = git(top, "config", "--get-regexp", r"^remote\..*\.gh-resolved$", check=False)
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        if value.strip() == "base":
            return key[len("remote."):-len(".gh-resolved")]
    return None


def remote_shas(top: Path, remote: str) -> set[str]:
    """Every commit the remote holds, read from the remote itself (tracking refs go stale)."""
    out = git(top, "ls-remote", remote, timeout=60)
    return {line.split("\t", 1)[0] for line in out.splitlines() if "\t" in line}


def present_locally(top: Path, shas: set[str]) -> list[str]:
    """The subset of `shas` this clone has, so `--not` can exclude them."""
    if not shas:
        return []
    out = git(top, "cat-file", "--batch-check", input="\n".join(sorted(shas)) + "\n")
    have = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] != "missing":
            have.append(parts[0])
    return have


def scan_commits(top: Path, tips: list[str], exclude: list[str], patterns: list[Pattern]) -> list[Finding]:
    """Scan every commit reachable from `tips` and not from `exclude`."""
    if not tips:
        return []
    # the exclusions go in on stdin: a remote with many refs would overflow a command line
    args = ["log", "-p", "-U0", "--no-color", "--no-ext-diff", "--no-renames", _FORMAT, *tips, "--stdin", "--"]
    excluded = "".join(f"^{sha}\n" for sha in exclude)
    try:
        # remerge: a merge shows only what its resolution added beyond the automatic merge
        out = git(top, args[0], "--diff-merges=remerge", *args[1:], timeout=120, input=excluded)
    except GitError as exc:
        if "diff-merges" not in str(exc):
            raise
        out = git(top, *args, timeout=120, input=excluded)  # git older than 2.36
    return parse_log(out, patterns)


def _header_path(line: str) -> str:
    """The path in `diff --git a/P b/P` (both halves are equal for an added file)."""
    rest = line[len("diff --git "):]
    if rest.startswith('"'):  # git quotes a name holding a quote, a tab or a newline
        at = rest.rfind(' "b/')
        return rest[at + 4:].rstrip('"') if at >= 0 else rest
    half = (len(rest) - 1) // 2
    if rest[:half][2:] == rest[half + 1:][2:]:
        return rest[half + 1:][2:]
    at = rest.find(" b/")
    return rest[at + 3:] if at >= 0 else rest


def parse_log(out: str, patterns: list[Pattern]) -> list[Finding]:
    findings: list[Finding] = []
    sha = ""
    message: list[str] = []
    in_message = False
    path: str | None = None
    header = ""
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
            path, in_hunk, header = None, False, raw
            continue
        if not in_hunk and raw.startswith("new file mode ") and header:
            # read from the header: a binary or empty file has no "+++" line to read it from
            name = _header_path(header)
            findings += scan_name(name, patterns, f"{name} (file name, commit {sha[:7]})")
            continue
        if not in_hunk and raw.startswith("--- "):
            continue
        if not in_hunk and raw.startswith("+++ "):
            target = raw[4:]
            path = None if target == "/dev/null" else target[2:] if target.startswith("b/") else target
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
