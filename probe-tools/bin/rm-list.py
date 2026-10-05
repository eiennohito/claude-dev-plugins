#!/usr/bin/env python3
"""Delete exactly the paths listed in a reviewed file — the safe alternative
to `find -delete`, `find -exec rm`, and `xargs rm` (which guard-destructive.py
denies).

Usage: python3 rm-list.py /abs/path/to/list.txt

The list holds one absolute path per line (blank lines ignored). Every line is
validated before anything is deleted; if any is unsafe (relative, contains
`..`, top-level or home dir), nothing is deleted. Files and symlinks are
unlinked (links are not followed), directories are removed recursively, and
already-missing paths are reported, not treated as errors. Nothing is
expanded: `*` in a line is a literal character.
"""
import os
import shutil
import sys


def problem(p):
    if not p.startswith("/"):
        return "not an absolute path"
    if ".." in p.split("/"):
        return "contains `..`"
    norm = os.path.normpath(p)
    if norm.count("/") < 2 or norm == os.path.normpath(os.path.expanduser("~")):
        return "top-level or home directory"
    return None


def main():
    if len(sys.argv) != 2 or not sys.argv[1].startswith("/"):
        sys.exit("usage: rm-list.py /abs/path/to/list.txt")
    with open(sys.argv[1], encoding="utf-8") as f:
        paths = [line.rstrip("\n") for line in f if line.strip()]
    bad = [f"  {p}: {why}" for p in paths if (why := problem(p))]
    if bad:
        sys.exit("refusing to delete anything; unsafe entries:\n" + "\n".join(bad))
    deleted = missing = 0
    for p in paths:
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        elif os.path.lexists(p):
            os.unlink(p)
        else:
            missing += 1
            continue
        deleted += 1
    print(f"deleted {deleted} of {len(paths)} listed paths"
          + (f" ({missing} already missing)" if missing else ""))


if __name__ == "__main__":
    main()
