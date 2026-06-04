---
name: probe-tools-configure
description: Walk the user through creating or editing this repo's .claude/probe-tools.py — the per-project config that extends what the probe-tools plugin detects and injects at session start (extra tools, package managers, skips, and custom probes like justfile targets).
allowed-tools: "Bash Read Write Edit AskUserQuestion"
---

# probe-tools — configure `.claude/probe-tools.py`

Help the user create or edit the per-project config that the probe-tools plugin
reads at session start. **Be interactive and concise**: propose concrete options
from what's actually installed in this repo, confirm with the user, then write
the file. Don't lecture.

## Current state (auto-diagnosed)

```!
python3 "${CLAUDE_PLUGIN_ROOT}/bin/probe-tools.py" --diagnose
```

Read the diagnostic above:

- `CONFIG_EXISTS` — whether `.claude/probe-tools.py` already exists (→ edit) or not (→ create).
- `CONFIG_ERROR` — if not `none`, the existing config is broken; offer to fix it first.
- `DETECTED_TOOLS` / `DETECTED_PACKAGE_MANAGERS` — what the built-in probe already finds (don't re-add these).
- `CUSTOM_PROBE_SECTIONS` — sections the current `probe()` emits.
- The injected-context preview shows exactly what the agent receives today.

## What to configure

The config file is plain Python imported by the hook. Every field is optional —
only write what adds value. Walk the user through these, using `AskUserQuestion`
to confirm choices, skipping anything they don't need:

1. **`TOOLS`** — extra binaries to detect that aren't in the built-in set, with a
   description telling the agent *when/how* to use them. Scan the repo for signals
   (e.g. `*.tf` → `terraform`, `Dockerfile`/`compose.yaml` → `docker`,
   `*.k8s.yaml` → `kubectl`, a `Makefile`/`justfile` → `make`/`just`). Propose the
   ones that are both relevant to this repo and currently installed. Each entry:
   `{"cmd": "terraform", "desc": "Terraform — plan/apply infra here"}` or a
   `("cmd", "desc")` tuple. Matching a built-in `cmd` overrides its description.

2. **`PKG_MANAGERS`** — extra package-manager commands to probe beyond the defaults
   (`brew npm pnpm yarn bun pip uv cargo go`). Add from repo signals — e.g.
   `pyproject.toml`+poetry → `poetry`, `Gemfile` → `gem`/`bundle`, `pom.xml` → `mvn`.

3. **`SKIP`** — built-in tool names to suppress even if installed (the user doesn't
   want them suggested). Usually empty.

4. **`probe(api)`** — the high-value extension: custom detection that injects its
   own section(s). Use it to surface task-runner targets so the agent runs them
   instead of rediscovering them. The flagship example — **justfile targets**:

   ```python
   def probe(api):
       if api.which("just"):
           summary = api.run(["just", "--summary"], cwd=api.root)
           if summary:
               body = "\n".join(f"- `just {t}`" for t in summary.split())
               return {"title": "just targets", "body": body}
   ```

   Adapt the same pattern to what this repo has: `make` →
   `make -qp`/parse the `Makefile` for targets; `npm`/`pnpm` → read `scripts` from
   `package.json`; `cargo` → `cargo --list`. `probe()` may return `None`, a
   markdown string (→ `## Project` heading), a `{"title","body"}` dict (→ its own
   `## <title>` section), or a list mixing those. `api` exposes `which(cmd)`,
   `run(args, timeout=2, cwd=None)`, `version(cmd, args)`, `platform` (dict), and
   `root` (project dir). Keep it cheap and side-effect-free — it runs every session.

## How to proceed

- If `CONFIG_EXISTS=no`: build a fresh file from the user's confirmed choices.
  Start from the template (copy structure/comments from it), then trim to what
  they chose:
  ```!
  cat "${CLAUDE_PLUGIN_ROOT}/examples/probe-tools.py"
  ```
- If `CONFIG_EXISTS=yes`: read the current file and `Edit` it to add/adjust the
  agreed fields. If `CONFIG_ERROR` was non-`none`, fix that first.
- Write to `<PROJECT_DIR>/.claude/probe-tools.py` (create `.claude/` if missing).
  Use the `PROJECT_DIR` from the diagnostic.

## Finish

Re-run the diagnostic to confirm the config loads cleanly (`CONFIG_ERROR=none`)
and that the new tools/sections now appear:

```
python3 "${CLAUDE_PLUGIN_ROOT}/bin/probe-tools.py" --diagnose
```

Then summarize in 2–3 lines what you added, and note that the new config takes
effect on the **next session start** (or after `/reload-plugins` + a new session),
since detection runs in the `SessionStart` hook.

## Reference — plugin README

Consult for anything not covered (the full return contract, trust model, defaults):

```!
cat "${CLAUDE_PLUGIN_ROOT}/README.md"
```
