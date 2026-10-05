#!/usr/bin/env python3
"""PreToolUse hook — deny shell commands whose target the command text
doesn't pin down.

Registered (hooks/hooks.json) for PreToolUse matcher "Bash". Targets honest
mistakes, not malice: the agent runs A, but what executes is B. Whether
destroying the *intended* target is OK is context (a checkpoint was taken, the
user asked) and is left to the permission rules and the auto-mode classifier.
This hook only checks that the target of rm, rm-list.py, find -delete, and
git commands that can lose work is what the text says. A ≠ B arises through
four channels, each checked here:

1. cwd drift — a relative rm target, or a git command without -C, resolves
   against a cwd the agent may misremember. Allowed only when the hook's real
   cwd is the project root (or a linked worktree of it, where worktree-isolated
   subagents run) and the command doesn't `cd` elsewhere first. An in-command
   `cd` isn't trusted: in the Bash tool it persists into later calls, Claude
   Code strips a `cd` to the current directory, and `cd` itself may be
   overridden by the user's shell profile. Only git that can lose work is
   checked (reset --hard, checkout/restore of paths, clean, rebase, stash
   drop, branch -D, force push, …); read-only and additive git (status, log,
   add, commit, stash push, …) loses nothing in the wrong repo.
2. Expansion — a variable that can't be resolved from the command itself as
   part of a longer path (an empty `$DIR` turns `rm -rf $DIR/x` into
   `rm -rf /x`), or `$(…)`. A target that is a whole variable (`"$tmpdir"`,
   a loop's `"$f"`) is allowed: empty, it gives rm no operand. So is an
   unresolved filename under a resolved dir for non-recursive rm, which can't
   delete directories. Literal
   assignments in the same command (`S=/abs; rm -rf $S/x`), `~`, `$HOME` and
   `$USER` resolve. Globs and braces are fine except a bare `*` directly under
   `/`, a top-level dir, or home.
3. Catastrophic targets — `/`, a direct child of `/`, or home (typo guard for
   `rm -rf / tmp/x`).
4. Runtime-computed sets — `find -delete`/`-exec rm`, `xargs rm`, and a force
   push without an explicit refspec (pushes whatever is checked out). For the
   former, the message points at the bundled rm-list.py.

Commands after the destructive one are never inspected: they can't change its
target. Parsing is shlex-based with quote-aware substitution masking and
heredoc stripping, so quoted text such as a commit message doesn't trigger.
Never fails the tool call on its own errors: bad hook input → allow.
"""
import json
import os
import re
import shlex
import sys

QUICK = re.compile(r"\b(rm|git|find|xargs|rm-list\.py)\b")
PUNCT = ";&|()<>\n"
GLOB_CHARS = set("*?[]{}")
SUB = "\x01"  # stands in for a masked $(…)/`…`: never resolvable
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
WRAPPERS = {"sudo", "doas", "command", "builtin", "exec", "nohup", "time",
            "nice", "env", "caffeinate", "timeout", "xargs"}
KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until",
            "for", "case", "esac", "!", "{", "}", "[[", "]]"}
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
ASSIGN = re.compile(r"^([A-Za-z_]\w*)=(.*)$", re.S)
VAR = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*)|([0-9@*#?$!-]))")
SOLE_VAR = re.compile(r"^\$(\{[A-Za-z_]\w*\}|[A-Za-z_]\w*)$")
HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(['\"]?)([A-Za-z_]\w*)\2")
CD_WHY = ("this command `cd`s first. In the Bash tool, `cd` persists into later "
          "calls and Claude Code strips a `cd` to the current directory, so this "
          "hook doesn't rely on it")
RM_LIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rm-list.py")

MSG = (
    "This command's target isn't pinned down by the command text, so what "
    "runs may not be what you intend:\n{problems}\n\n"
    "Fix: write the target as a literal absolute path, and for git use "
    "`git -C /abs/repo …`. Don't prefix `cd` to the current directory: Claude "
    "Code strips it. Replace unresolvable variables with their literal value."
)
LIST_MSG = (
    "\n\nFor pattern-driven deletion, as three separate Bash calls:\n"
    "1. Write the candidates to a list file, starting `find` from an absolute "
    "dir: `find /abs/dir -name '*.pyc' > /abs/tmp/delete-list.txt`\n"
    "2. Review it: `cat /abs/tmp/delete-list.txt` (edit out anything unintended).\n"
    f"3. Delete exactly that list: `python3 {RM_LIST} /abs/tmp/delete-list.txt` "
    "(one absolute path per line; directories are removed recursively; it "
    "validates every line and deletes nothing if any is unsafe)."
)


def strip_heredocs(s):
    """Drop heredoc bodies: they're data, not commands."""
    pos = 0
    while True:
        m = HEREDOC.search(s, pos)
        if not m:
            return s
        nl = s.find("\n", m.end())
        if nl < 0:
            return s
        tabs = "\t*" if m.group(1) else ""
        term = re.compile(rf"^{tabs}{re.escape(m.group(3))}[ \t]*$", re.M)
        t = term.search(s, nl + 1)
        s = s[:nl] + (s[t.end():] if t else "")
        pos = m.end()


def close_paren(s, i):
    """Index just past the `)` matching the `(` before s[i], skipping quotes."""
    depth, quote = 1, None
    while i < len(s):
        c = s[i]
        if c == "\\" and quote != "'":
            i += 2
            continue
        if quote:
            quote = None if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if not depth:
                return i + 1
        i += 1
    raise ValueError("unbalanced parentheses")


def mask(s):
    """Replace $(…), <(…), >(…), `…` and $((…)) with SUB (outside single
    quotes); return (masked, [inner command strings])."""
    out, inner, i, quote = [], [], 0, None
    while i < len(s):
        c = s[i]
        if c == "\\" and quote != "'":
            out.append(s[i:i + 2])
            i += 2
            continue
        if quote == "'":
            quote = None if c == "'" else quote
        elif c == "`":
            j = i + 1
            while j < len(s) and s[j] != "`":
                j += 2 if s[j] == "\\" else 1
            if j >= len(s):
                raise ValueError("unterminated backtick")
            inner.append(s[i + 1:j].replace("\\`", "`"))
            out.append(SUB)
            i = j + 1
            continue
        elif s.startswith("$((", i):
            out.append(SUB)
            i = close_paren(s, i + 2)
            i = i + 1 if s[i:i + 1] == ")" else i
            continue
        elif s.startswith("$(", i) or (not quote and s[i:i + 2] in ("<(", ">(")):
            j = close_paren(s, i + 2)
            inner.append(s[i + 2:j - 1])
            out.append(SUB)
            i = j
            continue
        elif c == '"':
            quote = None if quote == '"' else '"'
        elif c == "'" and not quote:
            quote = "'"
        out.append(c)
        i += 1
    return "".join(out), inner


def tokenize(s):
    lex = shlex.shlex(s.replace("\\\n", " "), posix=True, punctuation_chars=PUNCT)
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = "#"
    return list(lex)


def sequence(tokens):
    """Split into simple commands: [(operators before it, words)]."""
    seq, ops, words, skip = [], [], [], False
    for t in tokens:
        if t and all(c in PUNCT for c in t):
            if "<" in t or ">" in t:  # redirection: drop fd number and target
                if words and words[-1].isdigit():
                    words.pop()
                skip = True
                continue
            if words:
                seq.append((ops, words))
                ops, words = [], []
            ops.append(t)
        elif skip:
            skip = False
        else:
            words.append(t)
    if words:
        seq.append((ops, words))
    return seq


class State:
    def __init__(self, cwd, why, actual=None, root=None):
        self.cwd = cwd          # pinned working directory, or None
        self.why = why          # why cwd is None, for the message
        self.actual = actual    # real cwd when it's unpinned only for not being the root
        self.root = root
        self.vars = {}          # literal assignments seen so far (None = unknown)

    def copy(self):
        c = State(self.cwd, self.why, self.actual, self.root)
        c.vars = dict(self.vars)
        return c

    def expand(self, w):
        """Resolve ~ and $VARs; None if anything can't be resolved."""
        if SUB in w:
            return None
        if w == "~" or w.startswith("~/"):
            w = os.path.expanduser("~") + w[1:]
        unresolved = False

        def repl(m):
            nonlocal unresolved
            name = m.group(1) or m.group(2)
            if name in self.vars:
                v = self.vars[name]
            elif name in ("HOME", "USER"):
                v = os.environ.get(name)
            elif name == "PWD":
                v = self.cwd
            else:
                v = None
            if v is None:
                unresolved = True
                return ""
            return v
        w = VAR.sub(repl, w)
        return None if unresolved else w

    def resolve(self, w):
        """Absolute normalized path for a target word, or (None, reason)."""
        p = self.expand(w)
        if p is None and SOLE_VAR.match(w):
            return None, None  # empty → rm gets no operand; it can't widen
        if p is None:
            return None, "contains a `$(…)` or a variable this command doesn't assign"
        if not p.startswith("/"):
            if self.cwd is None:
                return None, f"relative path, and {self.why}"
            p = os.path.join(self.cwd, p)
        return os.path.normpath(p), None


def catastrophic(p):
    home = os.path.normpath(os.path.expanduser("~"))
    d, base = os.path.split(p)
    if GLOB_CHARS & set(base) and not GLOB_CHARS & set(d):
        if base.strip(".*") == "" and (d.count("/") < 2 or d == home):
            return True
        return False
    return not GLOB_CHARS & set(p) and (p.count("/") < 2 or p == home)


def short_flags(opts):
    return {c for o in opts if o.startswith("-") and not o.startswith("--")
            for c in o[1:]}


def unwrap(words):
    """Strip keywords, env assignments and wrappers (sudo, env, xargs, …)."""
    via_xargs = False
    while words:
        base = os.path.basename(words[0])
        if words[0] in KEYWORDS or ASSIGN.match(words[0]):
            words = words[1:]
        elif base in WRAPPERS:
            via_xargs |= base == "xargs"
            words = words[1:]
            while words and words[0].startswith("-"):
                words = words[1:]
            if base == "timeout" and words:
                words = words[1:]
        else:
            break
    return words, via_xargs


def check_paths(st, label, targets, problems, files_only=False):
    for t in targets:
        p, why = st.resolve(t)
        if why and files_only and "/" in t and not st.resolve(t.rsplit("/", 1)[0] or "/")[1]:
            continue  # non-recursive rm: the unresolved part only picks a file in a pinned dir
        if why:
            problems.append(f"`{label}` target `{t}`: {why}")
        elif p and catastrophic(p):
            problems.append(f"`{label}` target `{t}` resolves to `{p}`: "
                            "filesystem root, a top-level directory, or home")


def check_rm(st, args, via_xargs, problems):
    if via_xargs:
        problems.append("`xargs rm` deletes paths computed at runtime instead "
                        "of a reviewed list")
        return True
    opts, paths, end = [], [], False
    for a in args:
        if not end and a == "--":
            end = True
        elif not end and a.startswith("-") and a != "-":
            opts.append(a)
        else:
            paths.append(a)
    recursive = "--recursive" in opts or short_flags(opts) & {"r", "R"}
    check_paths(st, "rm", paths, problems, files_only=not recursive)
    return False


def check_git(st, args, problems):
    """Every git command needs its repo pinned: the cwd is the project root, or
    -C/--git-dir/--work-tree names it."""
    i, repo_pinned = 0, False
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("-C", "--git-dir", "--work-tree") and i + 1 < len(args):
            p, why = st.resolve(args[i + 1])
            repo_pinned |= why is None
        i += 2 if args[i] in GIT_OPTS_WITH_VALUE else 1
    sub, rest = (args[i], args[i + 1:]) if i < len(args) else ("", [])
    if sub == "push" and (
        "f" in short_flags(rest) or any(r == "--force" or r.startswith("--force-with-lease")
                                        or (len(r) > 1 and r[0] == "+") for r in rest)
    ) and len([r for r in rest if not r.startswith("-")]) < 2:
        problems.append("`git push --force` without an explicit remote and "
                        "refspec pushes whatever branch is checked out")
    if repo_pinned or st.cwd is not None or not git_destructive(sub, rest):
        return
    if st.actual:
        fix = (f"If you mean the repo at `{st.actual}`, rerun with "
               f"`git -C {st.actual} …`; if not, run `cd {st.root}` as its own call first.")
    else:
        fix = "Rerun with `git -C /abs/repo …`."
    if sub == "checkout":
        fix += " To change branches, `git switch <branch>` isn't checked (it never discards changes)."
    problems.append(f"`git {sub}` acts on the repo of the current directory, and {st.why}. {fix}")


def git_destructive(sub, rest):
    """Git operations that can lose work (uncommitted changes, commits, refs,
    stashes). Everything else is read-only or additive: in the wrong repo it
    misinforms or adds, but loses nothing, and a destructive follow-up is
    checked when it happens."""
    flags, args = short_flags(rest), set(rest)
    force = "f" in flags or "--force" in args
    if "--abort" in args and sub in ("merge", "rebase", "cherry-pick", "revert", "am"):
        return True
    return (
        (sub == "reset" and bool(args & {"--hard", "--merge", "--keep"}))
        or (sub == "checkout" and not (flags & {"b", "B"} or args & {"--orphan"}))  # may be paths
        or (sub == "restore" and not (args <= {"--staged", "-S"} | {r for r in rest if not r.startswith("-")}
                                      and args & {"--staged", "-S"} and not args & {"--worktree", "-W"}))
        or sub in ("clean", "rebase", "filter-branch", "filter-repo", "update-ref", "prune", "gc")
        or (sub == "rm" and force)
        or (sub == "switch" and (force or args & {"--discard-changes", "-C", "--force-create"}))
        or (sub == "stash" and bool(rest) and rest[0] in ("drop", "clear"))
        or (sub == "branch" and (bool(flags & {"D", "M", "C"}) or force))
        or (sub == "tag" and (bool(flags & {"d"}) or force or "--delete" in args))
        or (sub == "worktree" and bool(rest) and rest[0] in ("remove", "prune"))
        or (sub == "reflog" and bool(rest) and rest[0] in ("expire", "delete"))
        or (sub == "submodule" and bool(rest) and (rest[0] == "deinit" or (rest[0] == "update" and force)))
        or (sub == "push" and (force or bool(flags & {"d"}) or bool(args & {"--delete", "--mirror", "--prune"})
                               or any(r.startswith("--force-with-lease") or (len(r) > 1 and r[0] in "+:")
                                      for r in rest)))
    )


def check_find(args, problems):
    for i, a in enumerate(args):
        if a == "-delete" or (a in ("-exec", "-execdir", "-ok", "-okdir")
                              and i + 1 < len(args)
                              and os.path.basename(args[i + 1]) == "rm"):
            problems.append(f"`find … {a}` deletes whatever the pattern matches "
                            "instead of an explicit list")
            return True
    return False


def analyze(s, st, problems, depth=0):
    """Walk s in order, tracking cwd and assignments; append problems.
    Returns True if a list-file hint should be added."""
    if depth > 5:
        raise ValueError("nesting too deep")
    masked, inner = mask(strip_heredocs(s))
    hint = False
    for sub in inner:
        hint |= analyze(sub, st.copy(), problems, depth + 1)
    for ops, seg in sequence(tokenize(masked)):
        if any(o in ("(", ")") for o in ops) or seg[0] in ("for", "while", "until", "if", "case"):
            if seg[0] == "for" and len(seg) > 1:
                st.vars[seg[1]] = None
            if st.cwd is not None and any(o in ("(", ")") for o in ops):
                st.cwd, st.why = None, "a subshell makes the cwd hard to track"
        assigns = seg if all(ASSIGN.match(w) for w in seg) else \
            seg[1:] if seg[0] in ("export", "local", "declare", "readonly", "typeset") else []
        for w in assigns:
            m = ASSIGN.match(w)
            if m:
                st.vars[m.group(1)] = st.expand(m.group(2))
        if assigns or not seg:
            continue
        words, via_xargs = unwrap(seg)
        if not words:
            continue
        name = os.path.basename(words[0])
        args = words[1:]
        if name in ("cd", "pushd", "popd"):
            target = next((a for a in args if not a.startswith("-")), "~")
            if name == "popd" or st.cwd is None or st.resolve(target)[0] != st.cwd:
                st.cwd, st.why, st.actual = None, CD_WHY, None  # cd to where we are is a no-op
        elif name == "eval" or (name in SHELLS and "-c" in args):
            script = " ".join(args) if name == "eval" else \
                (args[args.index("-c") + 1:] or [""])[0]
            sub_st = st.copy()
            sub_st.vars = sub_st.vars if name == "eval" else {}
            hint |= analyze(script, sub_st, problems, depth + 1)
        elif name == "rm-list.py" or (name.startswith("python") and args
                                      and os.path.basename(args[0]) == "rm-list.py"):
            check_paths(st, "rm-list.py", args[1:] if name != "rm-list.py" else args, problems)
        elif name == "rm":
            hint |= check_rm(st, args, via_xargs, problems)
        elif name == "git":
            check_git(st, args, problems)
        elif name == "find":
            hint |= check_find(args, problems)
    return hint


def git_common_dir(top):
    """`<repo>/.git` for a main checkout or linked worktree top-level, else None."""
    g = os.path.join(top, ".git")
    if os.path.isdir(g):
        return os.path.normpath(g)
    try:
        with open(g, encoding="utf-8") as f:
            line = f.readline().strip()
    except OSError:
        return None
    if not line.startswith("gitdir: "):
        return None
    d = os.path.normpath(os.path.join(top, line[8:]))
    return os.path.dirname(os.path.dirname(d)) if os.path.basename(os.path.dirname(d)) == "worktrees" else None


def worktree_of(cwd, root):
    """True if cwd is the top-level of a linked worktree of root's repository
    (worktree-isolated subagents run there with CLAUDE_PROJECT_DIR = root)."""
    if not os.path.isfile(os.path.join(cwd, ".git")):
        return False
    common = git_common_dir(root)
    return common is not None and common == git_common_dir(cwd)


def deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "Denied by probe-tools: " + reason,
        }
    }, ensure_ascii=False))


def main():
    try:
        data = json.load(sys.stdin)
        cmd = data["tool_input"]["command"]
    except Exception:
        return
    if not isinstance(cmd, str) or not QUICK.search(cmd):
        return
    cwd = data.get("cwd") or ""
    root = os.environ.get("CLAUDE_PROJECT_DIR") or ""
    if cwd and root and (os.path.normpath(cwd) == os.path.normpath(root)
                         or worktree_of(cwd, root)):
        st = State(os.path.normpath(cwd), "")
    else:
        st = State(None, f"the current directory is `{cwd or 'unknown'}`, not the "
                         f"project root `{root or 'unknown'}`, so it may not be "
                         "what you expect", cwd or None, root or "/project/root")
    problems = []
    try:
        hint = analyze(cmd, st, problems)
    except ValueError:
        deny("Could not parse this command to check it for destructive operations. "
             "Run destructive parts (rm, git reset/clean/push --force, find -delete) "
             "as simple calls with literal absolute targets.")
        return
    if problems:
        deny(MSG.format(problems="\n".join(f"- {p}" for p in problems))
             + (LIST_MSG if hint else ""))


if __name__ == "__main__":
    main()
