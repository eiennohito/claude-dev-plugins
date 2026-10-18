# probe-tools

A Claude Code plugin that probes the machine at **session start** and
injects a concise capability summary into context — which modern CLI tools are
installed, which package managers exist, and any project-specific binaries — so
the agent reaches for the fast/available tool (`rg` over `grep -r`, `fd` over
`find`, `gh`, …) instead of guessing or burning tool calls discovering what's
there.

## What it does

On `SessionStart` (matcher `startup|resume|compact`) it runs
[`bin/probe-tools.py`](bin/probe-tools.py), which:

1. Reads basic platform facts (OS, release, arch, shell) — cross-platform. If
   the shell isn't bash, it adds a line saying the Bash tool runs in that shell;
   for zsh, that line also lists the options changed from zsh defaults
   (`zsh -c setopt`).
2. Probes a built-in set of modern-CLI tools and package managers with
   `command -v` + `--version`.
3. Loads an optional per-project config (below) to extend/override the set.
4. Returns the result as `additionalContext` plus a one-line `systemMessage`.

It re-runs on `compact` because the summary is lost when context is summarized.

## Explicit subagent model

A `PreToolUse` hook ([`bin/require-agent-model.py`](bin/require-agent-model.py))
denies any `Agent` tool call that doesn't set `model`, telling the agent to pick
one (sonnet by default, opus if sonnet falls short, fable only for complex plan
validation the user asked for). Without it, subagents inherit the session's
model, which is usually the most expensive one. Forks are exempt because they
always run on the parent model. So are agent types whose definition sets a
concrete `model:` (not `inherit`). The hook looks the type up in the project's
`.claude/agents/`, in `~/.claude/agents/`, and, for `plugin:name` types, in the
installed plugin's `agents/`. The session-start summary states the rule, so
the agent sets `model` on the first try instead of learning it from a denial.

Denials from all `PreToolUse` hooks start with `Denied by probe-tools:`.
Claude Code labels them "hook error", but they are deliberate denials, not
crashes.

## Destructive-command guard

A `PreToolUse` hook on `Bash` ([`bin/guard-destructive.py`](bin/guard-destructive.py))
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
  `rm -rf $DIR/x` into `rm -rf /x`. A whole-variable target (`"$tmpdir"`) is
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

Commands after the destructive one aren't inspected, since they can't change
its target. Replayed against ~23.7k historical Bash calls, it denied 0.37%. Most
denials are `cd /abs && rm rel` or `cd /abs && git checkout/rebase`, which
cost one retry with an absolute path or `git -C`. The hook parses with `shlex`, masks `$(…)` while
respecting quotes, and strips heredoc bodies, so a commit message that
mentions these commands isn't flagged. If it can't parse the command, it
denies with a hint.

## Process-wait guard

A `PreToolUse` hook on `Monitor` and `Bash` ([`bin/guard-monitor.py`](bin/guard-monitor.py))
denies waits that can hang the session or end too early:

- **Waiting on a process by name.** Claude Code runs every command inside
  `<shell> -c "… eval '<command>'"`, so `pgrep -f X` always matches that
  wrapping shell, whose argv contains X: `while pgrep -f X; do sleep 5; done`
  runs until the timeout. Without `-f`, `pgrep`/`pidof` match the truncated
  process name (15 chars on Linux), so
  long script names and processes under a wrapper (`uv`, `npm`, `python3`)
  never match. Denied in any `Monitor` command (`pgrep`, `pidof`,
  `ps … | grep`), and in `Bash` inside a `while`/`until … done` loop. One-off
  `pgrep` inspection in Bash is allowed.
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
  (systemd, launchd, docker compose, …). `&` inside quotes, an escaped `\&`,
  and `&&`/`2>&1`/`&>` don't count.

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

Replayed against ~23.9k historical Bash calls, it denied 151 (0.63%): 66
`nohup` launches, 45 other `&` escapes, and 40 name-based waits. The one
known false positive is an `&` inside `$'…'` quoting, which isn't masked. It also denied 4 of 59 `Monitor`
calls, all name-based waits. Heredoc bodies and comments
are ignored, so writing a launcher script through a heredoc isn't flagged.

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
