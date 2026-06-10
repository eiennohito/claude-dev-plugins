#!/usr/bin/env python3
"""SubagentStart hook — expand codex prompt files for the scout agent.

Fires for precheck-scout. Reads config.json for codex entries, checks codex
availability, expands each prompt file (with context.md prepended), and injects
the file paths as additionalContext.

Output format (injected into scout context):
  CODEX_AVAILABLE=true|false
  CODEX_PROMPT_<name>=<path>   (one per entry, only if available)
"""
import json
import os
import shutil
import subprocess
import sys

BIN = os.path.dirname(os.path.abspath(__file__))
EXPAND = os.path.join(BIN, "expand-file.py")


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}

    if "precheck-scout" not in (data.get("agent_type") or ""):
        return

    root = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    cfg_path = os.path.join(root, ".claude/precheck/config.json")
    if not os.path.isfile(cfg_path):
        return

    try:
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (ValueError, OSError):
        return

    codex = cfg.get("codex")
    if not codex or not isinstance(codex, dict):
        return

    entries = {k: v for k, v in codex.items()
               if not k.startswith("$") and isinstance(v, str) and v}
    if not entries:
        return

    available = shutil.which("codex") is not None
    lines = [f"CODEX_AVAILABLE={'true' if available else 'false'}"]

    if available:
        ctx = os.path.join(root, ".claude/precheck/context.md")
        prepend = ["--prepend", ".claude/precheck/context.md"] if os.path.isfile(ctx) else []
        for name, prompt_file in entries.items():
            try:
                r = subprocess.run(
                    ["python3", EXPAND, prompt_file, root] + prepend,
                    capture_output=True, text=True, timeout=10,
                )
                path = r.stdout.strip()
                if r.returncode == 0 and path:
                    lines.append(f"CODEX_PROMPT_{name}={path}")
            except Exception:
                pass

    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": "\n".join(lines),
        }
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
