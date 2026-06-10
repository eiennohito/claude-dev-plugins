# precheck

Parallel pre-commit code review for Claude Code. `/precheck` captures your current
diff, fans out independent reviewer subagents, and synthesizes one severity-ranked
report. Language-agnostic out of the box; customizable per-project.

## How it works

```
/precheck [plan-file | git-range]
        │
        ├─ bin/capture-diff.py   → writes a compact "semi-diff" to /tmp, leaves
        │                          git staging untouched, discovers .claude/precheck/
        ├─ spawns reviewers (Agent tool, concurrent):
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
`.claude/precheck/`, precheck runs all six reviewers with generic rules.

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
- `dimensions` — subset/order of built-ins to run. Default: all six.
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
├── commands/precheck.md      orchestrator A — prose/slash-command (the /precheck command)
├── commands/precheck-wf.md   orchestrator B — workflow variant (the /precheck-wf command)
├── commands/precheck-config.md  interactive setup/repair of .claude/precheck/
├── workflows/precheck.mjs    dynamic-workflow script (deterministic fan-out)
├── agents/precheck-*.md      six reviewer subagents (generic identities)   ← shared
├── hooks/hooks.json          hook registrations (SubagentStart + PostToolUse) ← shared
├── lib/synthesis.md          report merge/format spec (read at synthesis)  ← shared
├── bin/capture-diff.py       diff capture + semi-diff + .claude/precheck/ discovery + excludes ← shared
├── bin/inject-context.py     SubagentStart hook: injects .claude/precheck/ rules  ← shared
├── bin/inject-synthesis.py   PostToolUse hook: appends .claude/precheck/synthesis.md ← shared
├── bin/config-doctor.py      inspects/validates .claude/precheck/ (used by /precheck-config)
├── examples/precheck/        copy-paste customization templates
└── spike/                    dev-only: builds a sample repo to exercise both commands
```

## Two orchestrators, one engine

The reviewers, the hook, the capture, and synthesis are shared. Only the orchestration layer differs:

| | `/precheck` (A) | `/precheck-wf` (B) |
|---|---|---|
| Fan-out | main agent reads prose, spawns reviewers | `parallel()` in `workflows/precheck.mjs` |
| Determinism | model-driven | deterministic |
| Findings | collected from text output | structured (schema) → JS-tagged |
| Portability | runs anywhere | needs the **Workflow** feature (research preview, Claude Code ≥ 2.1.154) |
| Reviewers | `subagent_type: precheck-<dim>` | `agentType: 'precheck:precheck-<dim>'` |

Both leave git staging untouched, both auto-inject `.claude/precheck/` rules via the
SubagentStart hook (verified inside a workflow during development), and both hand
off to `lib/synthesis.md` for the final report. Keep both while comparing; drop one
once you've picked. `spike/setup-fixture.py` builds a sample repo seeded for every
dimension so you can A/B them.

The command body stays tiny because it pre-runs the capture script with a
` ```! ` block — the diff summary, file list, discovered `.claude/precheck/` paths, and
inlined `config.json` are injected into the prompt at invocation, so there's no
"run this, then parse the output" prose for the orchestrator to follow.
