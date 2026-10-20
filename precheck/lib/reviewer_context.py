"""SubagentStart: per-project rules for precheck reviewers.

For agents whose type contains "precheck-", returns .claude/precheck/context.md
(shared) and .claude/precheck/<dimension>.md (this reviewer) with their
@-includes expanded (lib/includes.py), so reviewers get project rules
automatically — the orchestrator threads no paths and the reviewer reads no
files.
"""
import os

from includes import include_blocks, read

MARKER = "precheck-"


def additional_context(data, root):
    # agent_type may arrive bare ("precheck-security") or plugin-namespaced
    # ("precheck:precheck-security"). Take everything after the last "precheck-".
    agent_type = data.get("agent_type", "") or ""
    idx = agent_type.rfind(MARKER)
    dim = agent_type[idx + len(MARKER):] if idx >= 0 else ""
    if not dim:
        return None
    base = os.path.join(root, ".claude/precheck")
    included, parts = set(), []
    for path, header in ((os.path.join(base, "context.md"), "# Project context (.claude/precheck/context.md)"),
                         (os.path.join(base, f"{dim}.md"), f"# Project rules - {dim} (.claude/precheck/{dim}.md)")):
        if not os.path.isfile(path):
            continue
        text = read(path)
        parts.append("\n\n".join([header + "\n\n" + text.rstrip("\n")] + include_blocks(text, root, included)))
    return "\n\n".join(parts) or None
