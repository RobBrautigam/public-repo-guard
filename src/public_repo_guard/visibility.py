"""Is a GitHub repository public? Read at the moment of action, cached for ten minutes.

Order: the cache, then the GitHub CLI (`gh api`, which sees your private repositories when you
are signed in), then GitHub's public REST API with no token. A 404 from the public API means the
world cannot see the repository, which for this tool is the same as private.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

PUBLIC = "public"
PRIVATE = "private"
INTERNAL = "internal"
UNKNOWN = "unknown"
NOT_GITHUB = "not-github"

ENV_FORCE = "PUBLIC_REPO_GUARD_VISIBILITY"
ENV_CACHE = "PUBLIC_REPO_GUARD_CACHE"
TTL_SECONDS = 600

_SLUG = re.compile(
    r"^(?:https?://(?:[^@/]+@)?(?:www\.)?github\.com(?::\d+)?/"
    r"|ssh://(?:[^@/]+@)?(?:ssh\.)?github\.com(?::\d+)?/"
    r"|(?:[^@/:]+@)?(?:ssh\.)?github\.com:)"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_PLAIN_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SCP = re.compile(r"^(?:[^@/:]+@)?(?P<host>[^@/:]+):(?!//)")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
# hosts that are known not to be GitHub; any other host (an SSH alias, a self-hosted server) is unknown
OTHER_HOSTS = ("gitlab.com", "bitbucket.org", "codeberg.org", "dev.azure.com", "ssh.dev.azure.com",
               "vs-ssh.visualstudio.com", "git.sr.ht", "gitea.com", "huggingface.co")

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def github_slug(url: str | None) -> str | None:
    """`owner/repo` for a GitHub remote URL (https, ssh, scp form); None for anything else."""
    if not url:
        return None
    match = _SLUG.match(url.strip())
    return f"{match['owner']}/{match['repo']}" if match else None


def host_kind(url: str) -> str:
    """"github", "other" (a local path or a known non-GitHub host) or "unknown" (an SSH alias, a
    self-hosted server: it may be GitHub under another name, so the caller treats it as public)."""
    text = url.strip()
    if github_slug(text):
        return "github"
    if "://" in text:
        scheme, rest = text.split("://", 1)
        if scheme.lower() == "file":
            return "other"
        host = rest.split("/", 1)[0].rsplit("@", 1)[-1].split(":", 1)[0].lower()
    elif _DRIVE.match(text) or not _SCP.match(text):
        return "other"  # a local path
    else:
        host = _SCP.match(text)["host"].lower()
    if any(host == h or host.endswith("." + h) for h in OTHER_HOSTS):
        return "other"
    return "unknown"


def repo_arg_slug(value: str | None) -> str | None:
    """`owner/repo` from a gh `--repo` value: `owner/repo`, `github.com/owner/repo`, or a URL."""
    if not value:
        return None
    text = value.strip()
    if text.lower().startswith("github.com/"):
        text = text[len("github.com/"):]
    if _PLAIN_SLUG.match(text):
        return text.removesuffix(".git")
    return github_slug(text)


def _cache_path() -> Path:
    env = os.environ.get(ENV_CACHE)
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "public-repo-guard" / "visibility.json"


def _read_cache() -> dict:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def remember(slug: str, answer: str) -> None:
    """Record an answer (also used when this tool sees a repository being made public)."""
    cache = _read_cache()
    cache[slug.lower()] = [answer, time.time()]
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass  # a cache that cannot be written only costs a lookup next time


def _from_gh(slug: str) -> str | None:
    if not shutil.which("gh"):
        return None
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{slug}", "--jq", ".visibility"],
            capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    answer = result.stdout.strip().lower()
    if result.returncode == 0 and answer in (PUBLIC, PRIVATE, INTERNAL):
        return answer
    return None


def _from_rest(slug: str) -> tuple[str, bool]:
    """(answer, worth caching). A 404 is private for now but never cached: the repository may be
    about to be created or made public."""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{slug}",
        headers={"User-Agent": "public-repo-guard", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return (PRIVATE, False) if exc.code == 404 else (UNKNOWN, False)
    except (urllib.error.URLError, OSError, ValueError):
        return UNKNOWN, False
    answer = str(data.get("visibility") or "").lower()
    if answer in (PUBLIC, PRIVATE, INTERNAL):
        return answer, True
    return (PRIVATE if data.get("private") else PUBLIC), True


def visibility(slug: str | None) -> str:
    """public, private, internal, unknown (could not read it), or not-github."""
    forced = os.environ.get(ENV_FORCE, "").strip().lower()
    if forced in (PUBLIC, PRIVATE, INTERNAL, UNKNOWN):
        return forced
    if not slug:
        return NOT_GITHUB
    entry = _read_cache().get(slug.lower())
    if isinstance(entry, list) and len(entry) == 2 and time.time() - float(entry[1]) < TTL_SECONDS:
        return str(entry[0])
    answer = _from_gh(slug)
    keep = answer is not None
    if answer is None:
        answer, keep = _from_rest(slug)
    if keep:
        remember(slug, answer)
    return answer
