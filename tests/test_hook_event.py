"""The agent hook on an event it cannot read: refused when the event's text looks like it publishes,
passed when it does not, and never a crash (Claude Code reads an exit of 1 as a hook error and runs
the command unchecked)."""
from __future__ import annotations

import json

import pytest
from conftest import guard

PUSH = "git push origin main"
PR = "gh pr create --title t --body b"


def event(tool_input, tool_name: str = "Bash", **extra) -> str:
    return json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool_name, "cwd": ".",
                       "tool_input": tool_input, **extra})


CASES = {
    # tool_input is not an object: a string, then a list, then a number, with and without a publishing command
    "string, git push": (event(PUSH), 2),
    "string, gh pr create": (event(PR), 2),
    "string, nothing that publishes": (event("ls -la"), 0),
    "list, git push": (event([PUSH]), 2),
    "list of words, gh pr create": (event(PR.split()), 2),
    "list of words, escaped git push": (event(PUSH.split()).replace('"push"', '"pu\\u0073h"'), 2),
    "list, nothing that publishes": (event(["ls", "-la"]), 0),
    "number, git push beside it": (event(42, description=PUSH), 2),
    "number, gh pr create beside it": (event(42, description=PR), 2),
    "number, nothing that publishes": (event(42), 0),
    "empty string, git push beside it": (event("", description=PUSH), 2),
    "PowerShell, string, git push": (event(PUSH, tool_name="PowerShell"), 2),
    "string, escaped tab and letter": (event(PUSH).replace(" push", "\\tpu\\u0073h"), 2),
    # the same read one level in, and one level out
    "command is a list, git push": (event({"command": PUSH.split()}), 2),
    "command is a number": (event({"command": 7}), 0),
    "event is not an object, git push": (json.dumps([PUSH]), 2),
    "event is not JSON, git push": (event({"command": PUSH})[:-2], 2),
    "event is not JSON, nothing that publishes": ("not json", 0),
    "event nested too deep to read, git push": (
        event(None).replace("null", "[" * 100_000 + json.dumps(PUSH) + "]" * 100_000), 2),
    # what the change leaves as it was
    "not a shell tool, string, git push": (event(PUSH, tool_name="Edit"), 0),
    "tool_input is null": (event(None), 0),
    "tool_input has no command": (event({"description": PUSH}), 0),
}


@pytest.mark.parametrize("stdin, code", list(CASES.values()), ids=list(CASES))
def test_an_event_that_cannot_be_read_is_refused_only_when_it_publishes(tmp_path, stdin, code):
    result = guard(tmp_path, "hook", stdin=stdin)
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == code, result.stderr
    if code == 2:
        assert "public-repo-guard: refused. the hook event could not be read" in result.stderr
    else:
        assert result.stderr == ""
