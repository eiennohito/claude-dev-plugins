# probe-tools

A Claude Code plugin that probes the machine at **session start** and
injects a concise capability summary into context — which modern CLI tools are
installed, which package managers exist, and any project-specific binaries — so
the agent reaches for the fast/available tool (`rg` over `grep -r`, `fd` over
`find`, `gh`, …) instead of guessing or burning tool calls discovering what's
there.

## What it does

On `SessionStart` (matcher `startup|resume|compact`) it runs
[`lib/probe.py`](lib/probe.py), which:

1. Reads basic platform facts (OS, release, arch, shell) — cross-platform. If
   the shell isn't bash, it adds a line saying the Bash tool runs in that shell;
   for zsh, that line also lists the options changed from zsh defaults
   (`zsh -c setopt`).
2. Probes a built-in set of modern-CLI tools and package managers with
   `command -v` + `--version`, concurrently.
3. Loads an optional per-project config (below) to extend/override the set.
4. Returns the result as `additionalContext` plus a one-line `systemMessage`.

It re-runs on `compact` because the summary is lost when context is summarized.

## Explicit subagent model

A `PreToolUse` check ([`lib/agent_model.py`](lib/agent_model.py))
denies any `Agent` tool call that doesn't set `model`, telling the agent to pick
one (sonnet by default, opus if sonnet falls short, fable only for complex plan
validation the user asked for). Without it, subagents inherit the session's
model, which is usually the most expensive one. Forks are exempt because they
always run on the parent model. So are agent types whose definition sets a
concrete `model:` (not `inherit`). The hook looks the type up in the project's
`.claude/agents/`, in `~/.claude/agents/`, and, for `plugin:name` types, in the
installed plugin's `agents/`. The session-start summary states the rule, so
the agent sets `model` on the first try instead of learning it from a denial.

Denials from all `PreToolUse` checks start with `Denied by probe-tools:`.
Claude Code labels them "hook error", but they are deliberate denials, not
crashes.

## How the hooks run

[`hooks/hooks.json`](hooks/hooks.json) registers one script,
[`bin/hook.py`](bin/hook.py), once per event; it routes on the event and tool
and imports only the checks that tool needs. The `PreToolUse` matcher lists
just the tools some check inspects (`Agent|Task|Bash|Monitor`), so other tool
calls start no process. Every check returns a deny reason or nothing; several
reasons are joined into one denial, and a check that crashes is skipped
rather than failing the call.

Shell commands are parsed once per call by
[`lib/shellparse.py`](lib/shellparse.py), a stdlib-only bash/zsh parser
(Python 3.8+). It turns the command into the simple commands that would run,
each with its context: loop, background (`&`, zsh `&!`/`&|`), subshell,
pipeline, nesting inside `$(…)`/`eval`/`sh -c`/`bash <<EOF`, or a remote
`ssh` command. Words keep their quoting and expansions, so quoted text,
comments and heredoc bodies fed to other programs are data, and `${D:-/}x`
is known to be unresolved. The guards below are written against that
structure, not regexes over the text. A command it can't parse (0.01% of
~27k real commands) is denied by the destructive guard with a hint and
allowed by the others.

Every tool call pays the hook process, so the budget is ~50 ms and never more
than 100 ms (Python startup is ~20 ms of that). Over the 360-command test
corpus the hook takes 34 ms median, 47 ms max.

Tests: `python3 tests/test_shellparse.py` (parser and guard unit tests),
`python3 tests/test_rules.py` (command rules),
`python3 tests/test_corpus.py [--report]` (verdicts on 360 labelled real agent
commands from bash and zsh sessions), `python3 tests/test_hook.py` (end to end
through `sh -c`, with the time budget).

## Command rules

Your own deny rules for shell commands, matched against the parsed command
instead of the text, as a replacement for `Bash(…)` permission rules.
Permission rules glob over the text of each `;`/`&&` part, so `*` spans quoted
text (`Bash(git * push *)` denies `git log --grep push`) and prefix patterns
miss global options and wrappers (`Bash(git push*)` misses `git --no-pager push`
and `env X=1 git push`). Rules live in `~/.claude/probe-tools/rules.json`
(user) and `<repo>/.claude/probe-tools/rules.json` (project); both apply:

```json
{"deny": [
  {"command": "git push", "reason": "Pushing is the user's call: say the branch is ready."},
  {"command": "git push -f|--force|--force-with-lease"},
  {"command": "make deploy"}
]}
```

`command` is a program name, then the leading positional words to match in
order (for git, after global options such as `-C`/`-c`/`--no-pager`) and
options that must appear anywhere (`-f` also matches `-fd`, `--hard` also
`--hard=…`); `a|b` gives alternatives. Wrappers (`sudo`, `env`, `timeout`,
`xargs`, …), `eval`, `sh -c '…'`, `bash <<EOF` and `ssh host '…'` are seen
through, and quoted text, comments and heredocs fed to other programs never
match. There is deliberately no `ask`: a prompt stalls long, unattended
sessions, so a rule denies with its `reason` as the instruction. The
session-start message shows how many rules loaded, and names any file that
failed to parse (such a file applies not at all).

## Destructive-command guard

A `PreToolUse` check on `Bash` ([`lib/destructive.py`](lib/destructive.py))
catches honest mistakes, not malice: the agent runs A, but what executes is B.
It does not judge whether destroying the intended target is OK. That depends
on context, such as a checkpoint taken with `git stash` or the user asking for
it, and is left to permission rules and the auto-mode classifier. The hook only
denies `rm`, `rm-list.py`, `find -delete`, and git commands that can lose work
when the command text doesn't pin down the target:

- **cwd drift.** A relative `rm` target, or a git command without `-C`, is
  allowed only when the session's cwd is the project root (or a linked worktree
  of it, where worktree-isolated subagents run) and the command doesn't `cd`
  elsewhere first. An in-command `cd` isn't trusted: it persists into later
  calls, and Claude Code strips a `cd` to the current directory. For git, the
  denial says to rerun with `git -C <cwd>` if that repo was meant, or to `cd`
  to the project root otherwise. Only git that can lose work is checked
  (`reset --hard`, `checkout`/`restore` of paths, `clean`, `rebase`,
  `stash drop`, `branch -D`, force push, …). Read-only and additive git
  (`status`, `log`, `add`, `commit`, `stash` push, …) loses nothing in the
  wrong repo.
- **Expansion.** A variable that this command doesn't assign, or a `$(…)`,
  can't be part of a recursive target's path: an empty `$DIR` turns
  `rm -rf $DIR/x` into `rm -rf /x`, and `${DIR:-/}x` into `rm -rf /x`. A whole-variable target (`"$tmpdir"`) is
  fine, because empty means no operand. So is an unresolved filename for a
  non-recursive `rm`. Literal assignments in the same command, `~`, `$HOME`,
  and globs resolve.
- **Catastrophic targets.** `/`, a top-level directory, or home, including a
  bare `*` directly under them.
- **Runtime-computed sets.** `find -delete`, `find -exec rm`, `xargs rm`, and
  a force push without an explicit remote and refspec. For deletion, the
  denial message gives the safe alternative in three separate calls: write the
  candidates to a list file (`find /abs/dir … > /abs/list.txt`), review it,
  then run `python3 bin/rm-list.py /abs/list.txt`.
  [`rm-list.py`](bin/rm-list.py) deletes exactly the listed absolute paths and
  never expands anything. It validates every line first and deletes nothing if
  any line is unsafe.

Wrappers are seen through (`sudo`, `env`, `noglob`, `timeout`, `xargs`, …),
as are `eval`, `sh -c '…'` and `bash <<EOF`. Commands run on another host
through `ssh` aren't checked: their cwd and variables are the remote's. Most
denials are `cd /abs && rm rel` or `cd /abs && git checkout/rebase`, which
cost one retry with an absolute path or `git -C`. A commit message or heredoc
that mentions these commands isn't flagged.

## Process-wait guard

A `PreToolUse` check on `Monitor` and `Bash` ([`lib/monitor.py`](lib/monitor.py))
denies waits that can hang the session or end too early:

- **Waiting on a process by name.** Claude Code runs every command inside
  `<shell> -c "… eval '<command>'"`, so `pgrep -f X` always matches that
  wrapping shell, whose argv contains X: `while pgrep -f X; do sleep 5; done`
  runs until the timeout. Without `-f`, `pgrep`/`pidof` match the truncated
  process name (15 chars on Linux), so
  long script names and processes under a wrapper (`uv`, `npm`, `python3`)
  never match. Denied in any `Monitor` command (`pgrep`, `pidof`,
  `ps … | grep`), and in `Bash` inside a `while`/`until … done` loop, also
  in a remote `ssh` command (sshd runs it through `<shell> -c` too). One-off
  `pgrep` inspection in Bash is allowed.
- **`pkill -f X` (both tools).** Its pattern is in the wrapping shell's argv,
  so it kills the command running it. The denial says to kill by PID.
- **Processes escaping the harness (Bash).** `nohup`, or an `&` that the same
  command never `wait`s on or kills (`setsid … &`, `disown`, `( … ) > log &`).
  Such a process throws away what a background Bash command gives for free
  (stdout+stderr capture into the task output file, an exit notification),
  runs unsupervised, and leaves no record in the session. There is no opt-out.
  A subagent's background tasks are killed when it finishes, so a job that
  must outlive the agent needs a supervisor. Long jobs such as training runs
  are babysat by the agent that stays alive, handed to the user, or submitted
  to the environment's scheduler (Slurm, a Kubernetes Job, …). Services and
  admin processes are registered with the environment's service manager
  (systemd, launchd, docker compose, …). zsh's `&!`/`&|` count; `&` inside
  quotes, an escaped `\&`, `&&`/`2>&1`/`&>`, and a detach inside a remote
  `ssh` command don't.

The denial gives the preferred options, in order:
1. To wait for a process, launch it as a background Bash command and add no
   Monitor. The harness captures stdout+stderr to the task output file and
   notifies on exit, including a crash or SIGKILL.
2. Use Monitor only for events that aren't the process exiting: output to
   react to while it runs (`tail -n +1 -F /abs/task.output | grep --line-buffered …`,
   while the background launch still reports the exit), or events outside any
   process, such as an external service. For persistent or important work,
   the process serves a websocket that Monitor binds to with `ws`.
3. For anything more complex, write a Python launcher script that watches the
   process and prints events, and launch it in the background, rather than
   emulating that in shell.

Heredoc bodies and comments are data, so writing a launcher script through a
heredoc isn't flagged.

## Install (local, unpublished)

```sh
claude --plugin-dir /path/to/claude-precheck/probe-tools
```

Hot-reload edits with `/reload-plugins`. Inspect hook firing with
`claude --debug hooks`.

## Per-project configuration (plugin-like)

Run **`/probe-tools-configure`** to have Claude walk you through creating or
editing the config: it shows what's already detected in your repo, proposes
relevant tools/package-managers/probes from the repo's signals, and writes the
file for you. Or write it by hand:

Drop a `.claude/probe-tools.py` in your repo. It is imported by the hook and may
define any of these (all optional):

| Name           | Type                          | Purpose                                                        |
| -------------- | ----------------------------- | -------------------------------------------------------------- |
| `TOOLS`        | list of dict / `(cmd, desc)`  | Extra tools to detect. A matching `cmd` overrides a built-in.  |
| `PKG_MANAGERS` | list of str                   | Extra package-manager commands to probe.                       |
| `SKIP`         | list of str                   | Built-in tool names to suppress even if installed.             |
| `probe(api)`   | function → section(s)         | Fully custom detection (see below).                            |

`api` passed to `probe()` exposes `which(cmd)`, `run(args, timeout=2, cwd=None)`,
`version(cmd, args=("--version",))`, `platform` (a dict with `name`, `release`,
`arch`, `shell`), and `root` (the project dir).

`probe()` may return any of:

- `None` / `""` — inject nothing;
- a **markdown string** — appended under a `## Project` heading;
- a **`{"title": str, "body": str}` dict** — rendered as its own `## <title>`
  section;
- a **list** mixing the above — several sections.

For example, to surface every [`just`](https://github.com/casey/just) recipe so
the agent runs `just <target>` instead of rediscovering the task runner:

```python
def probe(api):
    if api.which("just"):
        summary = api.run(["just", "--summary"], cwd=api.root)
        if summary:
            body = "\n".join(f"- `just {t}`" for t in summary.split())
            return {"title": "just targets", "body": body}
```

See [`examples/probe-tools.py`](examples/probe-tools.py) for a fuller worked
example.

### Trust & safety

`.claude/probe-tools.py` is your own repo's code and runs in the hook process —
the same trust model as `.claude/hooks/`. It is wrapped in `try/except`: any error
degrades to the built-in probe plus a short warning in the session message, never
a hard failure. The plugin itself never mutates anything; all probes are
read-only (`command -v` + `--version`).

## Built-in defaults

Tools: `rg fd jq yq bat eza delta fzf hyperfine tokei dust sd xsv gh watchexec`.
Package managers: `brew npm pnpm yarn bun pip uv cargo go`.
