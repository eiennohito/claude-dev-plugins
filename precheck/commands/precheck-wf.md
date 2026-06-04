---
name: precheck-wf
description: Parallel pre-commit review via a dynamic workflow (deterministic fan-out). Same reviewers + .claude/precheck customization as /precheck; requires the Workflow feature (research preview).
argument-hint: "[plan-file or git-range]"
allowed-tools: "Bash Read Workflow"
disable-model-invocation: true
---

# Precheck — workflow

Deterministic fan-out via a workflow: you run the workflow, and report the structured findings it returns. **You do not review code yourself.**

## Captured context

```!
python3 "${CLAUDE_PLUGIN_ROOT}/bin/capture-diff.py" $ARGUMENTS
```

If `DIFF_EMPTY=1`, tell the user there's nothing to review and stop.

## Run the workflow

Call the **Workflow** tool with:
- `scriptPath`: `${CLAUDE_PLUGIN_ROOT}/workflows/precheck.mjs`
- `args` (a real JSON object, not a string), assembled from the captured keys above:
  - `diffFile`    ← `DIFF_FILE`
  - `projectRoot` ← `PROJECT_ROOT`
  - `summary`     ← the `=== Diff stat ===` line
  - `files`       ← the entries under `=== Changed files ===` (include untracked; drop the `--- untracked ---` separator)
  - `plan`        ← `PLAN_FILE` if present, else `""`
  - `config`      ← the inlined `.claude/precheck/config.json` object if present, else `{}`

The reviewers receive their `.claude/precheck/` context automatically via the SubagentStart hook — you don't pass it.

The Workflow runs in the background. Read synthesis format rules if needed and wait for completion.

## Synthesize

When the workflow completes, format its `findings` (each tagged with `agent`) into the report by following `${CLAUDE_PLUGIN_ROOT}/lib/synthesis.md`. Note any `dimensionsFailed` in one line.
