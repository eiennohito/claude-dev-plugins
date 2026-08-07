# precheck testbed (dev only)

`setup-fixture.py` builds a throwaway git repo whose working tree is seeded so
**every reviewer dimension has something real to find** — use it to exercise
`/precheck`.

## Build it

```bash
# run from the precheck plugin root
python3 spike/setup-fixture.py
cd /tmp/precheck-testbed
claude --plugin-dir "$(cd - && pwd)"
#   then, in the session:
/precheck
/precheck docs/plans/checkout.md   # point plan-coverage at the plan
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
