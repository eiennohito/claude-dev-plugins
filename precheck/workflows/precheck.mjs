export const meta = {
  name: 'precheck',
  description: 'Parallel pre-commit code review: scout the environment, then fan reviewer subagents over the diff and return structured findings.',
  phases: [
    { title: 'Scout', detail: 'Capture diff, read config, check codex' },
    { title: 'Review' },
  ],
}

// args: { input: string, pluginRoot: string }
// input is the user's raw argument (plan file, git range, focus description, or empty).
// pluginRoot is the absolute path to the precheck plugin directory.
let a = {}
try {
  a = typeof args === 'string' ? JSON.parse(args) : (args || {})
} catch (_) {
  a = {}
}
const input = a.input || ''
const pluginRoot = a.pluginRoot || ''

const SCOUT = {
  type: 'object',
  additionalProperties: false,
  required: ['empty'],
  properties: {
    empty:          { type: 'boolean' },
    projectRoot:    { type: 'string' },
    diffFile:       { type: 'string' },
    summary:        { type: 'string' },
    plan:           { type: 'string', description: 'Plan file path, informal focus text, or "".' },
    files:          { type: 'array', items: { type: 'string' } },
    config:         { type: 'string', description: 'Raw JSON of .claude/precheck/config.json, or "{}".' },
    codexAvailable: { type: 'boolean' },
    codexPrompts:   { type: 'object', additionalProperties: { type: 'string' }, description: 'Codex dimension name → path to expanded prompt temp file.' },
  },
}

const FINDING = {
  type: 'object',
  additionalProperties: false,
  required: ['severity', 'file', 'line', 'symptom', 'diagnosis', 'direction'],
  properties: {
    severity:  { type: 'string', enum: ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'] },
    file:      { type: 'string', description: 'Path, or "" if not file-specific (e.g. plan coverage).' },
    line:      { type: 'string', description: 'Line or range ("42", "12-19"), or "" if N/A.' },
    symptom:   { type: 'string', description: 'Observable fact.' },
    diagnosis: { type: 'string', description: 'Root cause. Fold in attack scenario / quantified impact where relevant.' },
    direction: { type: 'string', description: 'Suggested fix direction (the concept, not code).' },
  },
}
const FINDINGS = {
  type: 'object',
  additionalProperties: false,
  required: ['findings'],
  properties: {
    findings: { type: 'array', items: FINDING },
    note:     { type: 'string', description: 'Optional: one line if nothing was found, or caveats.' },
  },
}

// ---------------------------------------------------------------------------
// Phase 1: Scout — capture diff, read config, check codex
// ---------------------------------------------------------------------------
phase('Scout')

const scout = await agent(
  `Input: ${input}\nPlugin root: ${pluginRoot}`,
  { agentType: 'precheck:precheck-scout', model: 'haiku', schema: SCOUT, phase: 'Scout', label: 'scout' }
)

if (!scout || scout.empty) {
  return { empty: true, dimensionsRun: [], dimensionsFailed: [], totalFindings: 0, findings: [] }
}

const projectRoot = scout.projectRoot || ''
const diffFile    = scout.diffFile || ''
const summary     = scout.summary || ''
const plan        = scout.plan || ''
const files       = Array.isArray(scout.files) ? scout.files : []

let cfg = {}
try { cfg = JSON.parse(scout.config || '{}') } catch (_) {}

// Default order = longest-running first so the slowest reviewers claim concurrency
// slots early. docs reliably runs longest; the tail is a heuristic.
const ALL   = ['docs', 'reusability', 'plan-coverage', 'quality', 'security', 'efficiency']
const dims  = (Array.isArray(cfg.dimensions) && cfg.dimensions.length) ? cfg.dimensions : ALL
const model = cfg.model || 'sonnet'

function ctx(extra) {
  return [
    `DIFF_FILE: ${diffFile}  (a semi-diff: removed lines carry content, added lines are ranges. Read it, then read source files for context.)`,
    `PROJECT_ROOT: ${projectRoot}`,
    summary ? `Diff summary: ${summary}` : '',
    files.length ? `Changed files:\n${files.map((f) => '  ' + f).join('\n')}` : '',
    extra || '',
    'Project context and per-dimension rules are injected into your context automatically. Return findings via the structured schema.',
  ].filter(Boolean).join('\n')
}

// ---------------------------------------------------------------------------
// Phase 2: Review — fan out built-in + custom + codex reviewers
// ---------------------------------------------------------------------------
phase('Review')

const builtins = dims.map((d) => () =>
  agent(
    ctx(d === 'plan-coverage' && plan ? `Plan / focus: ${plan}` : ''),
    { agentType: `precheck:precheck-${d}`, model, schema: FINDINGS, phase: 'Review', label: `review:${d}` }
  )
)

const customDefs = Array.isArray(cfg.custom) ? cfg.custom : []
const customs = customDefs.map((c) => () =>
  agent(
    `${c.instructions}\n\n${ctx('')}`,
    { agentType: 'general-purpose', model, schema: FINDINGS, phase: 'Review', label: `review:${c.name || 'custom'}` }
  )
)

// Codex-backed reviewers — only if the scout confirmed codex is installed and
// successfully expanded the prompt files.
const codexRaw = cfg.codex
const codexEntries = (scout.codexAvailable && codexRaw && typeof codexRaw === 'object' && !Array.isArray(codexRaw))
  ? Object.entries(codexRaw).filter(([k, v]) => !k.startsWith('$') && typeof v === 'string')
  : []
const codexModel  = (codexRaw && codexRaw['$model'])  || ''
const codexEffort = (codexRaw && codexRaw['$effort']) || ''
const codexPrompts = scout.codexPrompts || {}
const codexRuns = codexEntries
  .filter(([name]) => codexPrompts[name])
  .map(([name]) => ({
    name,
    thunk: () => agent(
      [
        `Dimension: ${name}`,
        `Prompt file: ${codexPrompts[name]}`,
        codexModel  ? `Codex model: ${codexModel}` : '',
        codexEffort ? `Codex effort: ${codexEffort}` : '',
        '',
        `DIFF_FILE: ${diffFile}  (semi-diff: removed lines carry content, added lines are ranges)`,
        `PROJECT_ROOT: ${projectRoot}`,
        summary ? `Diff summary: ${summary}` : '',
        files.length ? `Changed files:\n${files.map((f) => '  ' + f).join('\n')}` : '',
        plan ? `Plan / focus: ${plan}` : '',
      ].filter(Boolean).join('\n'),
      { agentType: 'precheck:codex-runner', model: 'haiku', schema: FINDINGS, phase: 'Review', label: `review:${name}(codex)` }
    ),
  }))

const labels  = [...dims, ...customDefs.map((c) => c.name || 'custom'), ...codexRuns.map((r) => r.name)]
const results = await parallel([...builtins, ...customs, ...codexRuns.map((r) => r.thunk)])

// Tag each finding with its source, flatten.
const findings = []
results.forEach((r, i) => {
  if (!r || !Array.isArray(r.findings)) return
  for (const f of r.findings) findings.push({ ...f, agent: labels[i] })
})

return {
  dimensionsRun:    labels.filter((_, i) => results[i] != null),
  dimensionsFailed: labels.filter((_, i) => results[i] == null),
  totalFindings:    findings.length,
  findings,
}
