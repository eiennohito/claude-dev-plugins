"""@-includes for .claude/precheck/ files, like CLAUDE.md's.

A file may pull in other in-repo files with `@path` or `@{path}`. The `@ref`
stays in the text and the referenced file's contents are appended below it:

    --- contents of <path> ---
    <contents>

References resolve relative to the project root, then .claude/precheck/; they
must be existing files inside the repo, are de-duped, and expand one level
deep (included files aren't scanned for `@refs`). Anything else (an
@mention, an email address) is left alone.
"""
import os
import re

INCLUDE_RE = re.compile(r"@\{([^}]+)\}|@(\S+)")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def search_dirs(root):
    return [root, os.path.join(root, ".claude/precheck")]


def within(path, root):
    """True if path is inside root (after resolving symlinks)."""
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)
    except ValueError:
        return False


def resolve(ref, dirs, root):
    for d in dirs:
        cand = os.path.normpath(os.path.join(d, ref))
        if os.path.isfile(cand) and within(cand, root):
            return cand
    return None


def include_blocks(text, root, already):
    """Include blocks for the @refs in text; `already` dedupes across calls."""
    blocks = []
    for m in INCLUDE_RE.finditer(text):
        ref = (m.group(1) if m.group(1) is not None else m.group(2)).strip()
        if not ref:
            continue
        path = resolve(ref, search_dirs(root), root)
        if not path or path in already:
            continue
        already.add(path)
        blocks.append(f"--- contents of {ref} ---\n{read(path).rstrip(chr(10))}")
    return blocks


def expand_file(file_path, root):
    """file_path's text followed by its include blocks, or None if missing.
    file_path resolves like a reference, else as given."""
    path = next((c for c in (os.path.normpath(os.path.join(d, file_path)) for d in search_dirs(root))
                 if os.path.isfile(c)), None)
    if path is None and os.path.isfile(file_path):
        path = file_path
    if path is None:
        return None
    text = read(path)
    return "\n\n".join([text.rstrip("\n")] + include_blocks(text, root, {path}))
