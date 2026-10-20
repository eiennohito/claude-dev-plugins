"""Deny process waits that can hang or end early, and processes escaping the harness.

Waiting on a process by *name* is wrong in both directions:

- False positive → hang. Claude Code runs every command as
  `<shell> -c "… eval '<command>'"`, so `pgrep -f X` always matches the
  wrapping shell, whose argv contains X: `while pgrep -f X; do sleep 5; done`
  never ends until the Monitor timeout. The same holds on a remote host,
  where sshd runs the command through `<shell> -c`.
- False negative → ends early. Without -f, pgrep/pidof match the 15-char comm
  name, so a long script name or a process under a wrapper (uv, npm, python3)
  never matches.

Waiting for a process to exit is the background Bash launch's job: the harness
captures its output and notifies on exit, however it exits. Monitor is for
events that aren't the process exiting (output lines, external services), so
following a log is fine.

Checked:
1. Monitor: any name-based lookup (pgrep, pidof, `ps … | grep`).
2. Bash: a name-based lookup inside a while/until loop (a background Bash
   command hangs the same way). One-off `pgrep` inspection, and a `for` loop
   that polls for a PID to capture, are allowed.
3. Bash: a process escaping the harness — `nohup`, or `&` (also zsh `&!`/`&|`)
   with no later `wait`/`kill` (not `kill -0`) in the same command (covers
   `setsid … &`, `disown`, `( … ) &`). It loses what a background Bash
   command gives for free (stdout+stderr capture, an exit notification) and
   leaves no record. No opt-out: background tasks die with the agent that
   launched them, so a job that must outlive the agent needs a supervisor.
   Remote commands (`ssh host 'nohup x &'`) aren't checked: they detach from
   the remote shell, not from Claude Code.
4. Both: `pkill -f X`, whose pattern matches the wrapping shell's argv too,
   so it kills the very command running it.
"""
import re

QUICK = re.compile(r"\b(pgrep|pidof|ps|nohup|pkill)\b|&")
GREPS = {"grep", "egrep", "fgrep", "rg"}

LADDER = (
    "Instead:\n"
    "1. To wait for a process: launch it as a background Bash command "
    "(run_in_background, no nohup, no trailing `&`) and add no Monitor. It "
    "captures stdout+stderr to its task output file and notifies you when the "
    "process exits, however it exits.\n"
    "2. Use Monitor only for events that aren't the process exiting: output you "
    "must react to while it runs (e.g. `tail -n +1 -F /abs/task.output | grep "
    "--line-buffered -E 'ERROR|step'`; the background launch still reports the "
    "exit), or events outside any process, such as an external service. For "
    "persistent or important workflows, have the process serve a websocket and "
    "bind Monitor to it with `ws`.\n"
    "3. If the condition is more complex than that, write a small Python launcher "
    "script that starts the process, watches it and its output, and prints "
    "events; launch that script in the background instead of emulating it in "
    "shell."
)
WHY_NAME = (
    "{what} finds processes by name. `pgrep -f X` always matches the shell "
    "Claude Code runs this command in (its argv contains X), so the wait "
    "never ends; without -f, truncated process names (15 chars on Linux) and "
    "processes under a wrapper (uv, npm, python3) never match, so it ends at "
    "once."
)
WHY_NOHUP = (
    "this detaches a process from Claude Code (`nohup`, or `&` with no later "
    "`wait`/`kill` in the same command): its output goes wherever the "
    "redirection you wrote says, its exit is never reported, nothing "
    "supervises it, and it leaves no record in the session. Background tasks "
    "are killed when the agent that launched them finishes, so a job that "
    "must outlive you goes through a supervisor instead:\n"
    "- Long jobs (training runs, big batch work): babysit them as a background "
    "Bash command from the agent that stays alive, hand them to the user, or "
    "submit them to the environment's scheduler (Slurm `sbatch`, a Kubernetes "
    "Job, …).\n"
    "- Services and admin processes: register them with the environment's "
    "service manager (`systemd-run --user`/a systemd unit, launchd, docker "
    "compose, supervisord, …). Ask the user if it isn't clear which one."
)
WHY_PKILL = (
    "`pkill -f {pat}` matches full command lines, and the shell Claude Code "
    "runs this command in has the pattern in its own argv, so it kills this "
    "command too. Kill by PID instead: the PID a background launch printed, "
    "or one you looked up and checked first (`pgrep -fl PATTERN` as its own "
    "call, then `kill <pid>`)."
)


def lookup(cmd, cmds):
    """'pgrep'/'pidof'/'ps … | grep' if cmd finds processes by name, else None."""
    name = cmd.inv.name
    if name in ("pgrep", "pidof"):
        return name
    if name == "ps" and cmd.ctx.pipe:
        vals = [a.value or "" for a in cmd.inv.args]
        if any(v in ("-p", "--pid", "-q") or v.startswith(("--pid=", "-p")) and v[2:].isdigit() for v in vals):
            return None
        pid, k, _ = cmd.ctx.pipe
        if any(c.ctx.pipe and c.ctx.pipe[0] == pid and c.ctx.pipe[1] > k and c.inv and c.inv.name in GREPS
               for c in cmds):
            return "ps … | grep"
    return None


def pkill_pattern(cmd):
    if cmd.inv.name != "pkill":
        return None
    vals = [a.value or "" for a in cmd.inv.args]
    flags = [v for v in vals if v.startswith("-") and not v.startswith("--")]
    if "--full" in vals or any("f" in v[1:] for v in flags):
        rest = [a.src for a in cmd.inv.args if not (a.value or "").startswith("-")]
        return rest[-1] if rest else "…"
    return None


def reaped_later(cmd, cmds):
    for c in cmds[cmd.seq + 1:]:
        if c.ctx.bg or not c.inv:
            continue
        if c.inv.name == "wait":
            return True
        if c.inv.name == "kill" and c.inv.args and c.inv.args[0].value != "-0":
            return True
    return False


def check(view):
    """Deny reason for a Bash/Monitor command (shellparse view), or None."""
    if view.tool not in ("Bash", "Monitor") or not QUICK.search(view.text):
        return None
    cmds = view.commands()
    if cmds is None:
        return None  # can't tell: allow
    from shellparse import Simple
    for cmd in cmds:
        if type(cmd.node) is not Simple:
            continue
        pat = pkill_pattern(cmd)
        if pat:
            return WHY_PKILL.format(pat=pat)
        found = lookup(cmd, cmds)
        if view.tool == "Monitor":
            if found:
                return WHY_NAME.format(what=f"`{found}`") + "\n\n" + LADDER
            continue
        if found and any(k in ("while", "until") for k in cmd.ctx.loops):
            return WHY_NAME.format(what=f"this loop waits on `{found}`, which") + "\n\n" + LADDER
        if cmd.ctx.remote:
            continue
        if "nohup" in cmd.inv.wrappers or cmd.inv.name == "nohup" or cmd.inv.name == "disown":
            return WHY_NOHUP + "\n\n" + LADDER
        if cmd.ctx.bg and not reaped_later(cmd, cmds):
            return WHY_NOHUP + "\n\n" + LADDER
    return None
