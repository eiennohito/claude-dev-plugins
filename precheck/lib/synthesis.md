# Precheck — synthesis

The orchestrator follows this when all reviewer subagents have returned. Produce
one merged, severity-ranked report from their findings.

## Step 1 — Collect and cluster
Gather all findings. Group co-located ones (multiple agents flagging the same
file/area). Deduplicate: if two agents flag the same root cause from different
angles, merge into one finding carrying both agent tags — don't list it twice.

## Step 2 — Deepen diagnosis
For each cluster, and for any standalone HIGH/CRITICAL finding:
- Treat the subagents' symptoms as ground truth and their root causes as best guesses.
- Attempt a deeper "why" — what structural or modeling issue explains this cluster across agents?
- Add it only if it improves on the subagent's diagnosis. If it doesn't, keep theirs. Don't fabricate depth.

## Step 3 — Format
Group by severity with a `### CRITICAL` / `### HIGH` / `### MEDIUM` / `### LOW`
markdown heading per group, in that order. Within a group, interleave across
agents (order by importance, not by agent). Keep each finding's agent tag(s),
e.g. `[security]` or merged `[security, quality]`.

Render findings as **plain markdown**. Do NOT wrap findings in code fences
(` ``` `); reserve a fenced block only for an actual code snippet *inside* a
finding. The shapes below are illustrative, not literal templates to echo.

A CRITICAL/HIGH/MEDIUM finding is one short block — a bolded header line followed
by indented detail lines:

- **[SEVERITY] [agent] `file:line`** — symptom (observable fact)
  - diagnosis: subagent's root cause
  - deeper: cross-cutting synthesis — include only when it adds insight beyond the subagent's diagnosis; otherwise omit
  - → suggested direction

A LOW finding is a single line (a registry of known minor issues, not action items):

- **[LOW] [agent] `file:line`** — symptom. → direction.

If an agent found nothing, say so in one line. No "everything looks good" filler.
