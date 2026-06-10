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

## Step 1 — Launch workflow

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

## Step 2 — Prepare for synthesis

After the Workflow call returns, use the **Read** tool to read `${CLAUDE_PLUGIN_ROOT}/lib/synthesis.md` (unless you already have this session). Follow the synthesis spec and any project-specific rules that appear after it. Then state:

> Precheck workflow launched (task **XXXXX**). I will produce the report once it completes.

(Use the real Task ID from the Workflow tool result.)

You have no findings yet — do not fabricate, guess, or read the task output file. The workflow is still running.

## Step 3 — Synthesize when notified

When a `<task-notification>` arrives whose `<task-id>` matches the Task ID from Step 1 and whose `<status>` is `completed`, synthesize the report from its `<result>` `findings` array following the synthesis format. Note any `dimensionsFailed` in one line.
