#!/usr/bin/env python3
"""PreToolUse hook — deny Agent tool calls that don't set `model` explicitly.

Registered (hooks/hooks.json) for PreToolUse matcher "Agent|Task" ("Task" is the
tool's older name). Without an explicit model a subagent inherits the session's
model, which is usually the most expensive one; forcing the choice makes the
agent pick a cheaper model for routine delegation. Forks are exempt: they always
run on the parent model and ignore `model`.

Never fails the tool call on its own errors: bad input → allow.
"""
import json
import sys

REASON = (
    "Specify the model explicitly. sonnet for most tasks, opus if sonnet fails "
    "to do what you expect from it, fable for complex plan validation when the "
    "user explicitly requests it (or discuss it with the user beforehand if you "
    "think that it is required)."
)


def main():
    try:
        tool_input = json.load(sys.stdin).get("tool_input") or {}
    except Exception:
        return
    if tool_input.get("subagent_type") == "fork":
        return
    if str(tool_input.get("model") or "").strip():
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": REASON,
        }
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
