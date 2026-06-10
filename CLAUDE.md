# CLAUDE.md

This repo is a **Claude Code plugin marketplace** (`.claude-plugin/marketplace.json`,
see [`README.md`](README.md)) bundling two plugins: `precheck/` (parallel
pre-commit code review) and `probe-tools/` (session-start tool detection). Each
plugin's own docs live in its `README.md`
([precheck](precheck/README.md), [probe-tools](probe-tools/README.md)).

This file captures **durable, generalizable lessons about developing Claude Code
plugins** — gotchas, the cost model, and conventions. Every rule states its
**reason**, because rules without reasons don't generalize: knowing *why* lets you
apply the principle to cases not listed here, and override it when the reason
doesn't hold.

---

## Design methodology (read first)

- **Establish the capability/constraint envelope BEFORE designing.** Find out what
  is possible, impossible, or *unknown* first; only then design. *Reason: a design
  is a set of choices conditioned on constraints. Proposing structure before you
  know the constraints produces plans that collapse on contact with reality, and
  you can't evaluate trade-offs you can't see. When asked to design, lead with the
  envelope, mark unknowns explicitly, and resolve the load-bearing ones before
  committing.*
- **Spike the load-bearing unknowns with a throwaway before building on them.**
  *Reason: one cheap 14-second experiment (does the hook fire inside a workflow?
  does `agentType` resolve?) can invalidate or validate an entire architecture.
  Building first and discovering the assumption was wrong wastes far more. A spike
  also surfaces adjacent facts you didn't think to ask (it revealed the
  `plugin:agent` namespacing).*
- **Verify from primary sources, not memory.** Read the actual transcripts/logs,
  run the actual command, check the actual env. *Reason: plugin/workflow behavior
  is under-documented and version-dependent; confident-sounding recall is often
  wrong. Several "successful" runs here hid silent failures only visible in the
  transcript.*

---

## Context & cost discipline (the most important section)

The dominant cost in a long agent session is **re-reading the accumulated context
every turn**. Internalize this model:

- **Per-turn cost ≈ size of the live context.** Each main-session turn re-reads the
  whole conversation as `cache_read`. *Reason: this is how prompt caching bills —
  raw token totals are misleading; what matters is `(turns) × (context size)`.*
- **Cumulative cache-read over a session is ~quadratic.** Context grows each turn,
  so work done *late* in a session (where context is largest) is the most
  expensive place to do it. *Reason: Σ(context size per turn) over a growing
  session ≈ N²·g/2. Appending N orchestration turns at the end of a 400k-token
  session costs ~N×400k, which dwarfs everything else.*
- **Measure tools in their real deployment context, not a fresh session.** A
  pre-commit/review tool runs at the *end* of a ~400k-token impl session. *Reason:
  benchmarking it in an empty session makes main-session turns look ~free and
  inverts the conclusion (we wrongly concluded a workflow was "more expensive"
  from a fresh-session measurement).*
- **Push heavy/iterative work OUT of the bloated main context into isolated
  contexts.** Subagents and background workflows start with their *own* small
  context. *Reason: a subagent doing 17 turns in its own 30k context is far cheaper
  than 17 main-session turns at 400k each. The architecture that minimizes
  expensive main-context turns wins at end-of-session, even if it uses more total
  tokens.*
- **Keep the orchestrator's prose minimal and its turn count low.** Move heavy
  prose into subagent definitions / `lib/` files the agent reads on demand, and
  pre-run deterministic setup with a `` ```! `` block so its output is injected
  rather than reasoned about. *Reason: anything in the slash-command body or
  generated as orchestrator output is paid for in the expensive main context, on
  every invocation. Writing N agent prompts by hand is the orchestrator's biggest
  output cost; deriving them in code (a workflow) removes it entirely.*
- **In background workflows, do NOT poll or narrate while waiting.** Launch, then
  stop until the completion notification (the harness re-invokes you). *Reason:
  every "still running…" turn re-reads the full session context for no work — pure
  waste, and worst at end-of-session. The Workflow tool literally tells you
  "nothing more to do this turn."*

---

## Plugins: structure & mechanics

- **A plugin is a directory with `.claude-plugin/plugin.json` plus component dirs**
  (`commands/`, `agents/`, `hooks/hooks.json`, `workflows/`, `bin/`, `lib/`,
  `examples/`). *Reason: components are discovered by convention; `plugin.json` is
  the only required file.*
- **Plugin agents/commands are namespaced `plugin:name`** — e.g. a workflow spawns
  `agentType: 'precheck:precheck-security'`, NOT the bare frontmatter name.
  *Reason: verified empirically — the bare name fails with "agent type not found"
  and the engine lists only the `plugin:agent` forms. The redundant `precheck:precheck-`
  is just because the agent is named `precheck-security` inside the `precheck` plugin.*
- **`${CLAUDE_PLUGIN_ROOT}`** is available in command bodies, hooks, and `!` blocks
  to reference bundled files. **`${CLAUDE_PROJECT_DIR}`** points at the user's repo
  (documented for hooks; works in command `!` blocks but is undocumented there).
  *Reason: a plugin's files live outside the user's project, so plugin-relative
  paths need `CLAUDE_PLUGIN_ROOT`; project-relative work needs `CLAUDE_PROJECT_DIR`
  or `git rev-parse --show-toplevel` (the most portable).*
- **`@`-includes in a *command/agent* file resolve against the user's project, not
  the plugin dir.** To inline a plugin's own file (e.g. its README into a command),
  use a `` ```! cat "${CLAUDE_PLUGIN_ROOT}/..." `` `` block, not `@`. *Reason: `@`
  has no way to reach `CLAUDE_PLUGIN_ROOT`; `!`-injection works regardless of
  whether commands even support `@`.*
- **Slash commands and skills are the right home for explicit `/command` UX with
  `$ARGUMENTS`; skills are model-invoked.** *Reason: match the invocation model to
  the trigger — don't make something a skill if the user types it as `/x`.*
- **Command/skill instructions must be oblivious to plugin internals.** The session
  agent doesn't know about hooks, injection mechanisms, or plugin architecture.
  Tell it *what to do* ("use the Read tool to read X"), not *why* ("this triggers a
  hook that appends Y"). If a hook transparently enriches a tool result, the
  command should just say to follow whatever appears. *Reason: leaking internals
  confuses the agent (it may try to replicate the hook's job) and couples the
  instructions to the implementation.*

## Dev & debugging workflow

- **Load a local unpublished plugin with `claude --plugin-dir <abs-path>`**; hot-reload
  edits with `/reload-plugins`. *Reason: no need to publish to a marketplace while
  iterating.*
- **`claude --debug hooks`** prints hook fires/matchers/exit/stdout. *Reason: there
  is no "hook fired" marker in the transcript JSONL, so this is the only direct
  visibility into hook execution.*
- **Transcripts on disk** (read these to diagnose; you have Bash on the user's
  machine, so locate them yourself):
  - main session: `~/.claude/projects/<cwd-hash>/<session-id>.jsonl`
  - subagents: `…/<session-id>/subagents/agent-<id>.jsonl` (+ `.meta.json` with `agentType`)
  - workflow run state: `…/<session-id>/workflows/wf_<id>.json` — has `status`,
    `error`, `durationMs`, `result`, and per-agent `workflowProgress` with timings.
  *Reason: usage, timing, and silent failures are only fully visible here; the
  human-facing report can look perfect while the run was degraded.*
- **Usage fields per assistant message:** `output_tokens`, `cache_creation_input_tokens`,
  `cache_read_input_tokens`. Sum per file to compare runs. *Reason: this is the
  ground truth for the cost model above.*

## Subagents & parallelism

- **Reviewer/worker identities belong in `agents/*.md`, not in the orchestrator
  body.** The orchestrator dispatches by `subagent_type`/`agentType` and passes
  only dynamic context. *Reason: an agent's system prompt loads only in *its own*
  context; putting it in the command body pollutes the main context on every run.*
- **Schedule longest-running agents first when the agent count may exceed the
  concurrency cap** (`min(16, cores−2)`). *Reason (user correction): a barrier
  (`parallel`/single-message fan-out) finishes only when the slowest agent does; if
  a straggler can't get a slot until late, it sets the floor. Longest-processing-
  first minimizes wall-clock. Order only matters when count > cap.*
- **Let parallel reviewers overlap; deduplicate at synthesis instead of policing
  strict lanes.** *Reason (user correction): "avid" agents that flag things slightly
  outside their dimension improve recall, and merging co-located findings is cheap.
  Forcing rigid separation costs recall for little gain. (Note: a single upstream
  agent that *gates* others — e.g. a router that drops dimensions — is the opposite
  case: there, a wrong call silently loses coverage, so be conservative + log it.)*
- **Tool-restrict worker agents** (`tools: Read, Grep, Glob, Bash`) unless they need
  more. *Reason: a `general-purpose` agent carries the full toolset → much larger
  base context (≈5× cache-create here) and tends to over-explore. Smaller surface =
  cheaper + faster + more predictable.*

## Hooks (`SubagentStart`, `PostToolUse`)

- **`SubagentStart` fires for agents spawned inside a dynamic workflow**, matched on
  `agent_type`, and can inject context. *Reason: verified by spike — this is what
  lets per-project customization reach workflow reviewers automatically, with zero
  main-context cost.*
- **`SubagentStart` injects context only via JSON**
  (`{"hookSpecificOutput":{"hookEventName":"SubagentStart","additionalContext":"…"}}`);
  plain stdout is NOT added (unlike `UserPromptSubmit`/`SessionStart`). *Reason: you
  must emit the JSON envelope; printing text does nothing.*
- **`agent_type` may arrive bare (`precheck-security`) or namespaced
  (`precheck:precheck-security`) — handle both.** *Reason: which form arrives isn't
  guaranteed; deriving the key with "strip everything up to the last known prefix"
  survives either and won't break on hyphenated names like `plan-coverage`.*
- **`PostToolUse` on `Read` can transparently append per-project addenda to a
  plugin-bundled file.** The hook receives `tool_input.file_path` on stdin (JSON);
  match on the path suffix, then print plain text — it appears as an attachment the
  agent sees right after the Read result. No JSON envelope needed (unlike
  `SubagentStart`). *Reason: verified by spike — this lets per-project synthesis
  rules reach the orchestrator without changing the command body or the read
  instruction; the hook fires on every Read but exits immediately for non-matching
  paths (< 1ms).*
- **Prefer hook injection over having agents run commands.** When data can be
  prepared deterministically (file expansion, availability checks, config reads),
  do it in a `SubagentStart` hook and inject the result. An agent running shell
  commands to gather the same data costs turns, invites improvisation, and needs
  permission grants. *Reason: hooks are free (no tokens, no turns, no approvals),
  deterministic, and invisible to the agent — it just sees the injected context.*

## Dynamic workflows

- **You can "bundle" a workflow in a plugin** by shipping the script and invoking
  it from a command via `Workflow({scriptPath: "${CLAUDE_PLUGIN_ROOT}/workflows/x.mjs", args})`.
  *Reason: workflows aren't a plugin manifest component, but `scriptPath` accepts any
  path, and a command instructing the agent to call Workflow is a valid opt-in.*
- **The scripting API is undocumented publicly — the Workflow tool's own description
  is the spec.** *Reason: there's no reference page; reverse-engineer from the tool
  description and the bundled `/deep-research` script.*
- **Workflow scripts are pure coordination: no filesystem, shell, network, or
  `require`.** Do all I/O in the agents (or before launch, passing data via `args`).
  *Reason: the sandbox forbids it; e.g. diff capture must run in the command's `!`
  block and be passed in, not done in the script.*
- **`args` MUST be a real JSON value, not a stringified one — but the orchestrator
  often stringifies it anyway, so parse defensively in the script**
  (`typeof args === 'string' ? JSON.parse(args) : args`). *Reason: a stringified
  `args` makes every `args.field` `undefined`, silently blanking diff/config and
  dropping custom agents — and the run still "succeeds" because agents improvise
  from cwd. This was a real, hard-to-spot bug; the defensive parse is the fix.*
- **Other script gotchas:** plain JS (no TS types); `export const meta = {…}` must be
  a pure literal; no `Date.now()`/`Math.random()`/argless `new Date()` (they break
  resume); top-level `await`/`return` ARE allowed (the runtime wraps the body in an
  async fn — so `node --check` falsely reports "illegal return"; dry-run by wrapping
  the body in `(async()=>{…})()` with stubbed `agent`/`parallel`/`phase` instead).
  *Reason: these constraints come from the determinism/resume model; knowing them
  avoids writing scripts that won't parse or won't resume.*
- **`schema` on `agent()` forces structured output** (validated, retried) — use it
  so results bind in code and dedup/cluster is plain JS, not model work. *Reason:
  structured findings are mergeable deterministically and keep the synthesis cheap.*
- **Background + barrier:** a workflow returns immediately (feels instant) but
  actually completes when its slowest agent does (`parallel` is a barrier). Its
  result is the *only* thing the main session sees. *Reason: don't confuse the fast
  handoff with completion; and anything the main session must reason over has to be
  in the returned value.*

## Scripts, dependencies, temp files

- **Write invocable scripts in Python 3 stdlib — avoid `jq`/`awk`/bash-isms.**
  *Reason (user correction): fewer external dependencies, easier JSON handling, and
  one language for the whole plugin. JSON parsing/escaping in bash is fragile.*
- **Emit JSON with `ensure_ascii=False`.** *Reason: keeps `→`, `—`, and non-ASCII in
  injected rules human-readable instead of `\uXXXX`.*
- **Put temp files under `$CLAUDE_CODE_TMPDIR` (fallback `$TMPDIR` → system temp), in
  a named subdir — never bare `/tmp`.** *Reason: stays within the session's managed
  temp space (cleaned up, scoped), and the subdir keeps artifacts grouped.*
- **Degrade gracefully when an optional dependency is absent** (e.g. no `python3` →
  hook no-ops, agents self-read). *Reason: a customization feature should never hard-
  fail the core flow.*

## Git safety

- **Never mutate the user's git state** (no `git add`, no index changes). To include
  untracked files in a diff, use `git diff --no-index` (and `git diff HEAD` for
  tracked) — both read-only. *Reason (user correction): a review/inspection tool must
  be side-effect-free; `git add -N` mutates the index and can strand entries if the
  process dies mid-run. `--no-index` bypasses the index entirely.*
- **Make pathspecs cwd-independent and repo-scoped** (`-- :/ :(top,glob,exclude)…`).
  *Reason: behavior shouldn't depend on which subdir the command runs from.*

## Passing diffs/large data to agents

- **Don't hand agents a full diff; hand them what they can't reconstruct, and let
  them read source for the rest.** The "semi-diff" carries filenames + removed lines
  (with content, since they're gone) + added line *ranges* (pointers); agents read
  the files for added content and context. *Reason (user correction): full-diff
  context lines are recoverable from the working tree, so they're wasted tokens;
  reading real files also gives ground truth and surrounding context the diff lacks.*
- **Pass big payloads by path (to a temp file), not inline.** *Reason: inlining bloats
  whoever holds it; a path lets each isolated agent pull only what it needs into its
  own context.*

---

## Conventions in this repo

- **Per-project plugin config lives under `.claude/precheck/`** (not a root-level
  dotdir). *Reason (user correction): keep project config under the standard
  `.claude/` tree rather than adding another top-level dotdir.*
- **`.claude/precheck/` `*.md` files support `@path` / `@{path}` includes** (built-in
  reviewers only): the `@ref` stays in the text and the file's contents are appended
  (`--- contents of <path> ---`); repo-root-relative, in-repo only, de-duped, one
  level deep. *Reason: lets config reference existing repo docs instead of
  duplicating them, matching CLAUDE.md ergonomics.*
- **Default reviewer order is longest-first** (`docs` first). *Reason: see scheduling
  rule above.*
- **Validate config with a read-only "doctor" script** the setup command consumes.
  *Reason: deterministic diagnostics (valid JSON, known dimensions, resolvable
  includes) are more reliable than asking the model to eyeball config, and give
  repair-mode a concrete checklist.*
