"""User and project command rules: deny shell commands by meaning, not text.

Permission rules match globs against the text of each `;`/`&&` part of a
command: `*` spans quoted text (`Bash(git * push *)` denies
`git log --grep push`), and prefix patterns miss global options and wrappers
(`Bash(git push*)` misses `git --no-pager push` and `env X=1 git push`).
These rules match the parsed command (lib/shellparse.py) instead.

Rules files, both optional, concatenated (user first):
  ~/.claude/probe-tools/rules.json          ($CLAUDE_CONFIG_DIR if set)
  <project>/.claude/probe-tools/rules.json

  {"deny": [{"command": "git push", "reason": "Pushing is the user's call."}]}

`command` is a program name followed by words the command must match:
- bare words: its leading positional arguments, in order (for git, after
  global options such as -C/-c/--no-pager, so the subcommand comes first);
- `-x` / `--long` words: options that must appear anywhere in its arguments
  (`-f` also matches a bundle like `-fd`; `--hard` also matches `--hard=…`);
- `a|b` in any word: alternatives (`git push -f|--force|--force-with-lease`).
Wrappers (sudo, env, timeout, xargs, …), `eval`, `sh -c '…'`, `bash <<EOF`
and `ssh host '…'` are seen through; quoted text, comments and heredocs fed to
other programs never match. Arguments built from expansions (`git $SUB`)
don't match a literal word. Deny only, no "ask": a prompt stalls long,
unattended sessions.
"""
import json
import os

GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}


def paths(root):
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    out = [os.path.join(config, "probe-tools", "rules.json")]
    if root:
        out.append(os.path.join(root, ".claude", "probe-tools", "rules.json"))
    return out


class Rule:
    __slots__ = ("command", "name", "words", "flags", "reason", "source")

    def __init__(self, command, reason, source):
        parts = command.split()
        if not parts:
            raise ValueError("empty command")
        self.command, self.reason, self.source = command, reason, source
        self.name = set(parts[0].split("|"))
        self.words = [set(p.split("|")) for p in parts[1:] if not p.startswith("-")]
        self.flags = [set(p.split("|")) for p in parts[1:] if p.startswith("-")]

    def matches(self, cmd):
        inv = cmd.inv
        if not inv or inv.name not in self.name:
            return False
        vals = [a.value for a in inv.args]
        pos = positionals(inv.name, vals)
        if len(pos) < len(self.words) or any(p not in w for p, w in zip(pos, self.words)):
            return False
        return all(any(has_flag(vals, f) for f in alts) for alts in self.flags)


def positionals(name, vals):
    """Leading non-option arguments (None for an expansion), skipping git's
    global options and their values."""
    out, i = [], 0
    if name == "git":
        while i < len(vals) and (vals[i] or "").startswith("-"):
            i += 2 if vals[i] in GIT_OPTS_WITH_VALUE else 1
    for v in vals[i:]:
        if v is not None and v.startswith("-"):
            continue
        out.append(v)
    return out


def has_flag(vals, flag):
    for v in vals:
        if v is None or not v.startswith("-"):
            continue
        if v == flag or (flag.startswith("--") and v.startswith(flag + "=")):
            return True
        if len(flag) == 2 and not v.startswith("--") and flag[1] in v[1:]:
            return True
    return False


def load(root):
    """(rules, errors) from the rules files that exist."""
    rules, errors = [], []
    for path in paths(root):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("deny", []) if isinstance(data, dict) else None
            if not isinstance(entries, list):
                raise ValueError('expected {"deny": [...]}')
            found = []
            for e in entries:
                if not isinstance(e, dict) or not isinstance(e.get("command"), str):
                    raise ValueError(f"bad rule {e!r}: needs a \"command\" string")
                found.append(Rule(e["command"], str(e.get("reason") or ""), path))
            rules += found  # a file applies whole or not at all
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
    return rules, errors


def check(view, root):
    """Deny reason for a Bash/Monitor command (shellparse view), or None."""
    if view.tool not in ("Bash", "Monitor"):
        return None
    rules, _ = load(root)
    rules = [r for r in rules if any(n in view.text for n in r.name)]
    if not rules:
        return None
    cmds = view.commands()
    if cmds is None:
        names = ", ".join(f"`{r.command}`" for r in rules)
        return (f"this command couldn't be parsed to check it against your rules ({names}). "
                "Split it into simple commands.")
    from shellparse import Simple
    for cmd in cmds:
        if type(cmd.node) is not Simple:
            continue
        for r in rules:
            if r.matches(cmd):
                where = f" on {cmd.ctx.remote}" if cmd.ctx.remote else ""
                return (f"`{r.command}`{where} is denied by a rule in {r.source}"
                        + (f": {r.reason}" if r.reason else "."))
    return None
