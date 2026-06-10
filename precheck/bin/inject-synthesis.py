#!/usr/bin/env python3
"""PostToolUse hook — append per-project synthesis rules after lib/synthesis.md.

Registered (hooks/hooks.json) to fire after Read tool calls. When the agent reads
the plugin's lib/synthesis.md, this hook checks for .claude/precheck/synthesis.md
in the project and prints its contents (with @ref expansion) so they appear as a
follow-up attachment the orchestrator sees right after the base spec.

Plain stdout (no JSON envelope) — PostToolUse hooks surface stdout as content,
unlike SubagentStart which needs a hookSpecificOutput wrapper.
"""
import json
import os
import re
import sys

TRIGGER_SUFFIX = "precheck/lib/synthesis.md"
INCLUDE_RE = re.compile(r"@\{([^}]+)\}|@(\S+)")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _within(path, root):
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)
    except ValueError:
        return False


def _resolve(ref, search_dirs, root):
    for d in search_dirs:
        cand = os.path.normpath(os.path.join(d, ref))
        if os.path.isfile(cand) and _within(cand, root):
            return cand
    return None


def _expand(text, search_dirs, root, already):
    blocks = []
    for m in INCLUDE_RE.finditer(text):
        ref = (m.group(1) if m.group(1) is not None else m.group(2)).strip()
        if not ref:
            continue
        path = _resolve(ref, search_dirs, root)
        if not path or path in already:
            continue
        already.add(path)
        content = _read(path).rstrip("\n")
        blocks.append(f"--- contents of {ref} ---\n{content}")
    return blocks


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return

    file_path = (data.get("tool_input") or {}).get("file_path", "")
    if not file_path.endswith(TRIGGER_SUFFIX):
        return

    root = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    base = os.path.join(root, ".claude/precheck")
    synth = os.path.join(base, "synthesis.md")
    if not os.path.isfile(synth):
        return

    text = _read(synth).rstrip("\n")
    search_dirs = [root, base]
    includes = _expand(text, search_dirs, root, set())

    parts = ["# Project synthesis rules (.claude/precheck/synthesis.md)", "", text]
    if includes:
        parts.append("")
        parts.extend("\n\n".join(includes).split("\n"))
    print("\n".join(parts))


if __name__ == "__main__":
    main()
