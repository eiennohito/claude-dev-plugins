"""SubagentStart: codex prompt files for precheck-scout.

Reads the `codex` entries of .claude/precheck/config.json, checks that codex is
installed, expands each prompt file (context.md prepended, @-includes resolved)
into a temp file, and returns lines for the scout's context:

  CODEX_AVAILABLE=true|false
  CODEX_PROMPT_<name>=<path>   (one per entry, only if available)
"""
import json
import os
import shutil
import tempfile

from includes import expand_file


def additional_context(data, root):
    if "precheck-scout" not in (data.get("agent_type") or ""):
        return None
    try:
        with open(os.path.join(root, ".claude/precheck/config.json"), encoding="utf-8") as fh:
            codex = json.load(fh).get("codex")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(codex, dict):
        return None
    entries = {k: v for k, v in codex.items() if not k.startswith("$") and isinstance(v, str) and v}
    if not entries:
        return None
    available = shutil.which("codex") is not None
    lines = [f"CODEX_AVAILABLE={'true' if available else 'false'}"]
    if available:
        context = expand_file(".claude/precheck/context.md", root)
        tmp_base = os.environ.get("CLAUDE_CODE_TMPDIR") or os.environ.get("TMPDIR") or tempfile.gettempdir()
        tmp_dir = os.path.join(tmp_base, "precheck")
        for name, prompt_file in entries.items():
            text = expand_file(prompt_file, root)
            if text is None:
                continue
            os.makedirs(tmp_dir, exist_ok=True)
            stem = os.path.splitext(os.path.basename(prompt_file))[0]
            fd, path = tempfile.mkstemp(prefix=f"codex-{stem}-", suffix=".prompt.md", dir=tmp_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n\n".join(p for p in (context, text) if p) + "\n")
            lines.append(f"CODEX_PROMPT_{name}={path}")
    return "\n".join(lines)
