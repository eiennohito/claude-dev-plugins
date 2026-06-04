---
name: precheck-config
description: Guide the user through setting up or repairing this repo's precheck customization in .claude/precheck/ (project context, dimensions, model, custom reviewers, excludes).
allowed-tools: "Bash Read Write Edit AskUserQuestion"
---

# Precheck — configure `.claude/precheck/`

Help the user create or fix their per-repo precheck configuration. **Be interactive and concise**: propose sensible defaults from what the repo tells you, confirm with the user, then write the files. Don't lecture.

## Current state (auto-diagnosed)

```!
python3 "${CLAUDE_PLUGIN_ROOT}/bin/config-doctor.py"
```

Read the diagnostic above: `DIR_EXISTS`, which files are present, any `config.json` problems, the repo's detected languages/docs/plan locations, and the `ISSUES` list with its `STATUS`.

## What to do

Branch on `STATUS`:

- **`STATUS=issues`** → **repair mode.** Walk each line in `ISSUES` and fix it in place: invalid JSON → correct the syntax; unknown dimension → map to the right one from `reusability, security, quality, efficiency, plan-coverage, docs` (confirm the intent if ambiguous); malformed `custom` entry → fix or drop with the user's ok; unrecognized override `.md` → rename to a known dimension or confirm deletion; **broken `@include`** → fix the path or confirm removing the reference. Show the user what you changed.

- **`STATUS=absent`** → **setup mode.** Create `.claude/precheck/` and populate it (below).

- **`STATUS=ok`** → ask whether they want to review/extend the existing config (add a per-dimension rule, a custom reviewer, an exclude) or stop.

## Setup mode — build the config

Use the repo signals to make concrete proposals, then confirm with `AskUserQuestion` (don't ask what you can already infer). Everything is optional — only write files that add value.

1. **`context.md`** (highest value) — draft shared project context from the detected languages, docs location, and plan location: stack/architecture, priorities (e.g. perf vs correctness, threat model for security), conventions, and where docs/plans live. Show the draft; let the user edit. This is read by **every** reviewer.
   - Prefer **`@`-includes** over copying: reference existing repo docs with `@path` or `@{path}` (e.g. `Architecture: @docs/architecture.md`). The `@ref` stays in the text and the file's contents are appended for reviewers — paths resolve from the repo root, must be in-repo, are de-duped, and expand one level deep. Same syntax works in `<dimension>.md`. (Custom reviewers don't get this — built-in dimensions only.)

2. **`config.json`** — confirm:
   - `dimensions`: default is all six in longest-first order `["docs","reusability","plan-coverage","quality","security","efficiency"]`. Offer to drop any irrelevant ones.
   - `model`: default `sonnet`.
   - `custom`: optional extra reviewers as `{ "name": ..., "instructions": ... }` (e.g. accessibility, i18n, API-compat). Only add if the user wants them.
   Omit keys the user leaves at default — a smaller config is better.

3. **`<dimension>.md`** (optional) — per-reviewer rules/severity calibration for any dimension the user wants to tune (e.g. `security.md`: "raw SQL is CRITICAL"; `quality.md`: file-size thresholds).

4. **`exclude`** (optional) — gitignore-style globs to drop from review (lockfiles, generated/vendored code, snapshots). Propose from the repo if obvious.

Templates to copy structure/comments from: `${CLAUDE_PLUGIN_ROOT}/examples/precheck/`.

## Finish

After writing, re-run the doctor to confirm a clean result:

```
python3 "${CLAUDE_PLUGIN_ROOT}/bin/config-doctor.py"
```

Then summarize what you created/changed in 2–3 lines, and remind the user that `.claude/precheck/` is read automatically by `/precheck` and `/precheck-wf` (per-dimension rules via the SubagentStart hook). Note that `.claude/precheck/` is itself excluded from review.

## Reference — plugin README

Consult this for anything not covered above (how customization is consumed, the two orchestrators, the `@`-include rules):

```!
cat "${CLAUDE_PLUGIN_ROOT}/README.md"
```
