"""End to end: the shapes a close review found that an earlier version let through."""
from __future__ import annotations

import json
from pathlib import Path

from conftest import commit, git, guard
from test_git import agent

from public_repo_guard import denylist as dl
from public_repo_guard.command import Action
from public_repo_guard.guard import check_action, check_gh_text, check_push


def test_a_binary_or_empty_new_file_is_read_by_its_name(repo):
    (repo / "northwind-traders-logo.png").write_bytes(b"\x89PNG\x00\x01\x02")
    (repo / "clients").mkdir()
    (repo / "clients" / "northwind_traders.keep").write_bytes(b"")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "assets")
    result = agent(repo, "git push origin main")
    assert result.returncode == 2
    assert "northwind-traders-logo.png (file name" in result.stderr
    assert "clients/northwind_traders.keep (file name" in result.stderr


def test_scan_range_all_reads_the_first_commit_and_every_branch(tmp_path):
    work = tmp_path / "w"
    git(tmp_path, "init", "-q", "-b", "main", str(work))
    commit(work, "a.txt", "Northwind Traders\n", "first")
    commit(work, "a.txt", "clean\n", "second")
    git(work, "branch", "jane-example-side")
    result = guard(work, "scan-range", "--all")
    assert result.returncode == 1
    assert "a.txt (commit" in result.stdout and "jane-example-side" in result.stdout


def test_a_push_of_head_reads_the_branch_it_creates(repo):
    git(repo, "checkout", "-q", "-b", "feat/northwind-traders-import")
    commit(repo, "b.txt", "clean\n")
    result = agent(repo, "git push --no-verify -u origin HEAD")
    assert result.returncode == 2 and "feat/northwind-traders-import" in result.stderr


def test_a_heredoc_with_an_apostrophe_does_not_hide_the_push(repo):
    commit(repo, "a.txt", "Northwind Traders\n")
    command = "git commit --allow-empty -q -F - <<'EOF'\nit's done\nEOF\ngit push origin main"
    assert agent(repo, command).returncode == 2


def test_a_push_from_a_folder_that_is_not_a_repository_is_refused(repo, tmp_path):
    result = agent(repo, f"git -C {(tmp_path / 'nowhere').as_posix()} push origin main")
    assert result.returncode == 2 and "cannot find the repository" in result.stderr


def test_insteadof_rewrites_are_applied_before_reading_visibility(repo, monkeypatch):
    monkeypatch.delenv("PUBLIC_REPO_GUARD_VISIBILITY")
    git(repo, "config", "url.https://github.com/.insteadOf", "gh:")  # applies to a pushurl too
    git(repo, "config", "remote.origin.pushurl", "gh:octo/tool")  # fetches stay on the local stand-in
    commit(repo, "a.txt", "Northwind Traders\n")
    seen: list = []

    def resolve(slug):
        seen.append(slug)
        return "public" if slug == "octo/tool" else "not-github"

    verdict = check_push(repo, "origin", ["main"], dl.load, resolve)
    assert "octo/tool" in seen and verdict.refused


def test_making_a_repository_public_updates_the_visibility_cache(repo, isolated):
    git(repo, "config", "remote.origin.pushurl", "https://github.com/octo/tool.git")
    action = Action("make_public", str(repo), verb="edit", words=["repo", "edit", "--visibility", "public"])
    assert not check_action(action, dl.load).refused
    cache = json.loads((isolated / "cache.json").read_text(encoding="utf-8"))
    assert cache["octo/tool"][0] == "public"


def test_a_mirror_push_from_a_bare_clone_is_read(repo, tmp_path):
    commit(repo, "a.txt", "Northwind Traders\n")
    bare = tmp_path / "mirror.git"
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
    target = tmp_path / "target.git"
    git(tmp_path, "init", "-q", "--bare", str(target))
    assert agent(bare, f"git push --mirror {target.as_posix()}").returncode == 2


def test_gh_text_follows_the_default_repository_gh_chose(repo, monkeypatch):
    monkeypatch.delenv("GH_REPO", raising=False)
    git(repo, "remote", "add", "upstream", "https://github.com/octo/public-upstream.git")
    git(repo, "config", "remote.origin.pushurl", "https://github.com/octo/private-fork.git")
    git(repo, "config", "remote.upstream.gh-resolved", "base")

    def resolve(slug):
        return "public" if slug == "octo/public-upstream" else "private"

    verdict = check_gh_text(Path(repo), None, ["Northwind Traders import"], [], dl.load, resolve)
    assert verdict.refused and verdict.target == "octo/public-upstream"
