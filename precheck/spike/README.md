# precheck testbed (dev only)

`setup-fixture.py` builds a throwaway git repo whose working tree is seeded so
**every reviewer dimension has something real to find** — use it to exercise and
compare both commands (`/precheck` and `/precheck-wf`).

## Build it

```bash
python3 /Users/hdymacuser/work/misc/claude-precheck/precheck/spike/setup-fixture.py
cd /tmp/precheck-testbed
claude --plugin-dir /Users/hdymacuser/work/misc/claude-precheck/precheck
#   then, in the session:
/precheck            # prose orchestrator
/precheck-wf         # workflow orchestrator
```

Add `--debug hooks` to the `claude` invocation to watch the `SubagentStart` hook
fire `inject-context.py` as each reviewer spawns.

## What's seeded (the bait)

| Dimension | Bait |
|-----------|------|
| reusability | `TAX` + subtotal logic duplicated across `orders.py` and `discounts.py` |
| security | f-string SQL in `orders.py`; hardcoded `API_KEY` in `discounts.py` |
| quality | free functions, magic numbers, missing comment on the tax rule |
| efficiency | N+1 query inside the `order_total` loop |
| plan-coverage | item 1 delivered, item 2 done **wrong**, item 3 dropped; unplanned `legacy.py` deletion |
| docs | `README.md` + `architecture.md` still reference `get_user` (renamed) and `legacy.py` (deleted) |
| custom: naming | `get_user → fetch_user` inconsistency (from `.claude/precheck/config.json`) |

The working tree is a deliberate mix: **staged** (`users.py`), **unstaged**
(`orders.py`, deleted `legacy.py`, excluded `deps.lock`), and **untracked**
(`discounts.py`) — so capture's staged/unstaged/untracked/deleted handling and the
`*.lock` exclude both get tested.

`.claude/precheck/` ships `context.md`, a `security.md` rule, a custom `naming` reviewer,
and an `exclude` — exercising the customization + hook-injection path end to end.

## Comparing the two runs

Both should surface the same core findings; what you're comparing is the
*orchestration*: does `/precheck-wf` reliably fan out all dimensions and return
clean structured findings, and does `/precheck`'s prose path match it? Note any
divergence in coverage, dedup quality, or the final report.
