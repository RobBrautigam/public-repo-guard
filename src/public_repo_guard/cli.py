"""public-repo-guard: stop private names reaching a public GitHub repository.

    public-repo-guard hook                  Claude Code PreToolUse hook (stdin: the hook JSON; exit 2 refuses)
    public-repo-guard pre-push REMOTE URL   git pre-push hook (stdin: git's ref lines; exit 1 refuses)
    public-repo-guard install [DIR]         write the pre-push hook into a repository
    public-repo-guard scan-tree [DIR]       every tracked file of a folder, before you make it public
    public-repo-guard scan-range RANGE      the commits of a range (origin/main..HEAD), or --all
    public-repo-guard check-denylist        load the denylist and say how many patterns it holds
    public-repo-guard visibility TARGET     public, private, internal, unknown or not-github
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from . import __version__
from . import denylist as dl
from . import gitscan
from .command import Unreadable, mentions_publishing, parse
from .guard import Verdict, _answer, check_action, check_push
from .visibility import repo_arg_slug, visibility

HOOK_MARK = "# installed by public-repo-guard"
_JSON_KINDS = {str: "a string", list: "an array", dict: "an object", bool: "a boolean", int: "a number",
               float: "a number"}
#: an unreadable event's text longer than this is read loosely: refused when it names git or gh at all,
#: because the publishing pattern backtracks on one long line of git words, and a hook that times out
#: lets the command run
_CLOSE_READ_LIMIT = 20_000
_GIT_OR_GH = re.compile(r"\b(?:git|gh)(?:\.exe)?\b", re.IGNORECASE)
_JSON_ESCAPE = re.compile(r'\\(?:u([0-9a-fA-F]{4})|(["\\/bfnrt]))')
_JSON_CHARS = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _kind(value: object) -> str:
    return _JSON_KINDS.get(type(value), type(value).__name__)


def _read_stdin() -> tuple[str, bool]:
    """The hook event as the stream's own encoding reads it, and True; bytes that encoding cannot
    decode come back read as UTF-8 with replacements, and False, so their text is still scanned."""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:  # a text stream with no bytes beneath it
        return sys.stdin.read(), True
    data = stream.read()
    try:
        return data.decode(sys.stdin.encoding or "utf-8", sys.stdin.errors or "strict"), True
    except (UnicodeDecodeError, LookupError):
        return data.decode("utf-8", "replace"), False


def _event_text(raw: str, event: object) -> str:
    """The event as it came, then its words decoded, so an escape (a tab written as two characters, a
    letter as a code point) and a list of words (`["gh", "pr", "create"]`) read as a shell would:
    every string of a parsed event joined by spaces in order, or, for text the json module could not
    read, the same text with JSON's escapes undone and its punctuation made spaces."""
    if event is None:
        undone = _JSON_ESCAPE.sub(lambda m: chr(int(m[1], 16)) if m[1] else _JSON_CHARS.get(m[2], m[2]), raw)
        return raw + "\n" + re.sub(r'["\[\]{},:]', " ", undone)
    words: list[str] = []
    stack = [event]
    while stack:  # no recursion: the json module reads nesting deeper than a recursive walk could
        value = stack.pop()
        if isinstance(value, str):
            words.append(value)
        elif isinstance(value, dict):
            stack.extend(reversed([part for pair in value.items() for part in pair]))
        elif isinstance(value, list):
            stack.extend(reversed(value))
    return raw + "\n" + " ".join(words)


def _unreadable_event(raw: str, event: object, why: str) -> int:
    """An event the hook cannot read is refused when its text looks like it publishes, else passed:
    an exit of 1 would let the command run unchecked."""
    text = _event_text(raw, event)
    if len(text) > _CLOSE_READ_LIMIT:
        if not _GIT_OR_GH.search(text):
            return 0
        looks = "it is too long to read closely and names git or gh"
    elif mentions_publishing(text):
        looks = "it looks like it publishes"
    else:
        return 0
    print(f"public-repo-guard: refused. the hook event could not be read ({why}) and {looks}; "
          f"nothing was checked, so it does not run", file=sys.stderr)
    return 2


def cmd_hook(_args: argparse.Namespace) -> int:
    """Claude Code PreToolUse: exit 2 with the reason on stderr refuses the tool call."""
    raw, decoded = _read_stdin()
    try:
        event = json.loads(raw or "{}")
    except RecursionError:
        return _unreadable_event(raw, None, "it is nested too deeply to read")
    except ValueError:
        return _unreadable_event(raw, None, "it could not be parsed as JSON")
    if not decoded:
        return _unreadable_event(raw, event, "its bytes could not be decoded")
    if not isinstance(event, dict):
        return _unreadable_event(raw, event, "it is not a JSON object")
    tool_name = event.get("tool_name")
    if tool_name is not None and not isinstance(tool_name, str):
        return _unreadable_event(raw, event, f"its tool_name is {_kind(tool_name)}, not a string")
    if tool_name not in ("Bash", "PowerShell"):
        return 0
    tool_input = event.get("tool_input")
    if tool_input is not None and not isinstance(tool_input, dict):
        return _unreadable_event(raw, event, f"its tool_input is {_kind(tool_input)}, not an object")
    command = (tool_input or {}).get("command")
    if command is not None and not isinstance(command, str):
        return _unreadable_event(raw, event, f"its command is {_kind(command)}, not a string")
    if not isinstance(command, str) or not command.strip():
        return 0
    cwd = event.get("cwd") or os.getcwd()
    if not isinstance(cwd, str):
        return _unreadable_event(raw, event, f"its cwd is {_kind(cwd)}, not a string")
    try:
        actions = parse(command, cwd, powershell=event.get("tool_name") == "PowerShell")
    except Unreadable as exc:
        if not mentions_publishing(command):
            return 0
        print(f"public-repo-guard: refused. this command looks like it publishes and could not be read ({exc}); "
              f"run the push or the gh command on its own line", file=sys.stderr)
        return 2
    for action in actions:
        try:
            verdict = check_action(action, dl.load)
        except gitscan.GitError as exc:
            print(f"public-repo-guard: refused. git could not be read for this command: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 - any other failure on a publishing command refuses it
            print(f"public-repo-guard: refused. the check failed ({type(exc).__name__}: {exc})", file=sys.stderr)
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
        if len(parts) == 4 and not gitscan.is_zero(parts[1]):
            tips.append(parts[1])
            names.append(parts[2].removeprefix("refs/heads/").removeprefix("refs/tags/"))
    if not tips:
        return 0
    verdict = check_push(top, args.remote, names, dl.load, visibility, local_tips=tips,
                         extra_urls=[args.url] if args.url else None)
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
    return _report(gitscan.scan_tree(root, dl.load()), str(root))


def cmd_scan_range(args: argparse.Namespace) -> int:
    top = gitscan.toplevel(Path(args.repo))
    if top is None:
        print(f"{args.repo} is not inside a git repository", file=sys.stderr)
        return 1
    if args.all:
        patterns = dl.load()
        findings = gitscan.scan_commits(top, ["--branches", "--tags"], [], patterns)
        for name in gitscan.git(top, "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/tags").split():
            findings += dl.scan_name(name, patterns, f"branch or tag name {name!r}")
        return _report(findings, "every branch and tag, from the first commit")
    if not args.range:
        print("give a range (origin/main..HEAD), one revision (HEAD: its whole history), or --all", file=sys.stderr)
        return 1
    if "..." in args.range:
        print("use A..B (the commits in B and not in A); A...B is not supported", file=sys.stderr)
        return 1
    tips = [part for part in args.range.split("..") if part]
    if ".." in args.range and len(tips) == 2:
        findings = gitscan.scan_commits(top, [tips[1]], [tips[0]], dl.load())
    else:
        findings = gitscan.scan_commits(top, [args.range], [], dl.load())
    return _report(findings, f"the commits of {args.range}")


def cmd_check_denylist(_args: argparse.Namespace) -> int:
    patterns = dl.load()
    categories = sorted({p.category for p in patterns})
    print(f"{len(patterns)} pattern(s) in {dl.default_path()} ({', '.join(categories)})")
    return 0


def cmd_visibility(args: argparse.Namespace) -> int:
    """A GitHub URL is looked up, and so is a target of two plain parts, a/b (letters, digits, dots,
    dashes, underscores), read as owner/repo, so a local path of that shape is written ./a/b; any
    other target is answered as the hooks answer its URL (not-github, or unknown for a host this
    tool cannot place), with no lookup."""
    slug = repo_arg_slug(args.target)
    if slug and not slug.split("/", 1)[0].strip("."):
        slug = None  # ./x or ../x is a relative path: no GitHub owner is named with dots only
    print(visibility(slug) if slug else _answer(args.target, visibility)[1])
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
    p.add_argument("range", nargs="?")
    p.add_argument("--all", action="store_true", help="every branch and tag, from the first commit")
    p.add_argument("--repo", default=".")
    p.set_defaults(fn=cmd_scan_range)
    sub.add_parser("check-denylist").set_defaults(fn=cmd_check_denylist)
    p = sub.add_parser("visibility", help="print public, private, internal, unknown or not-github")
    p.add_argument("target", help="owner/repo or a remote URL; a GitHub URL or a plain a/b is looked up "
                                  "on GitHub (write a local path of that shape as ./a/b)")
    p.set_defaults(fn=cmd_visibility)
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except (dl.DenylistError, gitscan.GitError) as exc:
        print(f"public-repo-guard: {exc}", file=sys.stderr)
        return 1 if args.command != "hook" else 2


if __name__ == "__main__":
    sys.exit(main())
