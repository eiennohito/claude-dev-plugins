"""Deny shell commands whose destructive target the command text doesn't pin down.

Targets honest mistakes, not malice: the agent runs A, but what executes is B.
Whether destroying the *intended* target is OK is context (a checkpoint was
taken, the user asked) and is left to the permission rules and the auto-mode
classifier. This only checks that the target of rm, rm-list.py, find -delete,
and git commands that can lose work is what the text says. A ≠ B arises
through four channels:

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
   `rm -rf /x`; `${DIR:-/}x` likewise), or `$(…)`. A target that is a whole
   variable (`"$tmpdir"`, a loop's `"$f"`) is allowed: empty, it gives rm no
   operand. So is an unresolved filename under a resolved dir for
   non-recursive rm, which can't delete directories. Literal assignments in
   the same command (`S=/abs; rm -rf $S/x`), `~`, `$HOME` and `$USER`
   resolve. Globs and braces are fine except a bare `*` directly under `/`, a
   top-level dir, or home.
3. Catastrophic targets — `/`, a direct child of `/`, or home (typo guard for
   `rm -rf / tmp/x`).
4. Runtime-computed sets — `find -delete`/`-exec rm`, `xargs rm`, and a force
   push without an explicit refspec (pushes whatever is checked out). For the
   former, the message points at the bundled rm-list.py.

Commands run on another host through `ssh` aren't checked: their cwd and
variables are the remote's. Quoted text, comments and heredoc bodies fed to
other programs are data (shellparse tells them apart from code), while
`$(…)`, `eval`, `sh -c '…'` and function bodies are checked.
"""
import os
import re

QUICK = re.compile(r"\b(rm|git|find|xargs|rm-list\.py)\b")
GLOB_CHARS = set("*?[]{}")
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
DECLARE = {"export", "local", "declare", "readonly", "typeset"}
UNRESOLVED = "contains a `$(…)` or a variable this command doesn't assign"
CD_WHY = ("this command `cd`s first. In the Bash tool, `cd` persists into later "
          "calls and Claude Code strips a `cd` to the current directory, so this "
          "hook doesn't rely on it")
RM_LIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "rm-list.py")

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
UNPARSEABLE = (
    "Could not parse this command to check it for destructive operations. "
    "Run destructive parts (rm, git reset/clean/push --force, find -delete) "
    "as simple calls with literal absolute targets."
)


class State:
    def __init__(self, cwd, why, actual=None, root=None):
        self.cwd = cwd          # pinned working directory, or None
        self.why = why          # why cwd is None, for the message
        self.actual = actual    # real cwd when it's unpinned only for not being the root
        self.root = root
        self.vars = {}          # assignments seen so far (None = unknown value)
        self.problems = []
        self.hint = False       # add the rm-list.py recipe

    def env(self):
        env = {"HOME": os.path.expanduser("~"), "USER": os.environ.get("USER"), "PWD": self.cwd}
        env.update(self.vars)
        return env

    def resolve(self, w):
        """(absolute normalized path, None) for a target Word, (None, reason)
        if it can't be pinned, or (None, None) for a sole variable."""
        p = w.expand(self.env())
        if p is None and w.sole_param():
            return None, None  # empty → no operand; it can't widen
        if p is None:
            return None, UNRESOLVED
        if not p.startswith("/"):
            if self.cwd is None:
                return None, f"relative path, and {self.why}"
            p = os.path.join(self.cwd, p)
        return os.path.normpath(p), None

    def check_paths(self, label, targets, files_only=False):
        for t in targets:
            p, why = self.resolve(t)
            if why and files_only:
                split = t.split_last()
                if split and self.resolve(split[0])[1] is None:
                    continue  # non-recursive rm: the unresolved part only picks a file in a pinned dir
            if why:
                self.problems.append(f"`{label}` target `{t.src}`: {why}")
            elif p and catastrophic(p):
                self.problems.append(f"`{label}` target `{t.src}` resolves to `{p}`: "
                                     "filesystem root, a top-level directory, or home")


def catastrophic(p):
    home = os.path.normpath(os.path.expanduser("~"))
    d, base = os.path.split(p)
    if GLOB_CHARS & set(base) and not GLOB_CHARS & set(d):
        return base.strip(".*") == "" and (d.count("/") < 2 or d == home)
    return not GLOB_CHARS & set(p) and (p.count("/") < 2 or p == home)


def short_flags(opts):
    return {c for o in opts if o.startswith("-") and not o.startswith("--") for c in o[1:]}


def check_rm(st, cmd):
    if "xargs" in cmd.inv.wrappers:
        st.problems.append("`xargs rm` deletes paths computed at runtime instead "
                           "of a reviewed list")
        st.hint = True
        return
    opts, paths, end = [], [], False
    for a in cmd.inv.args:
        v = a.value
        if not end and v == "--":
            end = True
        elif not end and v and v.startswith("-") and v != "-":
            opts.append(v)
        else:
            paths.append(a)
    recursive = "--recursive" in opts or short_flags(opts) & {"r", "R"}
    st.check_paths("rm", paths, files_only=not recursive)


def check_git(st, args):
    """Every git command that can lose work needs its repo pinned: the cwd is
    the project root, or -C/--git-dir/--work-tree names it."""
    i, repo_pinned = 0, False
    vals = [a.value for a in args]
    while i < len(args) and (vals[i] or "").startswith("-"):
        v = vals[i]
        if v in ("-C", "--git-dir", "--work-tree") and i + 1 < len(args):
            repo_pinned |= st.resolve(args[i + 1])[1] is None
        elif v.startswith(("--git-dir=", "--work-tree=")):
            from shellparse import lit_word
            repo_pinned |= st.resolve(lit_word(v.split("=", 1)[1]))[1] is None
        i += 2 if v in GIT_OPTS_WITH_VALUE else 1
    sub = (vals[i] or "") if i < len(args) else ""
    rest = [v or "" for v in vals[i + 1:]]
    if sub == "push" and (
        "f" in short_flags(rest) or any(r == "--force" or r.startswith("--force-with-lease")
                                        or (len(r) > 1 and r[0] == "+") for r in rest)
    ) and len([r for r in rest if not r.startswith("-")]) < 2:
        st.problems.append("`git push --force` without an explicit remote and "
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
    st.problems.append(f"`git {sub}` acts on the repo of the current directory, and {st.why}. {fix}")


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


def check_find(st, args):
    vals = [a.value for a in args]
    for i, a in enumerate(vals):
        if a == "-delete" or (a in ("-exec", "-execdir", "-ok", "-okdir")
                              and i + 1 < len(vals)
                              and os.path.basename(vals[i + 1] or "") == "rm"):
            st.problems.append(f"`find … {a}` deletes whatever the pattern matches "
                               "instead of an explicit list")
            st.hint = True
            return


def analyze(cmds, st):
    """Walk the executed commands in order, tracking cwd and assignments."""
    from shellparse import Simple, split_assign
    for cmd in cmds:
        if cmd.ctx.remote:
            continue
        if type(cmd.node) is not Simple:  # for/select header assigns its variable
            st.vars[cmd.node.var] = None
            continue
        inv = cmd.inv
        if not cmd.node.words:  # bare assignments persist in the shell
            env = st.env()
            for name, w in cmd.assigns:
                st.vars[name] = w.expand(env)
            continue
        name = inv.name
        if name in DECLARE:
            env = st.env()
            for a in inv.args:
                kv = split_assign(a) if a.parts else None
                if kv:
                    st.vars[kv[0]] = kv[1].expand(env)
        elif name in ("cd", "pushd", "popd"):
            target = next((a for a in inv.args if not (a.value or "x").startswith("-")), None)
            if name == "popd" or st.cwd is None or target is None \
                    or st.resolve(target)[0] != st.cwd:
                st.cwd, st.why, st.actual = None, CD_WHY, None  # cd to where we are is a no-op
        elif name == "rm-list.py" or (name and name.startswith("python") and inv.args
                                      and os.path.basename(inv.args[0].value or "") == "rm-list.py"):
            st.check_paths("rm-list.py", inv.args if name == "rm-list.py" else inv.args[1:])
        elif name == "rm":
            check_rm(st, cmd)
        elif name == "git":
            check_git(st, inv.args)
        elif name == "find":
            check_find(st, inv.args)


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


def initial_state(cwd, root):
    if cwd and root and (os.path.normpath(cwd) == os.path.normpath(root) or worktree_of(cwd, root)):
        return State(os.path.normpath(cwd), "")
    return State(None, f"the current directory is `{cwd or 'unknown'}`, not the "
                       f"project root `{root or 'unknown'}`, so it may not be "
                       "what you expect", cwd or None, root or "/project/root")


def check(view, cwd, root):
    """Deny reason for a Bash command (shellparse view), or None."""
    if view.tool != "Bash" or not QUICK.search(view.text):
        return None
    cmds = view.commands()
    if cmds is None:
        return UNPARSEABLE
    st = initial_state(cwd, root)
    analyze(cmds, st)
    if not st.problems:
        return None
    return (MSG.format(problems="\n".join(f"- {p}" for p in st.problems))
            + (LIST_MSG if st.hint else ""))
