#!/usr/bin/env python3
"""probe-tools' only hook entry point: every event is routed from here.

hooks/hooks.json registers this script once per event, with the PreToolUse
matcher limited to tools some check needs, so other tool calls spawn nothing.
Each PreToolUse check returns a deny reason or None; their reasons are joined
into one deny. A Bash/Monitor command is parsed once (lib/shellparse.py) and
shared by every check that needs it, and a check's module is imported only
when its tool is called. A check that crashes is skipped, so a bug in one
never disables the others or fails the tool call; bad hook input → allow.

Budget: every tool call pays this process, so keep it under ~50 ms (about 20
of which is interpreter startup) and never over 100 ms; tests/test_speed.py
enforces it.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))


def pre_tool_use(data):
    tool = data.get("tool_name")
    checks = []
    if tool in ("Agent", "Task"):
        import agent_model
        checks.append(lambda: agent_model.check(data))
    elif tool in ("Bash", "Monitor"):
        command = (data.get("tool_input") or {}).get("command")
        if not isinstance(command, str):
            return []
        import destructive
        import monitor
        import rules
        from shellparse import View
        view = View(tool, command)
        cwd, root = data.get("cwd") or "", os.environ.get("CLAUDE_PROJECT_DIR") or ""
        checks += [lambda: rules.check(view, root), lambda: destructive.check(view, cwd, root),
                   lambda: monitor.check(view)]
    reasons = []
    for check in checks:
        try:
            reason = check()
        except Exception:  # noqa: BLE001 - a broken check must not block the call
            reason = None
        if reason:
            reasons.append(reason)
    return reasons


def main():
    try:
        data = json.load(sys.stdin)
        event = data.get("hook_event_name")
    except Exception:  # noqa: BLE001
        return
    if event == "SessionStart":
        import probe
        try:
            probe.main()
        except Exception:  # noqa: BLE001 - never fail session start
            pass
    elif event == "PreToolUse":
        reasons = pre_tool_use(data)
        if reasons:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "Denied by probe-tools: " + "\n\n".join(reasons),
                }
            }, ensure_ascii=False))


if __name__ == "__main__":
    main()
