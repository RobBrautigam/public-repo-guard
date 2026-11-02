"""The private denylist: load it, and find its patterns in text.

The denylist is a TOML file that lives OUTSIDE every public repository (by default
`~/.config/public-repo-guard/denylist.toml`). Each entry names something that must never be
published: a client, a company, a person, an internal host, a home-folder path.

    [[pattern]]
    id = "client-acme"            # shown in a refusal; the regex never is
    category = "client"
    regex = 'acme[\\s_.-]*widgets'
    allow_paths = ["LICENSE"]     # optional globs where this pattern may appear
"""
from __future__ import annotations

import fnmatch
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ENV_PATH = "PUBLIC_REPO_GUARD_DENYLIST"


class DenylistError(Exception):
    """The denylist is missing, unreadable, or holds a pattern that cannot work."""


@dataclass(frozen=True)
class Pattern:
    id: str
    category: str
    regex: re.Pattern[str]
    allow_paths: tuple[str, ...] = ()

    def allowed_in(self, path: str | None) -> bool:
        if not path or not self.allow_paths:
            return False
        p = path.replace("\\", "/")
        return any(fnmatch.fnmatch(p, glob) for glob in self.allow_paths)


@dataclass(frozen=True)
class Finding:
    where: str
    pattern_id: str
    category: str
    matched: str

    def render(self) -> str:
        return f"{self.where}: {self.category} ({self.pattern_id}) matched {self.matched!r}"


def default_path() -> Path:
    env = os.environ.get(ENV_PATH)
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "public-repo-guard" / "denylist.toml"


def load(path: Path | None = None) -> list[Pattern]:
    """Read and compile the denylist. Raises DenylistError with a reason a person can act on."""
    path = path or default_path()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DenylistError(f"no denylist at {path} (set {ENV_PATH} or create the file)") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise DenylistError(f"cannot read the denylist at {path}: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise DenylistError(f"the denylist at {path} is not valid TOML: {exc}") from None

    patterns: list[Pattern] = []
    seen: set[str] = set()
    for index, raw in enumerate(data.get("pattern", []), 1):
        if not isinstance(raw, dict):
            raise DenylistError(f"entry {index} is not a [[pattern]] table")
        pid = str(raw.get("id") or f"pattern-{index}")
        if pid in seen:
            raise DenylistError(f"pattern id {pid!r} is used twice")
        seen.add(pid)
        source = raw.get("regex")
        if not isinstance(source, str) or not source:
            raise DenylistError(f"pattern {pid!r} has no regex")
        flags = 0 if raw.get("case_sensitive") else re.IGNORECASE
        try:
            compiled = re.compile(source, flags)
        except re.error as exc:
            raise DenylistError(f"pattern {pid!r} does not compile: {exc}") from None
        if compiled.search(""):
            raise DenylistError(f"pattern {pid!r} matches empty text, so it would refuse everything")
        allow = raw.get("allow_paths", [])
        if isinstance(allow, str):
            allow = [allow]
        patterns.append(Pattern(pid, str(raw.get("category", "private")), compiled, tuple(allow)))
    if not patterns:
        raise DenylistError(f"the denylist at {path} holds no [[pattern]] entries")
    return patterns


def scan_lines(
    lines: Iterable[tuple[int, str]],
    patterns: list[Pattern],
    label: str,
    path: str | None = None,
) -> list[Finding]:
    """Every pattern hit in (line number, text) pairs. `label` names the source in a finding."""
    found: list[Finding] = []
    active = [p for p in patterns if not p.allowed_in(path)]
    if not active:
        return found
    for number, text in lines:
        for pattern in active:
            match = pattern.regex.search(text)
            if match:
                found.append(Finding(f"{label}, line {number}", pattern.id, pattern.category, match.group(0)))
    return found


def scan_text(text: str, patterns: list[Pattern], label: str, path: str | None = None) -> list[Finding]:
    # newlines only, as git and editors number lines (str.splitlines() also breaks on 0x1c-0x1e)
    lines = text.replace("\r\n", "\n").split("\n")
    return scan_lines(enumerate(lines, 1), patterns, label, path)


def scan_name(name: str, patterns: list[Pattern], label: str) -> list[Finding]:
    """A file name or a branch name: one finding per pattern, labeled as given."""
    found: list[Finding] = []
    for pattern in patterns:
        if pattern.allowed_in(name):
            continue
        match = pattern.regex.search(name)
        if match:
            found.append(Finding(label, pattern.id, pattern.category, match.group(0)))
    return found
