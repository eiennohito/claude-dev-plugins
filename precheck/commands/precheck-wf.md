---
name: precheck-wf
description: Parallel pre-commit review via a dynamic workflow (deterministic fan-out). Same reviewers + .claude/precheck customization as /precheck; requires the Workflow feature (research preview).
argument-hint: "[plan-file, git-range, or focus description]"
allowed-tools: "Read Workflow"
disable-model-invocation: true
---

# Precheck — workflow

Deterministic fan-out via a workflow: you run the workflow, and report the structured findings it returns. **You do not review code yourself.**

## Step 1 — Launch workflow

Before calling Workflow, write a **one-sentence summary** of the task you were
working on this session (the feature, fix, or refactor — not the diff contents).
If the session just started and you have no task context, leave `context` empty.

Call the **Workflow** tool with:
- `scriptPath`: `${CLAUDE_PLUGIN_ROOT}/workflows/precheck.mjs`
- `args` (a real JSON object, not a string):
  - `input`      ← `$ARGUMENTS` (verbatim user input — may be a plan file path, git range, focus description, or empty)
  - `pluginRoot` ← `${CLAUDE_PLUGIN_ROOT}`
  - `context`    ← your one-sentence task summary (or `""` if none)

## Step 2 — Prepare for synthesis

After the Workflow call returns, use the **Read** tool to read `${CLAUDE_PLUGIN_ROOT}/lib/synthesis.md` (unless you already have it this session). Follow the synthesis spec and any project-specific rules that appear after it. Then state:

> Precheck workflow launched (task **XXXXX**). I will produce the report once it completes.

Use the real Task ID from the Workflow tool result. Then stop. Event will come in time. Do not worry.

## Step 3 — Synthesize when notified

When a `<task-notification>` arrives whose `<task-id>` matches the Task ID from Step 1 and whose `<status>` is `completed`:

- If the result has `"empty": true`, tell the user there is nothing to review and stop.
- Otherwise, synthesize the report from its `findings` array following the synthesis format. Note any `dimensionsFailed` in one line.
