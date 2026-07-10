#!/usr/bin/env python3
"""Capture the precheck review target as a compact "semi-diff", and discover any
project customization under .claude/precheck/.

Pure stdlib — no jq, no awk. Staging is NEVER mutated: tracked changes come from
`git diff HEAD` (read-only) and untracked files are rendered with
`git diff --no-index`, which bypasses the index entirely. Safe to kill mid-run.

Emits machine-readable KEY=value lines plus the changed-file list and diff stat;
the slash command pre-runs this in a ```! block, so its output is injected into
the prompt without any "run this then parse it" prose.

Usage: capture-diff.py [plan-file | git-range | free-text ...]
  - no arg          -> working-tree diff (staged + unstaged + untracked) vs HEAD
  - existing file   -> treated as a plan file; still diffs the working tree
  - valid git ref   -> treated as a git range (validated via git rev-parse)
  - anything else   -> treated as free-form focus text; diffs the working tree

Customization (all optional), under $PRECHECK_DIR (default .claude/precheck):
  config.json   -> orchestrator knobs (inlined below for the command to read)
  context.md    -> shared project context (CONTEXT_FILE)
  <dimension>.md-> per-reviewer rules (injected by the SubagentStart hook)
  exclude       -> gitignore-style pathspecs to drop from the diff
"""
import os
import re
import subprocess
import sys
import tempfile

_HUNK = re.compile(r"-(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))?")


def git(args):
    """Run a git command, return stdout as text (returncode ignored)."""
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout


def semi_diff(diff_text):
    """Reduce `git diff -U0` output to a semi-diff.

    Per file: the filename + status, removed lines WITH content and old line
    numbers (irrecoverable from the working tree), and added line RANGES only
    (a pointer — agents read the file for content). Input MUST be -U0.
    """
    out = []

    def fresh():
        return {
            "oldpath": "", "newpath": "", "rfrom": "",
            "old_null": False, "new_null": False,
            "status": "modified", "binary": False,
            "added": [], "removed": [], "oldln": 0,
            "in_hunk": False, "have": False,
        }

    st = fresh()

    def flush():
        if not st["have"]:
            return
        path = st["oldpath"] if st["new_null"] else (st["newpath"] or st["oldpath"])
        if not path:
            return
        status = st["status"]
        if st["old_null"]:
            status = "added"
        elif st["new_null"]:
            status = "deleted"
        label = f'{st["rfrom"]} -> {path}' if (status == "renamed" and st["rfrom"]) else path
        out.append(f"### {label}  [{status}]")
        if st["binary"]:
            out.append("  (binary)")
            out.append("---")
            return
        if st["added"]:
            out.append("added:   " + ", ".join(st["added"]))
        if st["removed"]:
            out.append("removed:")
            out.extend(st["removed"])
        if not st["added"] and not st["removed"]:
            out.append("  (no line changes - mode/rename only)")
        out.append("---")

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            flush()
            st = fresh()
            st["have"] = True
            parts = line.split(" ")
            if len(parts) >= 4:
                p3 = parts[2][2:] if parts[2].startswith("a/") else parts[2]
                p4 = parts[3][2:] if parts[3].startswith("b/") else parts[3]
                st["oldpath"], st["newpath"] = p3, p4
        elif line.startswith("new file mode"):
            st["status"] = "added"
        elif line.startswith("deleted file mode"):
            st["status"] = "deleted"
        elif line.startswith("rename from "):
            st["status"] = "renamed"
            st["rfrom"] = line[len("rename from "):]
        elif line.startswith("rename to "):
            st["newpath"] = line[len("rename to "):]
        elif line.startswith("Binary files "):
            st["binary"] = True
        elif not st["in_hunk"] and line.startswith("--- "):
            p = line[4:]
            if p == "/dev/null":
                st["old_null"] = True
            else:
                st["oldpath"] = p[2:] if p.startswith("a/") else p
        elif not st["in_hunk"] and line.startswith("+++ "):
            p = line[4:]
            if p == "/dev/null":
                st["new_null"] = True
            else:
                st["newpath"] = p[2:] if p.startswith("b/") else p
        elif line.startswith("@@"):
            m = _HUNK.search(line)
            if m:
                st["oldln"] = int(m.group(1))
                nstart = int(m.group(3))
                ncount = int(m.group(4)) if m.group(4) is not None else 1
                if ncount > 0:
                    st["added"].append(f"L{nstart}" if ncount == 1 else f"L{nstart}-{nstart + ncount - 1}")
                st["in_hunk"] = True
        elif st["in_hunk"] and line.startswith("-"):
            st["removed"].append(f"  L{st['oldln']}: {line[1:]}")
            st["oldln"] += 1
        elif st["in_hunk"] and line.startswith("+"):
            pass  # added content: range already recorded

    flush()
    return ("\n".join(out) + "\n") if out else ""


def is_git_range(arg):
    """Check if arg is a valid git revision or range (e.g. SHA, branch, main..HEAD)."""
    r = subprocess.run(["git", "rev-parse", arg], capture_output=True, text=True)
    return r.returncode == 0


def main():
    arg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    rng = plan = focus = ""
    if arg:
        if os.path.isfile(arg):
            plan = arg
        elif is_git_range(arg):
            rng = arg
        else:
            focus = arg         # free-form text (plan description, focus hint)

    precheck_dir = os.environ.get("PRECHECK_DIR", ".claude/precheck")

    # Pathspec: positive :/ (whole repo, cwd-independent) minus exclusions. The
    # .claude/precheck dir is always excluded; extra patterns come from .claude/precheck/exclude.
    excl = [f":(top,glob,exclude){precheck_dir}", f":(top,glob,exclude){precheck_dir}/**"]
    exclude_file = os.path.join(precheck_dir, "exclude")
    if os.path.isfile(exclude_file):
        with open(exclude_file, encoding="utf-8") as fh:
            for raw in fh:
                pat = raw.strip()
                if pat and not pat.startswith("#"):
                    excl.append(f":(top,glob,exclude){pat}")
    ps = ["--", ":/", *excl]

    project_root = git(["rev-parse", "--show-toplevel"]).strip() or os.getcwd()

    chunks = []
    if rng:
        chunks.append(git(["diff", "-U0", "--no-color", rng, *ps]))
    else:
        chunks.append(git(["diff", "-U0", "--no-color", "HEAD", *ps]))
        for f in git(["ls-files", "--others", "--exclude-standard", *ps]).splitlines():
            if f:
                chunks.append(git(["diff", "-U0", "--no-color", "--no-index", "/dev/null", f]))

    semi = semi_diff("\n".join(c for c in chunks if c))
    # Write under the session temp dir (Claude's, if set), in a precheck/ subdir —
    # not bare /tmp.
    tmp_base = os.environ.get("CLAUDE_CODE_TMPDIR") or os.environ.get("TMPDIR") or tempfile.gettempdir()
    tmp_dir = os.path.join(tmp_base, "precheck")
    os.makedirs(tmp_dir, exist_ok=True)
    fd, diff_file = tempfile.mkstemp(prefix="precheck-", suffix=".semidiff.txt", dir=tmp_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(semi)
    lines = semi.count("\n")

    out = [
        f"PROJECT_ROOT={project_root}",
        f"DIFF_FILE={diff_file}",
        f"SEMIDIFF_LINES={lines}",
    ]
    if plan:
        out.append(f"PLAN_FILE={plan}")
    if focus:
        out.append(f"FOCUS={focus}")
    if rng:
        out.append(f"RANGE={rng}")
    if lines == 0:
        out.append("DIFF_EMPTY=1")

    if os.path.isdir(precheck_dir):
        out.append(f"PRECHECK_DIR={precheck_dir}")
        ctx = os.path.join(precheck_dir, "context.md")
        if os.path.isfile(ctx):
            out.append(f"CONTEXT_FILE={ctx}")
        cfg = os.path.join(precheck_dir, "config.json")
        if os.path.isfile(cfg):
            out.append("--- config.json (begin) ---")
            with open(cfg, encoding="utf-8") as fh:
                out.append(fh.read().rstrip("\n"))
            out.append("--- config.json (end) ---")

    out += ["", "=== Changed files ==="]
    if rng:
        out.append(git(["diff", "--name-only", rng, *ps]).rstrip("\n"))
    else:
        tracked = git(["diff", "--name-only", "HEAD", *ps]).splitlines()
        cached = git(["diff", "--name-only", "--cached", *ps]).splitlines()
        out += sorted({f for f in (tracked + cached) if f})
        out.append("--- untracked ---")
        out.append(git(["ls-files", "--others", "--exclude-standard", *ps]).rstrip("\n"))

    out += ["", "=== Diff stat ==="]
    stat = git(["diff", "--stat", *( [rng] if rng else ["HEAD"] ), *ps]).splitlines()
    out.append(stat[-1] if stat else "")

    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
