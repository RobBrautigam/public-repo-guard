"""End to end: the shapes a close review found that an earlier version let through."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from conftest import commit, git, guard
from test_git import agent, push

from public_repo_guard import cli, gitscan
from public_repo_guard import denylist as dl
from public_repo_guard import visibility as vis
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
    read: list = []

    def resolve(slug):
        seen.append(slug)
        return "public" if slug == "octo/tool" else "not-github"

    def holds_nothing(_top, url):  # offline: the URL is never asked over the network
        read.append(url)
        return set()

    monkeypatch.setattr(gitscan, "remote_shas", holds_nothing)
    verdict = check_push(repo, "origin", ["main"], dl.load, resolve)
    assert "octo/tool" in seen and verdict.refused
    assert read == ["https://github.com/octo/tool"]  # the push URL is asked what it holds, not the fetch URL


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


def _bare(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(path))
    return path


def _holds(bare: Path) -> str:
    return git(bare, "for-each-ref", "--format=%(objectname)")


@pytest.mark.parametrize("door", ["agent hook", "git hook"])
@pytest.mark.parametrize("apart", [True, False], ids=["pushurl apart", "no pushurl"])
def test_what_the_push_url_holds_is_read_not_the_fetch_url(repo, tmp_path, door, apart):
    commit(repo, "a.txt", "Northwind Traders\n")
    git(repo, "push", "-q", "origin", "main")  # the fetch URL holds the name (no hook installed yet)
    target = tmp_path / "remote.git"
    if apart:  # the push goes to a repository that holds none of it; every URL reads as public
        target = _bare(tmp_path, "push.git")
        git(repo, "config", "remote.origin.pushurl", target.as_posix())
    commit(repo, "b.txt", "a clean line\n")
    held = _holds(target)
    if door == "agent hook":
        result = agent(repo, "git push origin main")
    else:
        assert guard(repo, "install").returncode == 0
        result = push(repo)
    if apart:
        assert result.returncode != 0 and "a.txt (commit" in result.stderr
        assert _holds(target) == held  # nothing reached the push URL
    else:
        assert result.returncode == 0, result.stderr


def test_only_what_every_push_url_holds_is_left_out(repo, tmp_path):
    commit(repo, "a.txt", "Northwind Traders\n")
    git(repo, "push", "-q", "origin", "main")  # the fetch URL, also the first push URL, holds it
    for url in (tmp_path / "remote.git", _bare(tmp_path, "behind.git")):
        git(repo, "config", "--add", "remote.origin.pushurl", url.as_posix())
    result = agent(repo, "git push origin main")
    assert result.returncode == 2 and "a.txt (commit" in result.stderr


def test_what_the_push_url_already_holds_is_left_out(repo, tmp_path):
    commit(repo, "a.txt", "Northwind Traders\n")
    ahead = _bare(tmp_path, "ahead.git")
    git(repo, "push", "-q", ahead.as_posix(), "main")  # the push URL holds the name; the fetch URL does not
    git(repo, "config", "remote.origin.pushurl", ahead.as_posix())
    commit(repo, "b.txt", "a clean line\n")
    result = agent(repo, "git push origin main")
    assert result.returncode == 0, result.stderr


def _merge_that_adds_a_line(repo: Path) -> None:
    """A merge whose own resolution adds a line neither parent holds."""
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "side.txt", "side\n")
    git(repo, "checkout", "-q", "main")
    commit(repo, "main.txt", "main\n")
    git(repo, "merge", "-q", "--no-ff", "--no-commit", "side")
    (repo / "merged.txt").write_text("Northwind Traders\n", encoding="utf-8")
    git(repo, "add", "merged.txt")
    git(repo, "commit", "-q", "-m", "merge side")


def _git_log_answers(monkeypatch, answer) -> list[list[str]]:
    """Record every `git log` the scan runs; `answer(cmd, timeout)` returns a stand-in result,
    raises, or returns None to let git run."""
    run = subprocess.run
    calls: list[list[str]] = []

    def fake(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "-C"] and "log" in cmd:
            calls.append(cmd)
            stand_in = answer(cmd, kwargs.get("timeout"))
            if stand_in is not None:
                return stand_in
        return run(cmd, *args, **kwargs)

    monkeypatch.setattr(gitscan.subprocess, "run", fake)
    return calls


def test_a_history_read_that_times_out_is_not_run_again(repo, monkeypatch):
    _merge_that_adds_a_line(repo)

    def time_out(cmd, timeout):
        if "--diff-merges=remerge" in cmd:
            raise subprocess.TimeoutExpired(cmd, timeout)

    calls = _git_log_answers(monkeypatch, time_out)
    verdict = check_push(repo, "origin", ["main"], dl.load)
    assert len(calls) == 1
    assert verdict.refused and "could not finish" in verdict.reason


def test_a_history_read_git_refuses_for_another_reason_is_not_run_again(repo, monkeypatch):
    calls = _git_log_answers(monkeypatch, lambda cmd, timeout: None)
    with pytest.raises(gitscan.GitError, match="bad revision"):
        gitscan.scan_commits(repo, ["no-such-branch"], [], dl.load())
    assert len(calls) == 1


@pytest.mark.parametrize("knows_remerge", [True, False], ids=["git 2.36 or newer", "older git"])
def test_a_merge_s_added_line_is_read_whenever_the_scan_finishes(repo, monkeypatch, knows_remerge):
    _merge_that_adds_a_line(repo)

    def older_git(cmd, timeout):
        if not knows_remerge and "--diff-merges=remerge" in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: unknown value for --diff-merges: remerge\n")
        return None

    calls = _git_log_answers(monkeypatch, older_git)
    verdict = check_push(repo, "origin", ["main"], dl.load)
    assert len(calls) == (1 if knows_remerge else 2)
    assert verdict.refused and "merged.txt (commit" in verdict.render()


@pytest.mark.parametrize("target, printed, looked_up", [
    ("https://gitlab.com/octo/tool.git", "not-github", []),
    ("git@bitbucket.org:octo/tool.git", "not-github", []),
    ("../remote.git", "not-github", []),
    ("build-box:octo/tool", "unknown", []),  # an SSH host alias: the hooks treat it as public
    ("https://github.com/octo/tool.git", "public", ["octo/tool"]),
    ("octo/tool", "public", ["octo/tool"]),
])
def test_the_visibility_command_looks_up_only_a_github_repository(monkeypatch, capsys, target, printed,
                                                                   looked_up):
    monkeypatch.delenv("PUBLIC_REPO_GUARD_VISIBILITY")
    lookups: list = []

    def answer(slug):
        lookups.append(slug)
        return "public"

    monkeypatch.setattr(vis, "_from_gh", answer)
    monkeypatch.setattr(vis, "_from_rest", lambda slug: (answer(slug), True))
    assert cli.main(["visibility", target]) == 0
    assert capsys.readouterr().out.strip() == printed
    assert lookups == looked_up
