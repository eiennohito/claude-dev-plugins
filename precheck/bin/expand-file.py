#!/usr/bin/env python3
"""Expand @-includes in a file, write the result to a temp file, print its path.

Usage: expand-file.py <file-path> [project-root] [-o output-path] [--prepend file]

Resolves @path and @{path} references relative to project root first, then
.claude/precheck/. One level deep, skips unresolvable refs. Pure stdlib.

--prepend <file>  Expand another file and prepend it (e.g. context.md).
                  Silently skipped if the file doesn't exist.
-o <path>         Write there instead of a temp file.
Prints the output path to stdout.
"""
import os
import re
import sys
import tempfile

INCLUDE_RE = re.compile(r"@\{([^}]+)\}|@(\S+)")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _within(path, root):
    try:
        return os.path.commonpath(
            [os.path.realpath(path), os.path.realpath(root)]
        ) == os.path.realpath(root)
    except ValueError:
        return False


def _resolve(ref, search_dirs, root):
    for d in search_dirs:
        cand = os.path.normpath(os.path.join(d, ref))
        if os.path.isfile(cand) and _within(cand, root):
            return cand
    return None


def _expand(file_path, search_dirs, root):
    """Resolve file_path, expand @-includes, return expanded text or None."""
    resolved = None
    for d in search_dirs:
        cand = os.path.normpath(os.path.join(d, file_path))
        if os.path.isfile(cand):
            resolved = cand
            break
    if not resolved and os.path.isfile(file_path):
        resolved = file_path
    if not resolved:
        return None, set()

    text = _read(resolved)
    already = {resolved}
    blocks = [text.rstrip("\n")]

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

    return "\n\n".join(blocks), already


def _parse_args():
    out_path = prepend = None
    positional = []
    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a == "-o" and i + 1 < len(sys.argv):
            out_path = sys.argv[i + 1]; i += 2
        elif a == "--prepend" and i + 1 < len(sys.argv):
            prepend = sys.argv[i + 1]; i += 2
        else:
            positional.append(a); i += 1
    return positional, out_path, prepend


def main():
    positional, out_path, prepend = _parse_args()

    if not positional:
        print("Usage: expand-file.py <file-path> [project-root] [-o output] [--prepend file]", file=sys.stderr)
        sys.exit(1)

    file_path = positional[0]
    root = positional[1] if len(positional) > 1 else os.getcwd()
    pdir = os.path.join(root, ".claude/precheck")
    search_dirs = [root, pdir]

    parts = []

    if prepend:
        text, _ = _expand(prepend, search_dirs, root)
        if text:
            parts.append(text)

    text, _ = _expand(file_path, search_dirs, root)
    if text is None:
        print(f"ERROR: not found: {file_path}", file=sys.stderr)
        sys.exit(1)
    parts.append(text)

    expanded = "\n\n".join(parts) + "\n"

    if out_path:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(expanded)
        print(out_path)
    else:
        tmp_base = os.environ.get("CLAUDE_CODE_TMPDIR") or os.environ.get("TMPDIR") or tempfile.gettempdir()
        tmp_dir = os.path.join(tmp_base, "precheck")
        os.makedirs(tmp_dir, exist_ok=True)
        name = os.path.splitext(os.path.basename(file_path))[0]
        fd, tmp = tempfile.mkstemp(prefix=f"codex-{name}-", suffix=".prompt.md", dir=tmp_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(expanded)
        print(tmp)


if __name__ == "__main__":
    main()
