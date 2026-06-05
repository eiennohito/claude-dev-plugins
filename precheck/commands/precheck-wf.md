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

## Step 1 — Read synthesis format

If you have not already read `${CLAUDE_PLUGIN_ROOT}/lib/synthesis.md` in this session, read it now (before launching the workflow).

## Step 2 — Launch workflow

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

Note the **Task ID** from the Workflow tool result.

## Step 3 — Wait then synthesize

The workflow runs in the background. After calling Workflow, **emit nothing** — no text, no tool calls.

**The sole trigger for synthesis** is a `<task-notification>` whose `<task-id>` matches the Task ID from Step 2 and whose `<status>` is `completed`. No other event (task reminders, attachments, progress updates, other task IDs) triggers synthesis.

When that notification arrives, synthesize the report from its `<result>` `findings` array following the synthesis format. Note any `dimensionsFailed` in one line.
