#!/usr/bin/env python3
"""precheck's only hook entry point: every event is routed from here.

hooks/hooks.json registers this script once per event:
- SubagentStart for precheck-* agents: project rules for reviewers
  (lib/reviewer_context.py) and, for the scout, codex prompt files
  (lib/codex_prompts.py), joined into one additionalContext (JSON: plain
  stdout isn't added for SubagentStart).
- PostToolUse on Read: per-project synthesis rules after lib/synthesis.md
  (lib/synthesis_rules.py), as plain stdout.

PostToolUse fires on every Read, so non-matching paths must exit fast (keep it
well under ~50 ms; Python startup is ~20 of that). A part that crashes is
skipped; bad hook input → no output.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))


def subagent_start(data, root):
    import codex_prompts
    import reviewer_context
    parts = []
    for part in (reviewer_context, codex_prompts):
        try:
            text = part.additional_context(data, root)
        except Exception:  # noqa: BLE001 - one broken part mustn't drop the others
            text = None
        if text:
            parts.append(text)
    if parts:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": "\n\n".join(parts)}
        }, ensure_ascii=False))


def main():
    try:
        data = json.load(sys.stdin)
        event = data.get("hook_event_name")
    except Exception:  # noqa: BLE001
        return
    root = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    if event == "SubagentStart":
        subagent_start(data, root)
    elif event == "PostToolUse" and data.get("tool_name") == "Read":
        path = (data.get("tool_input") or {}).get("file_path")
        if not isinstance(path, str) or not path.endswith("/synthesis.md"):
            return
        import synthesis_rules
        try:
            text = synthesis_rules.text_after_read(data, root)
        except Exception:  # noqa: BLE001
            text = None
        if text:
            print(text)


if __name__ == "__main__":
    main()
