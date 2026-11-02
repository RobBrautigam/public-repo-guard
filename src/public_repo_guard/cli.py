"""public-repo-guard: stop private names reaching a public GitHub repository.

    public-repo-guard hook                  Claude Code PreToolUse hook (stdin: the hook JSON; exit 2 refuses)
    public-repo-guard pre-push REMOTE URL   git pre-push hook (stdin: git's ref lines; exit 1 refuses)
    public-repo-guard install [DIR]         write the pre-push hook into a repository
    public-repo-guard scan-tree [DIR]       every tracked file of a folder, before you make it public
    public-repo-guard scan-range RANGE      the commits of a range, e.g. origin/main..HEAD
    public-repo-guard check-denylist        load the denylist and say how many patterns it holds
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from . import denylist as dl
from . import gitscan
from .command import Unreadable, parse
from .guard import Verdict, check_action, check_push
from .visibility import github_slug, visibility

HOOK_MARK = "# installed by public-repo-guard"


def _load() -> list[dl.Pattern]:
    return dl.load()


def cmd_hook(_args: argparse.Namespace) -> int:
    """Claude Code PreToolUse: exit 2 with the reason on stderr refuses the tool call."""
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(event, dict) or event.get("tool_name") not in ("Bash", "PowerShell"):
        return 0
    command = (event.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not command.strip():
        return 0
    cwd = event.get("cwd") or os.getcwd()
    try:
        actions = parse(command, cwd)
    except Unreadable:
        return 0  # the shell cannot run it either
    for action in actions:
        try:
            verdict = check_action(action, _load)
        except gitscan.GitError as exc:
            print(f"public-repo-guard: refused. git could not be read for this command: {exc}", file=sys.stderr)
            return 2
        if verdict.refused:
            print(verdict.render(), file=sys.stderr)
            return 2
    return 0


def cmd_pre_push(args: argparse.Namespace) -> int:
    """git pre-push: stdin lines are `<local ref> <local sha> <remote ref> <remote sha>`."""
    top = gitscan.toplevel(Path.cwd())
    if top is None:
        return 0
    tips, names = [], []
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[1] != gitscan.ZERO_SHA:
            tips.append(parts[1])
            names.append(parts[2].removeprefix("refs/heads/").removeprefix("refs/tags/"))
    if not tips:
        return 0
    verdict = check_push(top, args.remote, names, _load, visibility, local_tips=tips)
    if verdict.refused:
        print(verdict.render(), file=sys.stderr)
        return 1
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    top = gitscan.toplevel(Path(args.dir))
    if top is None:
        print(f"{args.dir} is not inside a git repository", file=sys.stderr)
        return 1
    hooks = Path(gitscan.git(top, "rev-parse", "--git-path", "hooks").strip())
    hooks = hooks if hooks.is_absolute() else top / hooks
    target = hooks / "pre-push"
    python = Path(sys.executable).as_posix()
    body = f'#!/bin/sh\n{HOOK_MARK}\nexec "{python}" -m public_repo_guard pre-push "$@"\n'
    if target.exists() and HOOK_MARK not in target.read_text(encoding="utf-8", errors="replace") and not args.force:
        print(f"{target} already exists and is not ours; add this line to it, or rerun with --force:\n"
              f'  "{python}" -m public_repo_guard pre-push "$@" || exit 1', file=sys.stderr)
        return 1
    hooks.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8", newline="\n")
    target.chmod(0o755)
    print(f"installed: {target}")
    return 0


def _report(findings: list[dl.Finding], what: str) -> int:
    if not findings:
        print(f"clean: no denylisted name in {what}")
        return 0
    print(Verdict(True, what, f"{len(findings)} denylisted name(s) in {what}:", findings).render())
    return 1


def cmd_scan_tree(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    return _report(gitscan.scan_tree(root, _load()), str(root))


def cmd_scan_range(args: argparse.Namespace) -> int:
    top = gitscan.toplevel(Path(args.repo))
    if top is None:
        print(f"{args.repo} is not inside a git repository", file=sys.stderr)
        return 1
    tips = [part for part in args.range.split("..") if part]
    if ".." in args.range and len(tips) == 2:
        findings = gitscan.scan_commits(top, [tips[1]], [tips[0]], _load())
    else:
        findings = gitscan.scan_commits(top, [args.range], [], _load())
    return _report(findings, f"the commits of {args.range}")


def cmd_check_denylist(_args: argparse.Namespace) -> int:
    patterns = _load()
    categories = sorted({p.category for p in patterns})
    print(f"{len(patterns)} pattern(s) in {dl.default_path()} ({', '.join(categories)})")
    return 0


def cmd_visibility(args: argparse.Namespace) -> int:
    slug = github_slug(args.target) or args.target
    print(visibility(slug))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="public-repo-guard", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook").set_defaults(fn=cmd_hook)
    p = sub.add_parser("pre-push")
    p.add_argument("remote")
    p.add_argument("url", nargs="?")
    p.set_defaults(fn=cmd_pre_push)
    p = sub.add_parser("install")
    p.add_argument("dir", nargs="?", default=".")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_install)
    p = sub.add_parser("scan-tree")
    p.add_argument("dir", nargs="?", default=".")
    p.set_defaults(fn=cmd_scan_tree)
    p = sub.add_parser("scan-range")
    p.add_argument("range")
    p.add_argument("--repo", default=".")
    p.set_defaults(fn=cmd_scan_range)
    sub.add_parser("check-denylist").set_defaults(fn=cmd_check_denylist)
    p = sub.add_parser("visibility", help="print public, private, internal, unknown or not-github")
    p.add_argument("target", help="owner/repo or a GitHub URL")
    p.set_defaults(fn=cmd_visibility)
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except dl.DenylistError as exc:
        print(f"public-repo-guard: {exc}", file=sys.stderr)
        return 1 if args.command != "hook" else 2


if __name__ == "__main__":
    sys.exit(main())
