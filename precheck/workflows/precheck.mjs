export const meta = {
  name: 'precheck',
  description: 'Parallel pre-commit code review: fan reviewer subagents over the diff and return structured findings for the main session to synthesize.',
  phases: [
    { title: 'Review' },
  ],
}

// args is assembled by the /precheck-wf command from the capture output:
//   { diffFile, projectRoot, summary, files: string[], plan, config }
// The workflow does no I/O itself — reviewers read DIFF_FILE and source; their
// .claude/precheck/ context arrives via the SubagentStart hook (verified by the spike).
// args should arrive as a JSON object, but the orchestrator sometimes serializes
// it to a string when filling the Workflow tool call. Tolerate both so a stringy
// envelope doesn't silently blank out diffFile/files/config (which makes reviewers
// blind-explore the repo and drops custom reviewers).
let a = {}
try {
  a = typeof args === 'string' ? JSON.parse(args) : (args || {})
} catch (_) {
  a = {}
}
const cfg = a.config || {}
// Default order = longest-running first, so the slowest reviewers claim concurrency
// slots before the rest when the agent count exceeds the parallelism cap. docs
// reliably runs longest; the tail is a turn-count heuristic, refine with data.
const ALL = ['docs', 'reusability', 'plan-coverage', 'quality', 'security', 'efficiency']
const dims = (Array.isArray(cfg.dimensions) && cfg.dimensions.length) ? cfg.dimensions : ALL
const model = cfg.model || 'sonnet'
const files = Array.isArray(a.files) ? a.files : []
const diffFile = a.diffFile || ''
const projectRoot = a.projectRoot || ''
const summary = a.summary || ''
const plan = a.plan || ''

const FINDING = {
  type: 'object',
  additionalProperties: false,
  required: ['severity', 'file', 'line', 'symptom', 'diagnosis', 'direction'],
  properties: {
    severity: { type: 'string', enum: ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'] },
    file: { type: 'string', description: 'Path, or "" if not file-specific (e.g. plan coverage).' },
    line: { type: 'string', description: 'Line or range ("42", "12-19"), or "" if N/A.' },
    symptom: { type: 'string', description: 'Observable fact.' },
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
    note: { type: 'string', description: 'Optional: one line if nothing was found, or caveats.' },
  },
}

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

phase('Review')

const builtins = dims.map((d) => () =>
  agent(
    ctx(d === 'plan-coverage' && plan ? `Plan file to check coverage against: ${plan}` : ''),
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

const labels = [...dims, ...customDefs.map((c) => c.name || 'custom')]
const results = await parallel([...builtins, ...customs])

// Tag each finding with its source, flatten. Mechanical only — the main session
// does dedup / deepen / format per lib/synthesis.md.
const findings = []
results.forEach((r, i) => {
  if (!r || !Array.isArray(r.findings)) return
  for (const f of r.findings) findings.push({ ...f, agent: labels[i] })
})

return {
  dimensionsRun: labels.filter((_, i) => results[i] != null),
  dimensionsFailed: labels.filter((_, i) => results[i] == null),
  totalFindings: findings.length,
  findings,
}
