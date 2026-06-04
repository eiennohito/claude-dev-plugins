#!/usr/bin/env python3
"""SubagentStart hook — inject per-project precheck customization into reviewers.

Registered (hooks/hooks.json) to fire for agents whose type matches "precheck-".
Reads .claude/precheck/context.md (shared) and .claude/precheck/<dimension>.md
(this reviewer) from the project root and returns them as `additionalContext`, so
reviewers get project rules automatically — the orchestrator threads no paths and
the reviewer reads no files.

@-includes (like CLAUDE.md): the .md files may pull in other in-repo files with
`@path` or `@{path}`. The `@ref` is left intact in the text, and the referenced
file's contents are appended below as:

    --- contents of <path> ---
    <contents>

References resolve relative to the project root (then the precheck dir), must be
existing files inside the repo, are de-duped, and expand one level deep (the
included files are not themselves scanned for `@refs`).

Pure stdlib (no jq). JSON is emitted with ensure_ascii=False so arrows/em-dashes
in the rules survive intact.
"""
import json
import os
import re
import sys

MARKER = "precheck-"
INCLUDE_RE = re.compile(r"@\{([^}]+)\}|@(\S+)")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _within(path, root):
    """True if path is inside root (after resolving symlinks)."""
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
    """Return appended include-blocks for @refs in `text` that resolve to in-repo
    files. `text` itself is left untouched; `already` dedupes across the injection."""
    blocks = []
    for m in INCLUDE_RE.finditer(text):
        ref = (m.group(1) if m.group(1) is not None else m.group(2)).strip()
        if not ref:
            continue
        path = _resolve(ref, search_dirs, root)
        if not path or path in already:
            continue          # unresolved (e.g. an @mention/email) or already included
        already.add(path)
        content = _read(path).rstrip("\n")
        blocks.append(f"--- contents of {ref} ---\n{content}")
    return blocks


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}

    # agent_type may arrive bare ("precheck-security") or plugin-namespaced
    # ("precheck:precheck-security"). Take everything after the last "precheck-".
    agent_type = data.get("agent_type", "") or ""
    idx = agent_type.rfind(MARKER)
    if idx < 0:
        return  # not one of ours
    dim = agent_type[idx + len(MARKER):]
    if not dim:
        return

    root = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    base = os.path.join(root, ".claude/precheck")
    search_dirs = [root, base]   # @refs resolve repo-root-first, then the precheck dir
    included = set()
    parts = []

    def add(path, header):
        if not os.path.isfile(path):
            return
        text = _read(path)
        block = header + "\n\n" + text.rstrip("\n")
        inc = _expand(text, search_dirs, root, included)
        if inc:
            block += "\n\n" + "\n\n".join(inc)
        parts.append(block)

    add(os.path.join(base, "context.md"), "# Project context (.claude/precheck/context.md)")
    add(os.path.join(base, f"{dim}.md"), f"# Project rules - {dim} (.claude/precheck/{dim}.md)")

    if not parts:
        return  # no customization for this dimension

    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": "\n\n".join(parts),
        }
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
