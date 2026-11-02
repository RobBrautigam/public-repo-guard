from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DENYLIST = """
[[pattern]]
id = "client-northwind"
category = "client"
regex = 'northwind[\\s_.-]*traders'

[[pattern]]
id = "person-jane"
category = "person"
regex = 'jane[\\s_.-]*example'

[[pattern]]
id = "company-bluebird"
category = "company"
regex = 'bluebird[\\s_.-]*labs'
allow_paths = ["LICENSE"]
"""


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No test reads the machine's git config, denylist, cache or GitHub."""
    home = tmp_path / "home"
    home.mkdir()
    gitconfig = home / ".gitconfig"
    gitconfig.write_text("[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    denylist = home / "denylist.toml"
    denylist.write_text(DENYLIST, encoding="utf-8")
    for key, value in {
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "PUBLIC_REPO_GUARD_DENYLIST": str(denylist),
        "PUBLIC_REPO_GUARD_CACHE": str(home / "cache.json"),
        "PUBLIC_REPO_GUARD_VISIBILITY": "public",
    }.items():
        monkeypatch.setenv(key, value)
    return home


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    assert result.returncode == 0, result.stderr
    return result.stdout


def guard(cwd: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "public_repo_guard", *args], cwd=cwd, input=stdin,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=os.environ.copy(), creationflags=NO_WINDOW)


def commit(repo: Path, name: str, text: str, message: str = "a change") -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A working repository whose origin is a bare repository beside it (a stand-in for GitHub)."""
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    git(tmp_path, "init", "-q", "-b", "main", str(work))
    git(work, "remote", "add", "origin", "../remote.git")
    commit(work, "README.md", "# a tool\n", "first commit")
    git(work, "push", "-q", "origin", "main")
    return work
