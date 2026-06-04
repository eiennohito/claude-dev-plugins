# probe-tools

A single-hook Claude Code plugin that probes the machine at **session start** and
injects a concise capability summary into context — which modern CLI tools are
installed, which package managers exist, and any project-specific binaries — so
the agent reaches for the fast/available tool (`rg` over `grep -r`, `fd` over
`find`, `gh`, …) instead of guessing or burning tool calls discovering what's
there.

## What it does

On `SessionStart` (matcher `startup|resume|compact`) it runs
[`bin/probe-tools.py`](bin/probe-tools.py), which:

1. Reads basic platform facts (OS, release, arch, shell) — cross-platform.
2. Probes a built-in set of modern-CLI tools and package managers with
   `command -v` + `--version`.
3. Loads an optional per-project config (below) to extend/override the set.
4. Returns the result as `additionalContext` plus a one-line `systemMessage`.

It re-runs on `compact` because the summary is lost when context is summarized.

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
