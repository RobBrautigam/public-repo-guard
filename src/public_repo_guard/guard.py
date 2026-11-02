"""The decisions: given an action, is it publishing to a public repository, and does what it
publishes carry a denylisted name? Silent unless public; fails closed when public."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import denylist as dl
from . import gitscan
from .command import Action
from .visibility import (INTERNAL, NOT_GITHUB, PRIVATE, PUBLIC, UNKNOWN, github_slug, host_kind, remember,
                         repo_arg_slug, visibility)

MAX_SHOWN = 12
FIX = (
    "Take each one out (edit the file, reword the message, or amend the commit) and run it again. "
    "Fixtures use example.com and invented names. Never --no-verify, and never a respelling that "
    "keeps the name: once a public repository holds it, it is published for good."
)


@dataclass
class Verdict:
    refused: bool = False
    target: str = ""
    reason: str = ""
    findings: list[dl.Finding] = field(default_factory=list)

    def render(self) -> str:
        if not self.refused:
            return ""
        lines = [f"public-repo-guard: refused. {self.reason}"]
        for finding in self.findings[:MAX_SHOWN]:
            lines.append(f"  - {finding.render()}")
        if len(self.findings) > MAX_SHOWN:
            lines.append(f"  - and {len(self.findings) - MAX_SHOWN} more")
        if self.findings:
            lines.append(FIX)
        return "\n".join(lines)


Loader = Callable[[], list[dl.Pattern]]
Resolver = Callable[[str | None], str]


def _is_public(answer: str) -> bool:
    """Unknown counts as public: a push needs the network anyway, so fail closed."""
    return answer not in (PRIVATE, INTERNAL, NOT_GITHUB)


def _denylist_inside(top: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(top.resolve())
        return True
    except ValueError:
        return False


def _patterns(load: Loader, target: str, top: Path | None) -> tuple[list[dl.Pattern] | None, Verdict | None]:
    try:
        patterns = load()
    except dl.DenylistError as exc:
        return None, Verdict(True, target, f"{target} is public and the denylist cannot be used: {exc}")
    if top is not None and _denylist_inside(top, dl.default_path()):
        return None, Verdict(True, target, "the denylist sits inside this repository, so a push would publish it; "
                                           "move it out (default ~/.config/public-repo-guard/denylist.toml)")
    return patterns, None


def _answer(url: str, resolve: Resolver) -> tuple[str, str]:
    """(target, visibility) for one push URL. A host this tool cannot place (an SSH alias, a
    self-hosted server) is unknown, and unknown is treated as public."""
    slug = github_slug(url)
    if slug:
        return slug, resolve(slug)
    forced = resolve(None)  # not-github, unless the environment forces an answer
    if forced != NOT_GITHUB:
        return url, forced
    return url, (UNKNOWN if host_kind(url) == "unknown" else NOT_GITHUB)


def check_push(top: Path, remote: str | None, refs: list[str], load: Loader,
               resolve: Resolver = visibility, local_tips: list[str] | None = None,
               extra_urls: list[str] | None = None) -> Verdict:
    remote = remote or gitscan.push_remote(top)
    urls = list(dict.fromkeys([*(u for u in (extra_urls or []) if u), *gitscan.push_urls(top, remote)]))
    if not urls:
        return Verdict()  # git will refuse an unknown remote on its own
    public = [(target, answer) for target, answer in (_answer(u, resolve) for u in urls) if _is_public(answer)]
    if not public:
        return Verdict()
    target, answer = public[0]
    patterns, refusal = _patterns(load, target, top)
    if refusal:
        return refusal
    try:
        exclude = gitscan.present_locally(top, gitscan.remote_shas(top, remote))
        tips = local_tips if local_tips is not None else _tips(top, refs)
        findings = gitscan.scan_commits(top, tips, exclude, patterns)
        names = list(refs) if local_tips is not None else _pushed_names(top, refs)
        findings += _scan_ref_names(names, patterns)
    except gitscan.GitError as exc:
        return Verdict(True, target, f"{target} reads as {answer} and the scan could not finish: {exc}")
    if findings:
        return Verdict(True, target, f"this push would publish private names to {target} ({answer}):", findings)
    return Verdict()


def _pushed_names(top: Path, refs: list[str]) -> list[str]:
    """The branch and tag names a typed push can create on the remote: the refspecs, plus the
    current branch (and its upstream name) for a bare push or HEAD, plus every local branch for
    `:`, --all, --branches or --mirror, and every tag for --tags or --mirror."""
    names = [r for r in refs if not r.startswith("-")]
    sources = [r.lstrip("+").split(":", 1)[0] for r in names]
    if not names or "HEAD" in sources:
        branch = gitscan.current_branch(top)
        if branch:
            names.append(branch)
            merge = gitscan.config(top, f"branch.{branch}.merge")
            if merge:
                names.append(merge.removeprefix("refs/heads/"))
    kinds = []
    if ":" in names or any(r in ("--all", "--branches", "--mirror") for r in refs):
        kinds.append("refs/heads")
    if any(r in ("--tags", "--mirror") for r in refs):
        kinds.append("refs/tags")
    if kinds:
        names += gitscan.git(top, "for-each-ref", "--format=%(refname:short)", *kinds).split()
    return names


def _tips(top: Path, refs: list[str]) -> list[str]:
    """The local commits a push names; everything local when the refspec cannot be read."""
    if any(r in ("--all", "--mirror", "--branches", ":", "+:") for r in refs):
        tips = ["--branches"]
        return tips + (["--tags"] if "--mirror" in refs else [])
    sources: list[str] = []
    for ref in refs:
        if ref == "--tags":
            sources.append("--tags")
            continue
        src = ref.lstrip("+").split(":", 1)[0]
        if not src:
            continue  # ":branch" deletes a remote branch and publishes nothing
        sources.append(src)
    if not refs or all(r == "--tags" for r in refs):
        sources.insert(0, "HEAD")
    resolved: list[str] = []
    for src in sources:
        if src == "--tags":
            resolved.append(src)
            continue
        out = gitscan.git(top, "rev-parse", "--verify", "--quiet", f"{src}^{{commit}}", check=False).strip()
        if not out:
            return ["--branches", "--tags"]  # a superset is safe; a guess is not
        resolved.append(out)
    return resolved


def _scan_ref_names(names: list[str], patterns: list[dl.Pattern]) -> list[dl.Finding]:
    found: list[dl.Finding] = []
    for ref in dict.fromkeys(names):
        if ref.startswith("-"):
            continue
        for part in dict.fromkeys(ref.lstrip("+").split(":")):
            if part and part != "HEAD":
                found += dl.scan_name(part, patterns, f"branch or tag name {part!r}")
    return found


def check_make_public(source: Path | None, words: list[str], load: Loader, slug: str | None) -> Verdict:
    target = slug or "a repository being made public"
    top = gitscan.toplevel(source) if source else None
    patterns, refusal = _patterns(load, target, top)
    if refusal:
        return refusal
    findings = dl.scan_text(" ".join(words), patterns, "the gh command's words")
    if source is not None and top is None and source.exists():
        findings += gitscan.scan_tree(source, patterns)
    elif top is not None:
        try:
            branches = gitscan.git(top, "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/tags")
            for name in branches.split():
                findings += dl.scan_name(name, patterns, f"branch or tag name {name!r}")
            if gitscan.git(top, "rev-parse", "--verify", "--quiet", "HEAD", check=False).strip():
                findings += gitscan.scan_commits(top, ["--branches", "--tags"], [], patterns)
        except gitscan.GitError as exc:
            return Verdict(True, target, f"the history being made public could not be read: {exc}")
    elif source is None:
        return Verdict(True, target, "a repository is being made public and this tool cannot tell which "
                                     "folder holds it; run the command from that folder")
    if findings:
        return Verdict(True, target, f"making {target} public would publish private names:", findings)
    if not slug and top is not None:
        slug = github_slug(gitscan.remote_url(top, gitscan.push_remote(top)))
    if slug:
        remember(slug, PUBLIC)  # so the next push reads it as public, not a cached "private"
    return Verdict()


def check_gh_text(cwd: Path, repo: str | None, words: list[str], files: list[str], load: Loader,
                  resolve: Resolver = visibility) -> Verdict:
    slug = repo_arg_slug(repo) or repo_arg_slug(os.environ.get("GH_REPO"))
    if slug is None:
        top = gitscan.toplevel(cwd)
        if top is None:
            return Verdict(True, "the repository", f"cannot tell which repository this gh command writes to "
                                                   f"from {cwd}; name it with -R OWNER/NAME")
        # gh's own choice first: `gh repo set-default`, then the push remote
        remote = gitscan.gh_default_remote(top) or gitscan.push_remote(top)
        url = gitscan.remote_url(top, remote)
        if url is None:
            return Verdict(True, "the repository", f"cannot read the remote {remote!r}; name the repository "
                                                   f"with -R OWNER/NAME")
        slug, answer = _answer(url, resolve)
        slug = github_slug(url)
    else:
        answer = resolve(slug)
    target = slug or "the repository"
    if not _is_public(answer):
        return Verdict()
    patterns, refusal = _patterns(load, target, None)
    if refusal:
        return refusal
    findings = dl.scan_text(" ".join(words), patterns, "the gh command's words")
    for name in files:
        try:
            text = Path(name).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return Verdict(True, target, f"{target} is {answer} and the file {name} could not be read: {exc}")
        findings += dl.scan_text(text, patterns, os.path.basename(name))
    if findings:
        return Verdict(True, target, f"this would post private names on {target} ({answer}):", findings)
    return Verdict()


def check_action(action: Action, load: Loader, resolve: Resolver = visibility) -> Verdict:
    cwd = Path(action.cwd)
    if action.kind == "push":
        top = gitscan.toplevel(cwd)
        if top is None:
            # fail closed: a folder this tool cannot find is a push it cannot read
            return Verdict(True, str(cwd), f"cannot find the repository this push runs in ({cwd}); "
                                           f"run it from inside the repository, or with git -C <folder>")
        return check_push(top, action.remote, action.refs, load, resolve)
    if action.kind == "make_public":
        slug = repo_arg_slug(action.repo)
        if action.verb == "create":
            if action.source:
                return check_make_public(Path(action.source), action.words, load, slug)
            # no --source: gh makes an empty repository, so only the command's own words publish
            # (its name and description), wherever the command runs
            target = slug or action.repo or "a new public repository"
            patterns, refusal = _patterns(load, target, None)
            if refusal:
                return refusal
            findings = dl.scan_text(" ".join(action.words), patterns, "the gh command's words")
            if findings:
                return Verdict(True, target, f"creating {target} would publish private names:", findings)
            if slug:
                remember(slug, PUBLIC)
            return Verdict()
        source: Path | None = cwd if not action.repo else None
        if source is None and slug:
            top = gitscan.toplevel(cwd)
            if top is not None:
                here = github_slug(gitscan.remote_url(top, gitscan.push_remote(top)))
                if here and here.lower() == slug.lower():
                    source = top
        return check_make_public(source, action.words, load, slug)
    if action.kind == "gh_text":
        return check_gh_text(cwd, action.repo, action.words, action.files, load, resolve)
    return Verdict()
