#!/usr/bin/env python3
"""Build a throwaway git repo with realistic, reviewable changes for exercising
BOTH precheck commands (/precheck and /precheck-wf) side by side.

The working tree is left with a staged + unstaged + untracked + deleted mix, and
the changes are seeded so every reviewer dimension has something real to find:

  reusability   TAX + subtotal logic duplicated between orders.py and discounts.py
  security      f-string SQL in orders.py; hardcoded API key in discounts.py
  quality       free functions, magic numbers, missing comment on the tax rule
  efficiency    N+1 query inside the order_total loop
  plan-coverage item 1 delivered, item 2 done WRONG, item 3 silently dropped,
                plus an unplanned deletion of legacy.py
  docs          README + architecture.md still reference get_user (renamed) and
                legacy.py (deleted)
  custom:naming get_user -> fetch_user inconsistency (config.json custom reviewer)

.claude/precheck/ ships context + a security rule + a custom reviewer + an exclude, so the
customization path (and the SubagentStart hook) is exercised too. deps.lock changes
but is excluded.
"""
import os
import shutil
import subprocess
import sys

PLUGIN = "/Users/hdymacuser/work/misc/claude-precheck/precheck"

BASELINE = {
    "README.md": (
        "# demo service\n\n"
        "## API\n"
        "- `get_user(user_id)` — look up a user by id.\n"
        "- `order_total(order)` — compute the taxed order total.\n\n"
        "`app/legacy.py` is kept for backward compatibility.\n"
    ),
    "docs/architecture.md": (
        "# Architecture\n\n"
        "- `app/users.py` — user lookup via `get_user`.\n"
        "- `app/orders.py` — order math (`order_total`).\n"
        "- `app/legacy.py` — deprecated, retained for back-compat.\n\n"
        "Plans live in `docs/plans/`.\n"
    ),
    "docs/plans/checkout.md": (
        "# Checkout plan\n\n"
        "- [ ] Add a discounts module (`app/discounts.py`).\n"
        "- [ ] Parameterize the orders SQL query.\n"
        "- [ ] Add refund support to `order_total`.\n"
    ),
    "app/users.py": (
        "import db\n\n\n"
        "def get_user(user_id):\n"
        '    return db.query("SELECT * FROM users WHERE id = ?", [user_id])\n'
    ),
    "app/orders.py": (
        "TAX = 0.2\n\n\n"
        "def order_total(order):\n"
        '    subtotal = sum(i["price"] * i["qty"] for i in order["items"])\n'
        "    return subtotal * (1 + TAX)\n"
    ),
    "app/legacy.py": "def old():\n    return None\n",
    "deps.lock": "lib==1.0.0\n",
    ".claude/precheck/context.md": (
        "# Project context\n\n"
        "Stack: a small Python service; persistence via a `db` module exposing\n"
        "`db.query(sql, params=None)` over SQLite. Plans live in `docs/plans/`.\n\n"
        "Priorities: correctness and security first — this is a payments-adjacent service.\n"
    ),
    ".claude/precheck/security.md": (
        "# security rules\n\n"
        "- All SQL must use `?` placeholders via `db.query(sql, params)`. String-interpolated\n"
        "  or f-string SQL is a CRITICAL finding.\n"
        "- No secrets in source (API keys, tokens). Flag any literal that looks like one.\n"
    ),
    ".claude/precheck/config.json": (
        "{\n"
        '  "dimensions": ["docs", "reusability", "plan-coverage", "quality", "security", "efficiency"],\n'
        '  "model": "sonnet",\n'
        '  "custom": [\n'
        '    { "name": "naming", "instructions": "Flag identifiers that leak implementation detail or diverge from the domain vocabulary (e.g. a get_/fetch_ inconsistency)." }\n'
        "  ]\n"
        "}\n"
    ),
    ".claude/precheck/exclude": "*.lock\n",
}

# Working-tree changes applied AFTER the baseline commit.
AFTER = {
    # rename get_user -> fetch_user (makes README + architecture stale; naming finding)
    "app/users.py": (
        "import db\n\n\n"
        "def fetch_user(user_id):\n"
        '    return db.query("SELECT * FROM users WHERE id = ?", [user_id])\n'
    ),
    # security (f-string SQL), efficiency (N+1 in loop), quality (free fn + magic + no comment)
    "app/orders.py": (
        "TAX = 0.2\n\n\n"
        "def order_total(order, db):\n"
        "    total = 0\n"
        '    for i in order["items"]:\n'
        "        row = db.query(f\"SELECT price FROM prices WHERE sku = '{i['sku']}'\")\n"
        '        total = total + row[0] * i["qty"]\n'
        "    return total * (1 + TAX)\n"
    ),
    # untracked: delivers plan item 1, but duplicates tax/subtotal logic + hardcodes a secret
    "app/discounts.py": (
        'API_KEY = "sk_live_abc123def456"\n'
        "TAX = 0.2\n\n\n"
        "def discounted_total(order, pct):\n"
        '    subtotal = sum(i["price"] * i["qty"] for i in order["items"])\n'
        "    taxed = subtotal * (1 + TAX)\n"
        "    return taxed * (1 - pct)\n"
    ),
    "deps.lock": "lib==1.1.0\n",  # excluded by .claude/precheck/exclude
}

DELETE = ["app/legacy.py"]   # unplanned deletion; architecture said "retained"
STAGE = ["app/users.py"]     # leave the rest unstaged → staged+unstaged+untracked+deleted mix


def write(root, rel, content):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def main():
    d = sys.argv[1] if len(sys.argv) > 1 else "/tmp/precheck-testbed"
    if os.path.exists(d):
        shutil.rmtree(d)
    os.makedirs(d)

    def g(*args):
        subprocess.run(["git", "-C", d, *args], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    subprocess.run(["git", "init", "-q", d], check=True)
    g("config", "user.email", "testbed@example.com")
    g("config", "user.name", "testbed")

    for rel, content in BASELINE.items():
        write(d, rel, content)
    g("add", "-A")
    g("commit", "-qm", "baseline")

    for rel, content in AFTER.items():
        write(d, rel, content)
    for rel in DELETE:
        os.remove(os.path.join(d, rel))
    g("add", *STAGE)

    print(f"testbed ready: {d}\n")
    print("working tree (review target):")
    print("  staged:    app/users.py (get_user -> fetch_user)")
    print("  unstaged:  app/orders.py (SQL injection + N+1), deps.lock (excluded), app/legacy.py (deleted)")
    print("  untracked: app/discounts.py (dup logic + hardcoded secret)")
    print()
    print("run BOTH and compare:")
    print(f"  cd {d}")
    print(f"  claude --plugin-dir {PLUGIN}")
    print("  /precheck            # prose orchestrator")
    print("  /precheck-wf         # workflow orchestrator")
    print("  /precheck docs/plans/checkout.md   # (optional) point plan-coverage at the plan")


if __name__ == "__main__":
    main()
