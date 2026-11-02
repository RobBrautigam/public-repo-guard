# public-repo-guard

Stop private names reaching a public GitHub repository: client and company names, people, internal
hosts, the home-folder paths of the machine that wrote the code. It reads what a push would publish,
and only when the repository is public.

Secret scanners such as gitleaks and git-secrets look for credentials. This looks for the other thing
an open-source repository must not carry, the names you work with, and it is built for the way code
gets published now: by AI coding agents that write session notes, copy real data into fixtures, and
push on their own.

- **Silent unless public.** The same push to a private repository passes with no output, so your
  denylist can name everything you work on without getting in the way of private work.
- **Fails closed when public.** No denylist, or a scan that cannot finish, on a public push: refused,
  with the reason. A visibility it cannot read is treated as public and scanned.
- **Reads what GitHub does not hold yet:** every new commit's message, every added line, every added
  file name (binary and empty files included) and the branch and tag names the push creates. What the
  remote already holds comes from `git ls-remote`, never stale tracking refs. A line removed in a later
  commit is still caught, because the history goes public too.
- **Two doors, one denylist:** a git `pre-push` hook (any person, any agent, any editor) and a Claude
  Code `PreToolUse` hook that also reads what git never sees: `gh repo create --public`,
  `gh repo edit --visibility public`, and pull request and issue titles, bodies and comments.
- **The denylist never travels.** It lives outside your repositories (a public push is refused if the
  denylist sits inside the repository being pushed), and a refusal names the pattern id and the
  matched words, never the pattern.
- **No dependencies**, Python 3.11 or newer, no account. Network: one visibility read per repository
  every ten minutes, and on a public push, one `git ls-remote` to the remote being pushed to.

```
public-repo-guard: refused. this push would publish private names to ../remote.git (public):
  - commit 078a728 message, line 1: person (person-jane) matched 'jane-example'
  - tests/fixture.json (commit 078a728), line 2: client (client-northwind) matched 'Northwind Traders'
Take each one out (edit the file, reword the message, or amend the commit) and run it again. ...
```

## Why it exists

Picture an AI coding session told to close its work by writing a notes file into the repository and
merging it. Right for a private repository, wrong for a public one: the notes name the clients and
projects the session touched, and they land on a public main branch. A rule written in prose does not
stop that, because the agent writing the notes is following a different rule. So the check belongs
at the moment of action: the push itself.

## Quick start (two minutes, offline)

```
git clone https://github.com/RobBrautigam/public-repo-guard
cd public-repo-guard
python -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python examples/demo.py
pytest
```

`examples/demo.py` makes a throwaway repository and a throwaway "GitHub" (a local bare repository
the guard is told to treat as public), installs the hook, and pushes three times: a clean commit goes
through, a fixture copied from a "real client" is refused at `git push` and again at the agent hook,
and the rewritten fixture goes through. Nothing leaves your machine.

## Use it on your own repositories

1. **Write your denylist** outside every repository. Start from [`examples/denylist.toml`](examples/denylist.toml)
   (every name in it is invented):

   ```
   mkdir -p ~/.config/public-repo-guard
   cp examples/denylist.toml ~/.config/public-repo-guard/denylist.toml
   public-repo-guard check-denylist
   ```

   ```toml
   [[pattern]]
   id = "client-northwind"
   category = "client"
   regex = 'northwind[\s_.-]*traders'   # also matches Northwind-Traders, northwind_traders
   allow_paths = []                     # optional globs where the name may appear (a LICENSE line)
   ```

   Patterns are case-insensitive unless `case_sensitive = true`. `PUBLIC_REPO_GUARD_DENYLIST` points
   at another file.

2. **The git hook**, per repository: `public-repo-guard install` (it never overwrites a `pre-push`
   hook it did not write; it prints the line to add instead). The hook calls the Python that ran
   `install`, so install from the environment the package lives in.

3. **The AI agent hook** (Claude Code), once for every repository, in `~/.claude/settings.json`:

   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "Bash|PowerShell",
           "hooks": [{ "type": "command", "command": "public-repo-guard hook", "timeout": 300 }]
         }
       ]
     }
   }
   ```

   The hook reads the command the agent is about to run, follows `cd`, `git -C`, `bash -c`,
   `pwsh -Command`, `cmd /c`, `env`, `nice`, `sudo` and `timeout`, skips comments and heredoc bodies,
   and exits 2 with the findings when it refuses; Claude Code shows the agent the reason, and the agent
   fixes the file instead of pushing. Keep the timeout generous: Claude Code lets a command through
   when its hook times out, and the scan gives git up to three minutes on a large push.

4. **Before making an old repository public:** `public-repo-guard scan-tree .` reads every tracked
   file, and `public-repo-guard scan-range --all` reads the history of every branch and tag, first
   commit included (`scan-range origin/main..HEAD` reads only the commits in that range). The agent
   hook runs the full history scan on its own when it sees `gh repo create --public --source` or
   `gh repo edit --visibility public`.

## How it decides

| Step | What happens |
|---|---|
| Which repository | The push remote of the current branch (pushRemote, pushDefault, the branch's remote, the only remote, origin), or the remote or URL given; `-R` or the `repos/<owner>/<repo>` path for gh |
| Public or not | A ten-minute cache, then `gh api repos/<owner>/<repo>` (sees your private repositories when you are signed in), then GitHub's public REST API with no token; a 404 there means the world cannot see it. A remote it cannot place (an SSH host alias, a self-hosted server) is treated as public; GitLab, Bitbucket and local paths are not GitHub and pass |
| Not public | Pass, no output, the denylist is never read |
| Public, or unreadable | Load the denylist (refuse if missing or inside the repository), ask the remote which commits it holds, scan the rest: messages, added lines (`git log -p -U0`, merges by `--diff-merges=remerge`), added file names, branch and tag names |
| Findings | Refuse with up to twelve, each with the file and line or the commit, the category, the pattern id and the matched words |

`PUBLIC_REPO_GUARD_VISIBILITY=public|private` forces the answer (the demo and the tests use it), and
`PUBLIC_REPO_GUARD_CACHE` moves the cache file.

## What it does not cover

Be clear-eyed about these before you rely on it:

- **`git push --no-verify` skips the git hook.** The agent hook still reads that push, because it runs
  before the command and does not care about git's flags. A person who means to publish can.
- **A bare `git push` under `push.default=matching` or a configured `remote.<name>.push` refspec**:
  the agent hook reads the current branch only; the git hook reads every branch git actually sends.
- **A name split across two lines**, spelled letter by letter, or encoded (base64, UTF-16 text, a
  screenshot) is not found. The contents of files git treats as binary are skipped; their file names
  are read.
- **Commit author and committer names and emails** are not scanned; set them to what you want public.
- **Non-GitHub remotes** (GitLab, Bitbucket, a local path) pass: their visibility is not read.
- **The agent hook reads the command line**, not scripts it calls: a `make release` or a shell script
  that pushes is read only by the git hook, and so is a push written inside a heredoc body fed to a
  shell. A PowerShell `$variable` in a path is not expanded. Annotated tag messages and gists are not
  read.
- **Issues, wiki pages and releases already on GitHub** are not read; neither is anything done in
  GitHub's web page.

## Commands

| Command | What it does |
|---|---|
| `public-repo-guard hook` | Claude Code PreToolUse hook; stdin is the hook JSON; exit 2 refuses |
| `public-repo-guard pre-push REMOTE [URL]` | git pre-push hook; stdin is git's ref lines; exit 1 refuses |
| `public-repo-guard install [DIR] [--force]` | write the pre-push hook into a repository |
| `public-repo-guard scan-tree [DIR]` | every tracked file (or every file of a plain folder) |
| `public-repo-guard scan-range (RANGE \| --all) [--repo DIR]` | the commits of a range, e.g. `origin/main..HEAD`, or every branch and tag with `--all` |
| `public-repo-guard check-denylist` | load the denylist; print its count and categories, never its patterns |
| `public-repo-guard visibility OWNER/REPO` | print public, private, internal, unknown or not-github |

## Prior art

| Project | Stars (2026-11-02) | What it does | Difference |
|---|---|---|---|
| [gitleaks](https://github.com/gitleaks/gitleaks) | 30,584 | Finds secrets with regex rules, pre-commit and CI | Built for credentials; custom rules can match names, but it runs the same on private and public repositories |
| [trufflehog](https://github.com/trufflesecurity/trufflehog) | 28,817 | Finds and verifies leaked credentials | Verification of live keys; no notion of private names |
| [git-secrets](https://github.com/awslabs/git-secrets) | 13,425 | Prohibited patterns in git hooks | No visibility check; no agent or gh coverage |
| [destructive_command_guard](https://github.com/Dicklesworthstone/destructive_command_guard) | 6,239 | Blocks destructive commands from AI agents | Stops deletes and visibility changes; does not read what a push publishes |

## License

MIT. See [LICENSE](LICENSE).
