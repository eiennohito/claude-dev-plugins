#!/usr/bin/env python3
"""PreToolUse hook — deny process-wait commands that can hang or end early.

Registered (hooks/hooks.json) for PreToolUse matcher "Monitor|Bash". Waiting on
a process by *name* is wrong in both directions:

- False positive → hang. Claude Code runs every command as
  `<shell> -c "… eval '<command>'"` (verified with zsh on Linux), so
  `pgrep -f X` always matches the wrapping shell, whose argv contains X. `while pgrep -f X; do sleep 5; done` never ends
  until the Monitor timeout.
- False negative → ends early. Without -f, pgrep/pidof match the 15-char comm
  name, so a long script name or a process under a wrapper (uv, npm, python3)
  never matches.

Waiting for a process to exit is the background Bash launch's job: the harness
captures its output and notifies on exit, however it exits. Monitor is for
events that aren't the process exiting (output lines, external services), so
following a log is fine.

Checked:
1. Monitor: any name-based lookup (pgrep, pidof, `ps … | grep`).
2. Bash: a name-based lookup inside a while/until … done loop (a background
   Bash command hangs the same way). One-off `pgrep` inspection, and a `for`
   loop that polls for a PID to capture, are allowed.
3. Bash: a process escaping the harness — `nohup`, or an `&` with no later
   `wait`/`kill` (not `kill -0`) in the same command (covers `setsid … &`,
   `disown`, `( … ) &`). It loses what a background Bash command gives for
   free (stdout+stderr capture, an exit notification) and leaves no record.
   No opt-out: background tasks die with the agent that launched them, so a
   job that must outlive the agent needs a supervisor — babysitting by the
   agent that stays alive, the user, a scheduler (Slurm, …) for long jobs, or
   a service manager (systemd, launchd, …) for services. Quoted text and
   escaped `\\&` are ignored.

Heredoc bodies are stripped first, so writing a launcher script through a
heredoc doesn't trigger. Monitor's `ws` mode has no command and is allowed.
Never fails the tool call on its own errors: bad hook input → allow.
"""
import json
import re
import sys

QUICK = re.compile(r"\b(pgrep|pidof|ps|nohup)\b|&")
# nohup in command position, possibly behind setsid/nice/env/sudo wrappers
NOHUP = re.compile(
    r"(?:^|[;&|(]|\b(?:then|do|else))\s*"
    r"(?:(?:setsid|nice(?:\s+-n\s*-?\d+)?|env|sudo|exec|\w+=\S*)\s+)*"
    r"nohup\s", re.M)
QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", re.S)
# a lone, unescaped `&` that backgrounds (not &&, >&, &>, |&, or `&x` in code)
BG_AMP = re.compile(r"(?<![&>|\\])&(?=[\s);}]|$)")
REAPED = re.compile(r"\bwait\b|\bkill\s+(?!-0\b)")
HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(['\"]?)([A-Za-z_]\w*)\2")
NAME_LOOKUP = re.compile(
    r"\bpgrep\b|\bpidof\b|\bps\b(?![^|;&\n]*(?:\s-p\b|\s--pid\b))[^|;&\n]*\|\s*e?grep\b")
LOOP = re.compile(r"(?:^|[;&|(\s])(?:while|until)\s.*?\bdone\b", re.S)
COMMENT = re.compile(r"(?m)(?:^|(?<=\s))#.*$")

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


def strip_heredocs(cmd):
    """Drop heredoc bodies (lines after `<<TAG` up to the TAG line)."""
    out, pending = [], []
    for line in cmd.split("\n"):
        if pending:
            tag, dash = pending[0]
            if (line.lstrip("\t") if dash else line) == tag:
                pending.pop(0)
            continue
        out.append(line)
        pending = [(m.group(3), m.group(1) == "-") for m in HEREDOC.finditer(line)]
    return "\n".join(out)


def deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "Denied by probe-tools: " + reason,
        }
    }, ensure_ascii=False))


def problem(tool, cmd):
    cmd = strip_heredocs(cmd)
    name = NAME_LOOKUP.search(cmd)
    if tool == "Monitor":
        if name:
            return WHY_NAME.format(what=f"`{name.group(0).split()[0]}`")
    else:
        cmd = COMMENT.sub("", cmd)
        if NOHUP.search(cmd):
            return WHY_NOHUP
        bare = QUOTED.sub("''", cmd)
        amp = BG_AMP.search(bare)
        if amp and not REAPED.search(bare, amp.end()):
            return WHY_NOHUP
        for loop in LOOP.finditer(cmd):
            name = NAME_LOOKUP.search(loop.group(0))
            if name:
                return WHY_NAME.format(
                    what=f"this loop waits on `{name.group(0).split()[0]}`, which")
    return None


def main():
    try:
        data = json.load(sys.stdin)
        tool = data["tool_name"]
        cmd = data["tool_input"].get("command")
    except Exception:
        return
    if not isinstance(cmd, str) or not QUICK.search(cmd):
        return
    why = problem(tool, cmd)
    if why:
        deny(f"{why}\n\n{LADDER}")


if __name__ == "__main__":
    main()
