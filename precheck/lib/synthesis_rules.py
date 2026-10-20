"""PostToolUse on Read: per-project synthesis rules after lib/synthesis.md.

When the orchestrator reads the plugin's lib/synthesis.md, returns
.claude/precheck/synthesis.md (with @-includes expanded) so it appears as a
follow-up attachment right after the base spec.
"""
import os

from includes import include_blocks, read

TRIGGER_SUFFIX = "precheck/lib/synthesis.md"


def text_after_read(data, root):
    file_path = (data.get("tool_input") or {}).get("file_path", "")
    if not isinstance(file_path, str) or not file_path.endswith(TRIGGER_SUFFIX):
        return None
    synth = os.path.join(root, ".claude/precheck/synthesis.md")
    if not os.path.isfile(synth):
        return None
    text = read(synth).rstrip("\n")
    blocks = include_blocks(text, root, set())
    return "\n".join(["# Project synthesis rules (.claude/precheck/synthesis.md)", "", text]
                     + ([""] + "\n\n".join(blocks).split("\n") if blocks else []))
