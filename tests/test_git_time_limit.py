"""A git call that reaches its time limit is stopped there, with the processes git started.

git runs a shell alias here whose program sleeps far past the limit and starts a process of its own
that sleeps too; both hold git's output open, as a remote helper, ssh or upload-pack does."""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

from public_repo_guard import gitscan

WINDOWS = os.name == "nt"
LIMIT = 6    # seconds: room for git, its shell and two Pythons to start on a busy machine
MARGIN = 4   # the call returns at most this long after its limit
SLEEP = 25   # how long the alias's two processes would run if nothing ended them

CHILD = """\
import os, subprocess, sys, time
from pathlib import Path
folder = Path(sys.argv[1])
(folder / "child.part").write_text(str(os.getpid()))
os.replace(folder / "child.part", folder / "child.pid")
# the grandchild is handed this process's output, which is git's: it holds it open too
subprocess.Popen([sys.executable, str(folder / "grandchild.py"), str(folder)], stdout=sys.stdout,
                 stderr=sys.stderr)
time.sleep({sleep})
"""

GRANDCHILD = """\
import os, sys, time
from pathlib import Path
folder = Path(sys.argv[1])
(folder / "grandchild.part").write_text(str(os.getpid()))
os.replace(folder / "grandchild.part", folder / "grandchild.pid")
time.sleep({sleep})
"""


class Watched:
    """The alias's processes, read by process id while the call runs. On Windows each is held
    open by a handle, so its id cannot pass to another process before it is checked or stopped."""

    def __init__(self) -> None:
        self.ids: dict[str, int] = {}
        self.handles: dict[str, int] = {}

    def look(self, folder: Path) -> None:
        for name in ("child", "grandchild"):
            path = folder / f"{name}.pid"
            if name not in self.ids and path.exists():
                self.ids[name] = int(path.read_text())
                if WINDOWS:
                    handle = _kernel32().OpenProcess(0x00100000 | 0x0001 | 0x1000, False, self.ids[name])
                    if handle:  # SYNCHRONIZE, PROCESS_TERMINATE, PROCESS_QUERY_LIMITED_INFORMATION
                        self.handles[name] = handle

    def running(self, name: str, wait: float = 0.0) -> bool:
        if WINDOWS:
            handle = self.handles.get(name)
            return bool(handle) and _kernel32().WaitForSingleObject(handle, int(wait * 1000)) == 0x102
        try:
            os.kill(self.ids[name], 0)
        except ProcessLookupError:
            return False
        return True

    def stop(self) -> None:
        """Every process this test started, stopped by its own id (or handle)."""
        for name, pid in self.ids.items():
            if WINDOWS:
                handle = self.handles.get(name)
                if handle:
                    _kernel32().TerminateProcess(handle, 1)
                    _kernel32().CloseHandle(handle)
            elif self.running(name):
                os.kill(pid, signal.SIGKILL)


def _kernel32():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def test_a_git_call_past_its_limit_is_stopped_with_what_git_started(tmp_path):
    folder = tmp_path / "sleepy"
    folder.mkdir()
    (folder / "child.py").write_text(CHILD.format(sleep=SLEEP), encoding="utf-8")
    (folder / "grandchild.py").write_text(GRANDCHILD.format(sleep=SLEEP), encoding="utf-8")
    python, child = Path(sys.executable).as_posix(), (folder / "child.py").as_posix()
    alias = f'alias.sleepy=!"{python}" "{child}" "{folder.as_posix()}"'
    outcome: dict = {}

    def call() -> None:
        start = time.monotonic()
        try:
            gitscan.git(tmp_path, "-c", alias, "sleepy", timeout=LIMIT)
        except gitscan.GitError as exc:
            outcome["error"] = str(exc)
        outcome["seconds"] = time.monotonic() - start

    watched = Watched()
    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    try:
        while worker.is_alive() and len(watched.ids) < 2:
            watched.look(folder)
            time.sleep(0.05)
        worker.join(SLEEP + 30)
        assert not worker.is_alive(), "the call never returned"
        assert set(watched.ids) == {"child", "grandchild"}, f"started within the limit: {watched.ids}"
        assert "could not run" in outcome.get("error", ""), outcome
        assert outcome["seconds"] < LIMIT + MARGIN, f"returned {outcome['seconds']:.1f} s after its start"
        if WINDOWS:
            # Linux and macOS end git itself and return at the limit, as before; there the
            # processes git started are left to end on their own
            assert not watched.running("child", wait=2), "git's child is still running"
            assert not watched.running("grandchild", wait=2), "the child's own child is still running"
    finally:
        watched.stop()
