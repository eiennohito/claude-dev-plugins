---
name: precheck
description: Parallel code review before commit. Spawns reviewer subagents over the current diff (reusability, security, quality, efficiency, plan coverage, doc sync), then synthesizes a severity-ranked report. Customizable per-project via .claude/precheck/.
argument-hint: "[plan-file or git-range]"
allowed-tools: "Bash Read Agent"
disable-model-invocation: true
---

# Precheck — parallel code review

You orchestrate only: the review context is captured below; you spawn the reviewers and merge their reports. **You do not review code yourself.**

## Captured context

```!
python3 "${CLAUDE_PLUGIN_ROOT}/bin/capture-diff.py" $ARGUMENTS
```

The block above gives you `PROJECT_ROOT`, `DIFF_FILE` (the semi-diff path — pass it to agents, never inline its contents), the diff summary, the changed-file list, any `CONTEXT_FILE` / `PLAN_FILE` paths, and inlined `.claude/precheck/config.json` (if present). If `DIFF_EMPTY=1`, tell the user there's nothing to review and stop.

## Spawn reviewers — single message, concurrent

Run the configured reviewers (config `dimensions`; default all six, longest-first so the slowest claim slots first: `docs reusability plan-coverage quality security efficiency`). For each, call the Agent tool with `subagent_type: precheck-<dim>`, `model` from config (default `sonnet`), and a prompt containing **only**: `DIFF_FILE`, the diff summary, the changed-file list, `PROJECT_ROOT`, and the `$ARGUMENTS`/`PLAN_FILE` scope. Project `.claude/precheck/` context and per-dimension rules are injected into each built-in reviewer automatically by a SubagentStart hook — do **not** pass them. Reviewer identities live in the subagent definitions — don't restate them.

For each `config.custom` entry (if any): same call but `subagent_type: general-purpose`, prompt = its `instructions` + the same context + `CONTEXT_FILE` (if any — the hook does not reach custom reviewers, so pass the path here) + this contract: *read the semi-diff at `DIFF_FILE` (removed lines carry content; added lines are ranges — read source for context); return findings only as `[SEVERITY] file:line — symptom`, a one-line diagnosis, and a `→` direction; severities CRITICAL/HIGH/MEDIUM/LOW.*

## Synthesize

When all reviewers return, use the **Read** tool to read `${CLAUDE_PLUGIN_ROOT}/lib/synthesis.md`, then produce the report following the synthesis spec and any project-specific rules that appear after it.
