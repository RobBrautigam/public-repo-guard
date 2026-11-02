from __future__ import annotations

from pathlib import Path

import pytest

from public_repo_guard import denylist as dl
from public_repo_guard.command import Unreadable, parse
from public_repo_guard.visibility import github_slug, repo_arg_slug, visibility

ROOT = Path(__file__).resolve().parents[1]


def test_the_example_denylist_loads():
    patterns = dl.load(ROOT / "examples" / "denylist.toml")
    assert {p.id for p in patterns} >= {"client-northwind", "home-path"}


def test_a_name_split_by_punctuation_is_still_found():
    patterns = dl.load()
    for text in ("Northwind Traders", "northwind-traders", "NORTHWIND_TRADERS", "northwind.traders"):
        assert dl.scan_text(text, patterns, "x"), text
    assert not dl.scan_text("northwind is a direction", patterns, "x")


def test_allow_paths_scope_a_pattern(tmp_path):
    patterns = dl.load()
    assert not dl.scan_text("Copyright Bluebird Labs", patterns, "LICENSE", path="LICENSE")
    assert dl.scan_text("Copyright Bluebird Labs", patterns, "README.md", path="README.md")


def test_line_numbers_follow_newlines_only():
    patterns = dl.load()
    found = dl.scan_text("one\x1etwo\nNorthwind Traders\n", patterns, "f")
    assert found[0].where == "f, line 2"


@pytest.mark.parametrize("body, reason", [
    ("[[pattern]]\nid = 'a'\nregex = '('\n", "does not compile"),
    ("[[pattern]]\nid = 'a'\nregex = 'x*'\n", "matches empty text"),
    ("[[pattern]]\nid = 'a'\nregex = 'x'\n[[pattern]]\nid = 'a'\nregex = 'y'\n", "used twice"),
    ("title = 'no patterns'\n", "holds no"),
])
def test_a_denylist_that_cannot_work_says_why(tmp_path, body, reason):
    path = tmp_path / "d.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(dl.DenylistError, match=reason):
        dl.load(path)


def test_a_missing_denylist_names_its_path(tmp_path):
    with pytest.raises(dl.DenylistError, match="no denylist at"):
        dl.load(tmp_path / "absent.toml")


@pytest.mark.parametrize("url, slug", [
    ("https://github.com/octo/tool.git", "octo/tool"),
    ("https://github.com/octo/tool", "octo/tool"),
    ("https://token@github.com/octo/tool.git", "octo/tool"),
    ("git@github.com:octo/tool.git", "octo/tool"),
    ("ssh://git@github.com/octo/tool.git", "octo/tool"),
    ("https://gitlab.com/octo/tool.git", None),
    ("../remote.git", None),
    ("repos/remote.git", None),
])
def test_github_slug(url, slug):
    assert github_slug(url) == slug


def test_repo_arg_slug():
    assert repo_arg_slug("octo/tool") == "octo/tool"
    assert repo_arg_slug("github.com/octo/tool") == "octo/tool"
    assert repo_arg_slug("https://github.com/octo/tool") == "octo/tool"


def test_forced_visibility(monkeypatch):
    monkeypatch.setenv("PUBLIC_REPO_GUARD_VISIBILITY", "private")
    assert visibility("octo/tool") == "private"
    monkeypatch.delenv("PUBLIC_REPO_GUARD_VISIBILITY")
    assert visibility(None) == "not-github"


def kinds(command: str, cwd: str = "/work") -> list[tuple]:
    return [(a.kind, a.verb, a.remote, a.repo) for a in parse(command, cwd)]


def test_parse_finds_pushes_in_compound_commands():
    assert kinds("git add . && git commit -m x && git push") == [("push", "", None, None)]
    assert kinds("git -C sub push origin main")[0][:3] == ("push", "", "origin")
    assert kinds("FOO=1 timeout 30 git push upstream")[0][2] == "upstream"
    assert kinds('bash -c "git push origin main"')[0][0] == "push"
    assert kinds("git status\ngit push")[0][0] == "push"


def test_parse_follows_cd_and_dash_c():
    actions = parse("cd sub && git push", "/work")
    assert Path(actions[0].cwd) == Path("/work/sub")
    actions = parse("git -C ../other push", "/work/here")
    assert Path(actions[0].cwd) == Path("/work/other")


def test_parse_ignores_what_does_not_publish():
    assert kinds("echo git push") == []
    assert kinds("git log --oneline") == []
    assert kinds("gh pr view 12") == []
    assert kinds("gh api repos/octo/tool") == []
    assert kinds("gh repo create octo/tool --private --source . --push") == []


def test_parse_reads_gh_writes():
    assert kinds("gh repo create octo/tool --public --source . --push") == [
        ("make_public", "create", None, "octo/tool")]
    assert kinds("gh repo create --public -s. octo/tool")[0][3] == "octo/tool"
    assert kinds("gh repo edit octo/tool --visibility public") == [("make_public", "edit", None, "octo/tool")]
    assert kinds('gh repo edit --description "x" --visibility public') == [("make_public", "edit", None, None)]
    assert kinds('gh pr create --title "t" --body "b" -R octo/tool') == [("gh_text", "", None, "octo/tool")]
    assert kinds("gh api -X PATCH repos/octo/tool -f private=false")[0][:2] == ("make_public", "api")
    assert kinds("gh api repos/octo/tool/issues -f title=x")[0][3] == "octo/tool"


def test_parse_reads_inside_shell_blocks_and_keywords():
    assert kinds("if ($?) { git push origin main }")[0][:3] == ("push", "", "origin")
    assert kinds("if git fetch; then git push; fi")[-1][0] == "push"
    assert kinds("(cd sub && git push)")[0][0] == "push"
    assert kinds("{ git add . ; git push ; }")[0][0] == "push"
    assert kinds('git commit -m "a (quoted) message; not a clause"') == []


def test_a_push_that_deletes_publishes_nothing():
    assert kinds("git push origin --delete old-branch") == []
    assert kinds("git push -d origin old-branch") == []


def test_an_unclosed_quote_is_unreadable():
    with pytest.raises(Unreadable):
        parse('git commit -m "half', "/work")
