"""End to end: real git repositories, a bare repository standing in for GitHub, the real hooks."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from conftest import commit, git, guard

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def push(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "push", "-q", "origin", "main"], cwd=repo, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)


def agent(repo: Path, command: str) -> subprocess.CompletedProcess:
    event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
    return guard(repo, "hook", stdin=json.dumps(event))


def test_the_pre_push_hook_refuses_a_name_in_an_added_line(repo):
    assert guard(repo, "install").returncode == 0
    commit(repo, "tests/fixture.json", '{"customer": "Northwind Traders"}\n')
    result = push(repo)
    assert result.returncode != 0
    assert "tests/fixture.json (commit" in result.stderr
    assert "line 1: client (client-northwind) matched 'Northwind Traders'" in result.stderr
    assert "northwind[" not in result.stderr  # the regex itself is never printed


def test_a_commit_message_and_a_file_name_are_read(repo):
    commit(repo, "docs/jane-example-notes.md", "nothing here\n", "notes for jane example")
    result = agent(repo, "git push origin main")
    assert result.returncode == 2
    assert "message, line 1: person (person-jane)" in result.stderr
    assert "docs/jane-example-notes.md (file name, commit" in result.stderr


def test_a_private_repository_passes_silently(repo, monkeypatch):
    monkeypatch.setenv("PUBLIC_REPO_GUARD_VISIBILITY", "private")
    commit(repo, "a.txt", "Northwind Traders\n")
    result = agent(repo, "git push")
    assert result.returncode == 0 and result.stderr == ""


def test_unknown_visibility_fails_closed(repo, monkeypatch):
    monkeypatch.setenv("PUBLIC_REPO_GUARD_VISIBILITY", "unknown")
    commit(repo, "a.txt", "Northwind Traders\n")
    assert agent(repo, "git push").returncode == 2


def test_what_the_remote_already_holds_is_not_read_again(repo, monkeypatch):
    monkeypatch.setenv("PUBLIC_REPO_GUARD_VISIBILITY", "private")
    commit(repo, "old.txt", "Northwind Traders\n")
    assert push(repo).returncode == 0  # it went out while the repository was private
    monkeypatch.setenv("PUBLIC_REPO_GUARD_VISIBILITY", "public")
    commit(repo, "new.txt", "a clean line\n")
    assert agent(repo, "git push").returncode == 0


def test_a_removed_line_is_not_a_finding(repo, monkeypatch):
    monkeypatch.setenv("PUBLIC_REPO_GUARD_VISIBILITY", "private")
    commit(repo, "a.txt", "Northwind Traders\n")
    assert push(repo).returncode == 0
    monkeypatch.setenv("PUBLIC_REPO_GUARD_VISIBILITY", "public")
    commit(repo, "a.txt", "Example Co\n", "use an invented name")
    assert agent(repo, "git push").returncode == 0


def test_allow_paths_let_a_license_line_through(repo):
    commit(repo, "LICENSE", "Copyright (c) 2026 Bluebird Labs\n")
    assert agent(repo, "git push").returncode == 0
    commit(repo, "README.md", "Made at Bluebird Labs\n")
    assert agent(repo, "git push").returncode == 2


def test_a_missing_denylist_refuses_a_public_push(repo, monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLIC_REPO_GUARD_DENYLIST", str(tmp_path / "absent.toml"))
    commit(repo, "a.txt", "clean\n")
    result = agent(repo, "git push")
    assert result.returncode == 2 and "no denylist at" in result.stderr


def test_a_denylist_inside_the_repository_is_refused(repo, monkeypatch):
    inside = repo / "denylist.toml"
    inside.write_text("[[pattern]]\nid='x'\nregex='zzz'\n", encoding="utf-8")
    monkeypatch.setenv("PUBLIC_REPO_GUARD_DENYLIST", str(inside))
    assert "sits inside this repository" in agent(repo, "git push").stderr


def test_making_a_folder_public_reads_its_whole_history(repo, monkeypatch):
    monkeypatch.setenv("PUBLIC_REPO_GUARD_VISIBILITY", "private")
    commit(repo, "a.txt", "Northwind Traders\n")
    commit(repo, "a.txt", "Example Co\n", "scrub it")  # gone from the tree, still in history
    result = agent(repo, "gh repo create octo/tool --public --source . --push")
    assert result.returncode == 2 and "a.txt (commit" in result.stderr


def test_creating_an_empty_public_repository_reads_its_description(tmp_path):
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    result = agent(outside, 'gh repo create octo/tool --public --description "helper for Northwind Traders"')
    assert result.returncode == 2 and "creating octo/tool would publish" in result.stderr
    assert agent(outside, 'gh repo create octo/tool --public --description "a helper"').returncode == 0


def test_a_gh_pull_request_body_is_read(repo):
    result = agent(repo, 'gh pr create -R octo/tool --title "Fix" --body "asked by Jane Example"')
    assert result.returncode == 2 and "the gh command's words" in result.stderr


def test_a_gh_body_file_is_read(repo, tmp_path):
    body = tmp_path / "body.md"
    body.write_text("Context: the Northwind Traders rollout\n", encoding="utf-8")
    result = agent(repo, f'gh pr create -R octo/tool --title "Fix" --body-file "{body.as_posix()}"')
    assert result.returncode == 2 and "body.md, line 1" in result.stderr


def test_scan_tree_reports_and_skips_binaries(repo):
    (repo / "notes.md").write_text("draft for northwind traders\n", encoding="utf-8")
    (repo / "image.bin").write_bytes(b"\x00\x01Northwind Traders")
    git(repo, "add", "notes.md", "image.bin")
    result = guard(repo, "scan-tree", ".")
    assert result.returncode == 1
    assert "notes.md, line 1" in result.stdout and "image.bin, line" not in result.stdout


def test_scan_range(repo):
    commit(repo, "a.txt", "Northwind Traders\n")
    result = guard(repo, "scan-range", "origin/main..HEAD")
    assert result.returncode == 1 and "a.txt (commit" in result.stdout


def test_install_never_overwrites_someone_elses_hook(repo):
    hooks = repo / ".git" / "hooks"
    (hooks / "pre-push").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    result = guard(repo, "install")
    assert result.returncode == 1 and "already exists" in result.stderr
    assert (hooks / "pre-push").read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"


def test_a_tool_call_that_is_not_a_shell_command_passes(repo):
    event = {"tool_name": "Edit", "tool_input": {"file_path": "x"}, "cwd": str(repo)}
    assert guard(repo, "hook", stdin=json.dumps(event)).returncode == 0
