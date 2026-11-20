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

    def holds_nothing(_top, url, _remote=None):  # offline: the URL is never asked over the network
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
        if door == "git hook":  # the agent hook never runs the push, so only this door can move it
            assert _holds(target) == held  # nothing reached the push URL
    else:
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("behind_first", [False, True], ids=["behind URL last", "behind URL first"])
def test_only_what_every_push_url_holds_is_left_out(repo, tmp_path, behind_first):
    commit(repo, "a.txt", "Northwind Traders\n")
    git(repo, "push", "-q", "origin", "main")  # the fetch URL, also a push URL, holds it
    behind = _bare(tmp_path, "behind.git")
    urls = [tmp_path / "remote.git", behind]
    for url in reversed(urls) if behind_first else urls:
        git(repo, "config", "--add", "remote.origin.pushurl", url.as_posix())
    result = agent(repo, "git push origin main")
    assert result.returncode == 2 and "a.txt (commit" in result.stderr
    assert f"to {behind.as_posix()} (public)" in result.stderr  # the URL that lacks it is named


@pytest.mark.parametrize("behind_first", [False, True], ids=["behind URL last", "behind URL first"])
def test_a_push_url_behind_another_is_scanned_only_for_what_it_lacks(repo, tmp_path, behind_first):
    commit(repo, "a.txt", "Northwind Traders\n")  # already public: both push URLs hold it
    behind = _bare(tmp_path, "behind.git")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "push", "-q", behind.as_posix(), "main")
    commit(repo, "b.txt", "a clean line\n")
    git(repo, "push", "-q", "origin", "main")  # one URL is a commit ahead of the other
    urls = [tmp_path / "remote.git", behind]
    for url in reversed(urls) if behind_first else urls:
        git(repo, "config", "--add", "remote.origin.pushurl", url.as_posix())
    commit(repo, "c.txt", "another clean line\n")
    result = agent(repo, "git push origin main")
    assert result.returncode == 0, result.stderr


def test_a_finding_two_push_urls_both_lack_is_listed_once(repo, tmp_path):
    empty = _bare(tmp_path, "empty.git")  # holds nothing, while the other holds the first commit
    for url in (tmp_path / "remote.git", empty):
        git(repo, "config", "--add", "remote.origin.pushurl", url.as_posix())
    commit(repo, "a.txt", "Northwind Traders\n")
    verdict = check_push(repo, "origin", ["main"], dl.load)
    assert verdict.refused
    assert len([f for f in verdict.findings if f.where.startswith("a.txt (commit")]) == 1


def test_the_git_hook_reads_only_the_url_it_runs_for(repo, tmp_path, monkeypatch):
    commit(repo, "a.txt", "Northwind Traders\n")
    git(repo, "push", "-q", "origin", "main")
    ahead, behind = tmp_path / "remote.git", _bare(tmp_path, "behind.git")
    for url in (ahead, behind):
        git(repo, "config", "--add", "remote.origin.pushurl", url.as_posix())
    tip = git(repo, "rev-parse", "HEAD").strip()
    reads = _git_log_answers(monkeypatch, lambda cmd, timeout: None, word="ls-remote")
    # git runs the hook once for each push URL and names it; the run for a URL that holds the
    # commit passes, and only that URL is asked
    verdict = check_push(repo, "origin", ["main"], dl.load, local_tips=[tip], extra_urls=[ahead.as_posix()])
    assert not verdict.refused, verdict.render()
    assert [cmd[-1] for cmd in reads] == [ahead.as_posix()]
    verdict = check_push(repo, "origin", ["main"], dl.load, local_tips=[tip], extra_urls=[behind.as_posix()])
    assert verdict.refused and "a.txt (commit" in verdict.render()


def test_a_remote_without_a_separate_push_url_is_read_by_its_name(repo, monkeypatch):
    commit(repo, "a.txt", "a clean line\n")
    reads = _git_log_answers(monkeypatch, lambda cmd, timeout: None, word="ls-remote")
    assert not check_push(repo, "origin", ["main"], dl.load).refused
    # by its name, so the remote's own settings (a proxy, an upload-pack) apply, as for git itself
    assert [cmd[-1] for cmd in reads] == ["origin"]


def test_a_separate_push_url_is_read_with_the_remote_s_proxy(repo, tmp_path, monkeypatch):
    target = _bare(tmp_path, "push.git")
    git(repo, "config", "remote.origin.pushurl", target.as_posix())
    git(repo, "config", "remote.origin.proxy", "http://proxy.example:3128")  # git pushes through it too
    commit(repo, "a.txt", "a clean line\n")
    reads = _git_log_answers(monkeypatch, lambda cmd, timeout: None, word="ls-remote")
    assert not check_push(repo, "origin", ["main"], dl.load).refused
    assert len(reads) == 1 and reads[0][-1] == target.as_posix()
    assert "http.proxy=http://proxy.example:3128" in reads[0]


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


def _git_log_answers(monkeypatch, answer, word: str = "log") -> list[list[str]]:
    """Record every `git log` (or other git command, by `word`) the scan runs; `answer(cmd, timeout)`
    returns a stand-in result, raises, or returns None to let git run."""
    run = subprocess.run
    calls: list[list[str]] = []

    def fake(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "-C"] and word in cmd:
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


def _git_of(version: str):
    """A stand-in for an older git's answer to `git log`: 2.31 to 2.35 know `--diff-merges` but not
    `remerge`; before 2.31 git knows no `--diff-merges` at all (the messages are git's own)."""
    def answer(cmd, timeout):
        for word in cmd:
            if version == "2.31 to 2.35" and word == "--diff-merges=remerge":
                return subprocess.CompletedProcess(cmd, 128, "", "fatal: unknown value for --diff-merges: remerge\n")
            if version == "before 2.31" and word.startswith("--diff-merges"):
                return subprocess.CompletedProcess(cmd, 128, "", f"fatal: unrecognized argument: {word}\n")
        return None
    return answer


@pytest.mark.parametrize("version", ["2.36 or newer", "2.31 to 2.35", "before 2.31"])
def test_a_merge_s_added_line_is_read_whenever_the_scan_finishes(repo, monkeypatch, version):
    _merge_that_adds_a_line(repo)
    calls = _git_log_answers(monkeypatch, _git_of(version))
    verdict = check_push(repo, "origin", ["main"], dl.load)
    assert len(calls) == (1 if version == "2.36 or newer" else 2)
    assert verdict.refused and "merged.txt (commit" in verdict.render()


def test_a_merge_s_message_is_listed_once_on_an_older_git(repo, monkeypatch):
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "side.txt", "side\n")
    git(repo, "checkout", "-q", "main")
    commit(repo, "main.txt", "main\n")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge the Northwind Traders work", "side")
    _git_log_answers(monkeypatch, _git_of("2.31 to 2.35"))  # -m prints a merge once for each parent
    verdict = check_push(repo, "origin", ["main"], dl.load)
    assert verdict.refused
    assert len([f for f in verdict.findings if "message" in f.where]) == 1


def test_a_second_history_read_that_fails_names_git_log(repo, monkeypatch):
    _merge_that_adds_a_line(repo)

    def no_diff_merges_and_no_m(cmd, timeout):
        return subprocess.CompletedProcess(cmd, 128, "", "fatal: unrecognized argument: --diff-merges=remerge\n")

    _git_log_answers(monkeypatch, no_diff_merges_and_no_m)
    verdict = check_push(repo, "origin", ["main"], dl.load)
    assert verdict.refused and "could not finish: git log -m failed" in verdict.reason


@pytest.mark.parametrize("target, printed, looked_up", [
    ("https://gitlab.com/octo/tool.git", "not-github", []),
    ("git@bitbucket.org:octo/tool.git", "not-github", []),
    ("../remote.git", "not-github", []),
    ("./mirror.git", "not-github", []),
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
