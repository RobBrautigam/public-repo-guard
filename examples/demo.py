"""A two-minute, offline demo: a throwaway repository, a throwaway "GitHub" (a local bare
repository the guard is told to treat as public), the example denylist, and three pushes.

    python examples/demo.py

Nothing leaves your machine: the remote is a folder in a temporary directory.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(cmd: list[str], cwd: Path, env: dict, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, input=stdin, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", creationflags=NO_WINDOW)


def step(title: str) -> None:
    print(f"\n=== {title}")


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        env = dict(os.environ)
        env.update({
            "PUBLIC_REPO_GUARD_DENYLIST": str(HERE / "denylist.toml"),
            "PUBLIC_REPO_GUARD_VISIBILITY": "public",   # stand-in for a public GitHub repository
            "PUBLIC_REPO_GUARD_CACHE": str(root / "cache.json"),
            "GIT_AUTHOR_NAME": "Demo", "GIT_AUTHOR_EMAIL": "demo@example.com",
            "GIT_COMMITTER_NAME": "Demo", "GIT_COMMITTER_EMAIL": "demo@example.com",
        })
        remote, work = root / "remote.git", root / "work"
        run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], root, env)
        run(["git", "init", "-q", "-b", "main", str(work)], root, env)
        run(["git", "remote", "add", "origin", "../remote.git"], work, env)
        installed = run([sys.executable, "-m", "public_repo_guard", "install"], work, env)
        print(installed.stdout.strip() or installed.stderr.strip())

        step("1. a clean first commit is pushed")
        (work / "README.md").write_text("# demo tool\n\nA small open-source helper.\n", encoding="utf-8")
        run(["git", "add", "README.md"], work, env)
        run(["git", "commit", "-q", "-m", "first commit"], work, env)
        push = run(["git", "push", "-q", "origin", "main"], work, env)
        print("push exit code:", push.returncode)

        step("2. an agent adds a test fixture copied from a real client, and tries to push")
        (work / "tests").mkdir()
        (work / "tests" / "fixture.json").write_text(
            json.dumps({"customer": "Northwind Traders", "contact": "ops@example.com"}, indent=2) + "\n",
            encoding="utf-8")
        run(["git", "add", "tests/fixture.json"], work, env)
        run(["git", "commit", "-q", "-m", "add the fixture from the jane-example account"], work, env)
        push = run(["git", "push", "-q", "origin", "main"], work, env)
        print("push exit code:", push.returncode)
        print(push.stderr.strip())

        step("3. the same push, asked through the Claude Code hook, is refused before it runs")
        event = {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}, "cwd": str(work)}
        hook = run([sys.executable, "-m", "public_repo_guard", "hook"], work, env, json.dumps(event))
        print("hook exit code:", hook.returncode, "(2 refuses the tool call)")

        step("4. the fixture is rewritten with invented names, the commit amended, the push goes through")
        (work / "tests" / "fixture.json").write_text(
            json.dumps({"customer": "Example Co", "contact": "ops@example.com"}, indent=2) + "\n",
            encoding="utf-8")
        run(["git", "add", "tests/fixture.json"], work, env)
        run(["git", "commit", "-q", "--amend", "-m", "add an invented fixture"], work, env)
        push = run(["git", "push", "-q", "origin", "main"], work, env)
        print("push exit code:", push.returncode)
        return 0


if __name__ == "__main__":
    sys.exit(main())
