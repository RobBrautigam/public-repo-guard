"""The README's demo keeps to its own temporary folder, whatever git config the machine holds."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import NO_WINDOW, ROOT


def _files(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("where", ["the home folder's .gitconfig", "a GIT_CONFIG_GLOBAL file",
                                   "GIT_CONFIG_COUNT"])
def test_the_demo_leaves_a_shared_hooks_folder_as_it_was(isolated, tmp_path, monkeypatch, where):
    shared = tmp_path / "shared-hooks"
    shared.mkdir()
    (shared / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    setting = f"[core]\n\thooksPath = {shared.as_posix()}\n"
    if where == "the home folder's .gitconfig":  # where a user's own global config lives
        monkeypatch.delenv("GIT_CONFIG_GLOBAL")
        monkeypatch.setenv("HOME", str(isolated))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated / ".config"))
        with (isolated / ".gitconfig").open("a", encoding="utf-8") as config:
            config.write(setting)
    elif where == "a GIT_CONFIG_GLOBAL file":
        with (isolated / ".gitconfig").open("a", encoding="utf-8") as config:
            config.write(setting)
    else:  # as `git -c core.hooksPath=...` hands it to the programs git runs
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", shared.as_posix())
    before = _files(shared)
    result = subprocess.run([sys.executable, str(ROOT / "examples" / "demo.py")], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            env=os.environ.copy(), timeout=300, creationflags=NO_WINDOW)
    assert result.returncode == 0, result.stderr
    assert _files(shared) == before
    assert [line for line in result.stdout.splitlines() if "exit code:" in line] == [
        "push exit code: 0", "push exit code: 1", "hook exit code: 2 (2 refuses the tool call)",
        "push exit code: 0",
    ]
