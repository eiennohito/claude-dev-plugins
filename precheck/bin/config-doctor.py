#!/usr/bin/env python3
"""Inspect a project's .claude/precheck/ config and the repo, and print a
diagnostic for the /precheck-config command to act on.

Pure stdlib. Reports: which config files exist, whether config.json is valid and
well-formed (known dimensions, well-formed custom reviewers), plus repo signals
(languages, docs, plan locations) to seed a good context.md. Read-only.
"""
import json
import os
import re
import subprocess
import sys
from collections import Counter

INCLUDE_RE = re.compile(r"@\{([^}]+)\}|@(\S+)")
# extensions that mark an @ref as an intended file include (so we don't flag
# incidental @mentions / emails like dev@example.com as broken includes)
INC_EXTS = {".md", ".txt", ".rst", ".json", ".yaml", ".yml", ".toml", ".cfg",
            ".ini", ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt"}

KNOWN_DIMS = ["reusability", "security", "quality", "efficiency", "plan-coverage", "docs"]
DEFAULT_ORDER = ["docs", "reusability", "plan-coverage", "quality", "security", "efficiency"]
KNOWN_KEYS = {"dimensions", "model", "custom"}
EXT_LANG = {
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
    ".jsx": "JavaScript", ".rs": "Rust", ".go": "Go", ".java": "Java", ".kt": "Kotlin",
    ".swift": "Swift", ".rb": "Ruby", ".php": "PHP", ".c": "C", ".h": "C/C++",
    ".cpp": "C++", ".cc": "C++", ".cs": "C#", ".scala": "Scala", ".ex": "Elixir",
    ".sql": "SQL", ".sh": "Shell",
}


def git(args, root):
    try:
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True).stdout
    except Exception:
        return ""


def include_refs(text):
    """Yield (ref, include_like) for @path / @{path} references in text."""
    for m in INCLUDE_RE.finditer(text):
        ref = (m.group(1) if m.group(1) is not None else m.group(2)).strip()
        if not ref:
            continue
        like = ("/" in ref) or (os.path.splitext(ref)[1].lower() in INC_EXTS)
        yield ref, like


def include_resolves(ref, root, pabs):
    for d in (root, pabs):
        cand = os.path.normpath(os.path.join(d, ref))
        if os.path.isfile(cand):
            try:
                if os.path.commonpath([os.path.realpath(cand), os.path.realpath(root)]) == os.path.realpath(root):
                    return True
            except ValueError:
                pass
    return False


def main():
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    pdir = os.environ.get("PRECHECK_DIR", ".claude/precheck")
    pabs = os.path.join(root, pdir)
    issues = []

    print("=== precheck config doctor ===")
    print(f"PROJECT_ROOT={root}")
    print(f"PRECHECK_DIR={pdir}")
    print(f"DIR_EXISTS={'yes' if os.path.isdir(pabs) else 'no'}")

    # --- files present ---
    print("\n--- files ---")
    def has(name):
        return os.path.isfile(os.path.join(pabs, name))
    print(f"config.json: {'present' if has('config.json') else 'absent'}")
    print(f"context.md:  {'present' if has('context.md') else 'absent'}")
    print(f"synthesis.md:{'present' if has('synthesis.md') else 'absent'}")
    print(f"exclude:     {'present' if has('exclude') else 'absent'}")
    overrides = [d for d in KNOWN_DIMS if has(f"{d}.md")]
    print(f"overrides:   {', '.join(overrides) if overrides else 'none'}")
    stray = []
    if os.path.isdir(pabs):
        for fn in sorted(os.listdir(pabs)):
            base, ext = os.path.splitext(fn)
            if ext == ".md" and base not in KNOWN_DIMS and base not in ("context", "synthesis"):
                stray.append(fn)
    if stray:
        print(f"unrecognized .md files: {', '.join(stray)}")
        issues.append(f"unrecognized override file(s): {', '.join(stray)} — not a known dimension ({', '.join(KNOWN_DIMS)})")

    # --- config.json validation ---
    cfg = None
    if has("config.json"):
        print("\n--- config.json ---")
        raw = open(os.path.join(pabs, "config.json"), encoding="utf-8").read()
        try:
            cfg = json.loads(raw)
            print("valid JSON: yes")
        except ValueError as e:
            print(f"valid JSON: NO — {e}")
            issues.append(f"config.json is not valid JSON: {e}")
        if isinstance(cfg, dict):
            dims = cfg.get("dimensions")
            if dims is None:
                print(f"dimensions: (unset -> default all six, order {DEFAULT_ORDER})")
            elif isinstance(dims, list):
                unknown = [d for d in dims if d not in KNOWN_DIMS]
                print(f"dimensions: {dims}")
                if unknown:
                    issues.append(f"unknown dimension(s): {unknown}. Known: {KNOWN_DIMS}")
            else:
                issues.append('"dimensions" must be a list of strings')
            print(f"model: {cfg.get('model', '(unset -> sonnet)')}")
            custom = cfg.get("custom", [])
            if custom and isinstance(custom, list):
                names = []
                for i, c in enumerate(custom):
                    if not isinstance(c, dict) or not c.get("name") or not c.get("instructions"):
                        issues.append(f'custom[{i}] must be an object with non-empty "name" and "instructions"')
                    else:
                        names.append(c["name"])
                print(f"custom reviewers: {names if names else '(present but malformed)'}")
            elif custom:
                issues.append('"custom" must be a list of {name, instructions}')
            else:
                print("custom reviewers: none")
            unknown_keys = [k for k in cfg if k not in KNOWN_KEYS and not k.startswith("$")]
            if unknown_keys:
                issues.append(f"unknown config key(s): {unknown_keys}. Known: {sorted(KNOWN_KEYS)}")

    # --- @-includes in the .md files ---
    md_files = [("context.md", os.path.join(pabs, "context.md")),
                ("synthesis.md", os.path.join(pabs, "synthesis.md"))]
    md_files += [(f"{d}.md", os.path.join(pabs, f"{d}.md")) for d in overrides]
    inc_lines = []
    for label, p in md_files:
        if not os.path.isfile(p):
            continue
        for ref, like in include_refs(open(p, encoding="utf-8").read()):
            if include_resolves(ref, root, pabs):
                inc_lines.append(f"{label}: @{ref} -> ok")
            elif like:
                inc_lines.append(f"{label}: @{ref} -> UNRESOLVED")
                issues.append(f"broken @include in {label}: '{ref}' — no such file in the repo")
    if inc_lines:
        print("\n--- @-includes ---")
        for ln in inc_lines:
            print(ln)

    # --- repo signals ---
    print("\n--- repo signals ---")
    files = [f for f in git(["ls-files"], root).splitlines() if f]
    exts = Counter()
    for f in files:
        _, e = os.path.splitext(f)
        if e in EXT_LANG:
            exts[EXT_LANG[e]] += 1
    langs = ", ".join(f"{l}({n})" for l, n in exts.most_common(6)) or "unknown (no tracked source detected)"
    print(f"languages: {langs}")
    docs = []
    if any(f.lower().startswith("readme") for f in (os.path.basename(x) for x in files)):
        docs.append("README")
    if os.path.isdir(os.path.join(root, "docs")):
        docs.append("docs/")
    print(f"docs: {', '.join(docs) if docs else 'none detected'}")
    plan_spots = []
    for cand in ["docs/plans", ".plans", "PLAN.md", "TODO.md"]:
        if os.path.exists(os.path.join(root, cand)):
            plan_spots.append(cand)
    print(f"plans: {', '.join(plan_spots) if plan_spots else 'none detected'}")

    # --- summary ---
    print("\n--- ISSUES ---")
    if issues:
        for i in issues:
            print(f"- {i}")
        print(f"\nSTATUS=issues ({len(issues)})")
    else:
        print("none")
        print(f"\nSTATUS={'ok' if os.path.isdir(pabs) else 'absent'}")


if __name__ == "__main__":
    main()
