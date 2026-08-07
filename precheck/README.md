# precheck

Parallel pre-commit code review for Claude Code. `/precheck` captures your current
diff, fans out independent reviewer subagents, and synthesizes one severity-ranked
report. Language-agnostic out of the box; customizable per-project.

## How it works

```
/precheck [plan-file | git-range | focus]
        │
        ├─ bin/capture-diff.py   → writes a compact "semi-diff" to /tmp, leaves
        │                          git staging untouched, discovers .claude/precheck/
        ├─ workflows/precheck.mjs  deterministic parallel() fan-out:
        │     docs · reusability · plan-coverage · quality · security · efficiency
        │     (+ any custom reviewers you configure)
        └─ orchestrator merges, dedupes, deepens, and ranks findings
```

Design notes:
- **Low main-context cost.** Each reviewer's identity lives in `agents/*.md` and
  loads only inside that subagent's own context. The main session sees just the
  thin orchestrator and the final report.
- **Semi-diff, not full diff.** The artifact carries only what reviewers can't
  recover themselves: filenames, **removed lines with content**, and **added line
  ranges** (pointers). Reviewers read the actual source for context.
- **Never touches git staging.** Tracked changes come from `git diff HEAD`;
  untracked files render via `git diff --no-index`. No `git add` anywhere; safe
  to interrupt.

## Usage

```
/precheck                 # review working-tree changes (staged + unstaged + untracked)
/precheck main..HEAD      # review a git range
/precheck docs/plan.md    # note a plan file for the plan-coverage reviewer
```

## Customization — `.claude/precheck/`

Per-repo config lives in `.claude/precheck/`. Everything is optional; with no
`.claude/precheck/`, precheck runs all built-in reviewers with generic rules.

**Easiest setup:** run **`/precheck-config`** — it inspects the repo, drafts a
`context.md` from the detected stack, walks you through dimensions / model / custom
reviewers / excludes, and writes the files (it also *repairs* an existing config).
For manual setup, see [`examples/precheck/`](examples/precheck).

| File | Consumed by | Purpose |
|------|-------------|---------|
| `config.json` | orchestrator | which dimensions run, `model`, extra `custom` reviewers |
| `context.md` | **every** reviewer | shared project context: stack, priorities, threat model, where docs/plans live |
| `<dimension>.md` | that one reviewer | extra rules + severity calibration (`security.md`, `quality.md`, …) |
| `synthesis.md` | orchestrator (at synthesis) | addenda to the base synthesis spec: severity overrides, extra grouping, suppressions |
| `exclude` | capture script | gitignore-style globs dropped from the diff |

`context.md` / `<dimension>.md` can **include other in-repo files** with `@path`
or `@{path}` (like CLAUDE.md): the `@ref` stays in the text and the file's contents
are appended below it (`--- contents of <path> ---`). Paths resolve from the repo
root, must be inside the repo, are de-duped, and expand one level deep.

`context.md` and `<dimension>.md` reach the reviewers through a **`SubagentStart`
hook** (`hooks/hooks.json` → `bin/inject-context.py`): when a `precheck-*` reviewer
spawns, the hook reads the matching `.claude/precheck/` files and injects them as
`additionalContext`. The orchestrator threads no paths and the reviewers read no
files — customization is automatic and costs the main session nothing. (Custom
reviewers run as `general-purpose`, which the hook can't target, so the
orchestrator passes `context.md` to those in-prompt instead.)

`synthesis.md` reaches the orchestrator through a **`PostToolUse` hook**
(`hooks/hooks.json` → `bin/inject-synthesis.py`): when the orchestrator reads
`lib/synthesis.md`, the hook checks for `.claude/precheck/synthesis.md` and
appends its contents as a follow-up attachment. The base synthesis rules always
apply; per-project rules add to them (severity overrides, extra sections,
suppressions).

> All invocable scripts are Python 3.10+ stdlib (no `jq`, no `awk`). If `python3`
> is missing, hook injection no-ops silently and reviewers fall back to reading
> `.claude/precheck/` files themselves.

`config.json` knobs:
- `dimensions` — subset/order of built-ins to run. Default: all built-ins.
- `model` — model for reviewers. Default: `sonnet`.
- `custom` — array of `{ name, instructions }` extra reviewers run as general-purpose agents.

Customization files are read inside subagent contexts, so they add nothing to the
main session. To set up manually instead of `/precheck-config`:

```bash
mkdir -p .claude && cp -r "<plugin>/examples/precheck" .claude/precheck
# then edit .claude/precheck/* and delete what you don't need
```

## Components

```
precheck/
├── commands/precheck.md      /precheck command (launches the workflow)
├── commands/precheck-config.md  interactive setup/repair of .claude/precheck/
├── workflows/precheck.mjs    dynamic-workflow script (deterministic fan-out)
├── agents/precheck-*.md      built-in reviewer subagents (generic identities)
├── hooks/hooks.json          hook registrations (SubagentStart + PostToolUse)
├── lib/synthesis.md          report merge/format spec (read at synthesis)
├── bin/capture-diff.py       diff capture + semi-diff + .claude/precheck/ discovery + excludes
├── bin/inject-context.py     SubagentStart hook: injects .claude/precheck/ rules
├── bin/inject-synthesis.py   PostToolUse hook: appends .claude/precheck/synthesis.md
├── bin/config-doctor.py      inspects/validates .claude/precheck/ (used by /precheck-config)
├── examples/precheck/        copy-paste customization templates
└── spike/                    dev-only: builds a sample repo to exercise /precheck
```

## Architecture

`/precheck` launches a deterministic workflow (`workflows/precheck.mjs`) that fans
out reviewers via `parallel()`. Each reviewer is a tool-restricted subagent
(`agentType: 'precheck:precheck-<dim>'`) that returns structured findings via a
schema. The workflow returns those findings to the main session, which merges and
ranks them following `lib/synthesis.md`.

The command body stays tiny: a ` ```! ` block pre-runs the capture script so the
diff summary, file lists, discovered `.claude/precheck/` paths, and inlined
`config.json` are injected into the prompt at invocation. The orchestrator just
calls the Workflow tool and synthesizes the result.
