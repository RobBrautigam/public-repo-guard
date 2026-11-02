"""Read a shell command an agent is about to run and find the actions that publish:
`git push`, `gh repo create --public`, `gh repo edit --visibility public`, and gh writes whose
words land on GitHub (pull request and issue titles, bodies, comments, release notes)."""
from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field

MAX_DEPTH = 3
_WRAPPERS = {"env", "nice", "nohup", "command", "exec", "time", "builtin", "sudo"}
_SHELLS = {"bash", "sh", "zsh", "dash", "bash.exe", "sh.exe"}
_POWERSHELLS = {"pwsh", "powershell", "pwsh.exe", "powershell.exe"}
_CD = {"cd", "pushd", "set-location", "sl", "chdir"}
_GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
_PUSH_OPTS_WITH_VALUE = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
_GH_TEXT = {
    "pr": {"create", "edit", "comment", "review", "close", "merge"},
    "issue": {"create", "edit", "comment", "close"},
    "release": {"create", "edit"},
}
_FILE_FLAGS = ("--body-file", "-F", "--notes-file")
_GH_GLOBAL_VALUES = {"-R", "--repo", "--hostname"}
_REPO_CREATE_VALUES = {"-d", "--description", "-h", "--homepage", "-s", "--source", "-r", "--remote",
                       "-t", "--team", "-p", "--template", "-g", "--gitignore", "-l", "--license",
                       "--visibility"}
_REPO_EDIT_VALUES = {"-d", "--description", "-h", "--homepage", "--visibility", "--add-topic",
                     "--remove-topic", "--default-branch"}
_API_VALUES = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input", "-H", "--header",
               "-q", "--jq", "-t", "--template", "--cache", "-p", "--preview"}


class Unreadable(Exception):
    """The command could not be split into words (an unclosed quote, for one)."""


@dataclass
class Action:
    kind: str                     # "push", "make_public", "gh_text"
    cwd: str
    verb: str = ""                # gh: "create", "edit", "api", ...
    remote: str | None = None     # push: the remote name or URL given, if any
    refs: list[str] = field(default_factory=list)   # push: refspecs, or --all / --tags / --mirror
    repo: str | None = None       # gh: the -R / --repo value or the positional repo
    source: str | None = None     # make_public: the folder whose history becomes public
    words: list[str] = field(default_factory=list)  # gh: every word that would land on GitHub
    files: list[str] = field(default_factory=list)  # gh: files whose contents would land


_SEPARATORS = set(";&|()")
_BLOCK_WORDS = {"{", "}"}
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!"}


def _clauses(command: str) -> list[list[str]]:
    """Words per simple command: split on ; & | ( ) and on bare { } (bash groups, PowerShell blocks)."""
    text = command.replace("\r\n", "\n").replace("\n", " ; ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise Unreadable(str(exc)) from None
    clauses: list[list[str]] = [[]]
    for token in tokens:
        if (token and set(token) <= _SEPARATORS) or token in _BLOCK_WORDS:
            clauses.append([])
        else:
            clauses[-1].append(token)
    return [c for c in clauses if c]


def _strip_prefixes(words: list[str]) -> list[str]:
    i = 0
    while i < len(words):
        word = words[i]
        name = os.path.basename(word).lower()
        if word in _KEYWORDS:
            i += 1  # if git push; then ... / until git push; do ...
        elif "=" in word and not word.startswith("-") and word.split("=", 1)[0].replace("_", "").isalnum():
            i += 1  # FOO=bar before the command
        elif name in _WRAPPERS:
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
        elif name == "timeout":
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
            i += 1  # the duration
        else:
            break
    return words[i:]


def _resolve(cwd: str, path: str) -> str:
    path = os.path.expanduser(path)
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path))


def parse(command: str, cwd: str, depth: int = 0) -> list[Action]:
    """Every publishing action in `command`, each with the folder it runs in."""
    actions: list[Action] = []
    for clause in _clauses(command):
        words = _strip_prefixes(clause)
        if not words:
            continue
        head = os.path.basename(words[0]).lower()
        if head in _CD:
            targets = [w for w in words[1:] if not w.startswith("-")]
            if targets:
                cwd = _resolve(cwd, targets[-1])
            continue
        if head in _SHELLS and depth < MAX_DEPTH:
            for i, word in enumerate(words[1:-1], 1):
                if word.startswith("-") and "c" in word.lstrip("-") and len(word) <= 4:
                    actions += parse(words[i + 1], cwd, depth + 1)
                    break
            continue
        if head in _POWERSHELLS and depth < MAX_DEPTH:
            for i, word in enumerate(words[1:-1], 1):
                if word.lower() in ("-command", "-c"):
                    actions += parse(" ".join(words[i + 1:]), cwd, depth + 1)
                    break
            continue
        if head in ("cmd", "cmd.exe") and depth < MAX_DEPTH:
            for i, word in enumerate(words[1:-1], 1):
                if word.lower() in ("/c", "/k"):
                    actions += parse(" ".join(words[i + 1:]), cwd, depth + 1)
                    break
            continue
        if head in ("git", "git.exe"):
            action = _git(words[1:], cwd)
            if action:
                actions.append(action)
        elif head in ("gh", "gh.exe"):
            action = _gh(words[1:], cwd)
            if action:
                actions.append(action)
    return actions


def _git(args: list[str], cwd: str) -> Action | None:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        option = args[i].split("=", 1)[0]
        if option == "-C" and i + 1 < len(args):
            cwd = _resolve(cwd, args[i + 1])
            i += 2
        elif option in _GIT_OPTS_WITH_VALUE and "=" not in args[i]:
            i += 2
        else:
            i += 1
    if i >= len(args) or args[i] != "push":
        return None
    rest = args[i + 1:]
    if "--delete" in rest or "-d" in rest:
        return None  # deleting a remote branch or tag publishes nothing
    positional: list[str] = []
    refs: list[str] = []
    j = 0
    while j < len(rest):
        word = rest[j]
        if word in ("--all", "--tags", "--mirror", "--branches"):
            refs.append(word)
        elif word.startswith("--repo="):
            positional.insert(0, word.split("=", 1)[1])
        elif word in _PUSH_OPTS_WITH_VALUE:
            if word == "--repo" and j + 1 < len(rest):
                positional.insert(0, rest[j + 1])
            j += 1
        elif word.startswith("-") or ">" in word or "<" in word:
            pass
        else:
            positional.append(word)
        j += 1
    remote = positional[0] if positional else None
    refs += positional[1:]
    return Action("push", cwd, remote=remote, refs=refs)


def _flag_value(args: list[str], *names: str) -> str | None:
    for i, word in enumerate(args):
        for name in names:
            if word == name and i + 1 < len(args):
                return args[i + 1]
            if name.startswith("--") and word.startswith(name + "="):
                return word.split("=", 1)[1]
            if not name.startswith("--") and word.startswith(name) and len(word) > len(name):
                return word[len(name):]  # -s. or -sC:/dir
    return None


def _positionals(words: list[str], value_flags: set[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        if word.startswith("-"):
            skip = word in value_flags  # "--flag=value" carries its own value
            continue
        out.append(word)
    return out


def _gh(args: list[str], cwd: str) -> Action | None:
    words = list(args)
    repo = _flag_value(words, "--repo", "-R")
    head = _positionals(words, _GH_GLOBAL_VALUES)
    if len(head) < 2:
        return None
    group, verb = head[0], head[1]
    if group == "repo" and verb == "create":
        if "--public" not in words and _flag_value(words, "--visibility") != "public":
            return None
        source = _flag_value(words, "--source", "-s")
        rest = _positionals(words, _GH_GLOBAL_VALUES | _REPO_CREATE_VALUES)
        name = rest[2] if len(rest) > 2 else None
        return Action("make_public", cwd, verb="create", repo=name, words=words,
                      source=_resolve(cwd, source) if source else None)
    if group == "repo" and verb == "edit":
        rest = _positionals(words, _GH_GLOBAL_VALUES | _REPO_EDIT_VALUES)
        target = rest[2] if len(rest) > 2 else repo
        if _flag_value(words, "--visibility") == "public":
            return Action("make_public", cwd, verb="edit", repo=target, words=words)
        return Action("gh_text", cwd, repo=target, words=words)
    if group in _GH_TEXT and verb in _GH_TEXT[group]:
        files = [v for v in (_flag_value(words, f) for f in _FILE_FLAGS) if v and v != "-"]
        return Action("gh_text", cwd, repo=repo, words=words, files=[_resolve(cwd, f) for f in files])
    if group == "api":
        method = (_flag_value(words, "-X", "--method") or "").upper()
        has_fields = any(w in ("-f", "-F", "--field", "--raw-field", "--input") or
                         w.startswith(("--field=", "--raw-field=", "--input=")) for w in words)
        if method in ("", "GET") and not has_fields:
            return None
        rest = _positionals(words, _GH_GLOBAL_VALUES | _API_VALUES)
        path = rest[1] if len(rest) > 1 else ""
        parts = path.strip("/").split("/")
        target = f"{parts[1]}/{parts[2]}" if len(parts) >= 3 and parts[0] == "repos" else None
        joined = " ".join(words).lower()
        if target and ("private=false" in joined or "visibility=public" in joined):
            return Action("make_public", cwd, verb="api", repo=target, words=words)
        body = _flag_value(words, "--input")
        files = [_resolve(cwd, body)] if body and body != "-" else []
        return Action("gh_text", cwd, repo=target or repo, words=words, files=files)
    return None
