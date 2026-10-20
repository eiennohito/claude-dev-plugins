"""Check hook verdicts against the labelled corpus of real agent commands.

corpus.jsonl holds commands mined from Claude Code transcripts (bash on
Linux, zsh on macOS; usernames and hostnames replaced), each labelled with
the verdict its policy expects from four consumers: destructive and monitor
(the probe-tools guards), python_c and push (reference consumers defined
here, to keep the parser able to answer those questions too).

    python3 tests/test_corpus.py            # unittest
    python3 tests/test_corpus.py --report   # every disagreement, with reasons
"""
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))

import destructive  # noqa: E402
import monitor  # noqa: E402
import shellparse as sp  # noqa: E402

ROOT = "/proj"
PY = re.compile(r"(python|pypy)(\d+(\.\d+)*)?$")
GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}


def stdin_body(cmd):
    for r in cmd.redirs:
        if r.fd in (None, "0") and (r.heredoc is not None or r.op == "<<<"):
            return r.heredoc.body if r.heredoc is not None else (r.target.value or r.target.src)
    return None


def python_c(cmds):
    """True if a python interpreter runs a >2-line -c program or a non-empty
    program from a heredoc/here-string."""
    env = {}
    for cmd in cmds:
        if type(cmd.node) is not sp.Simple:
            continue
        if not cmd.node.words:
            for name, w in cmd.assigns:
                env[name] = w.expand(env)
            continue
        inv, name, args = cmd.inv, cmd.inv.name, cmd.inv.args
        if name is None and inv.word is not None:
            v = inv.word.expand(env)
            last = inv.word.parts[-1]
            if v is None and type(last) is sp.Lit and "/" in last.text:
                v = last.text  # `$W/.venv$v/bin/python`: the program name is literal
            name = os.path.basename(v) if v else None
        if name == "-" and "uv" in inv.wrappers:  # `uv run - <<EOF`
            name, args = "python", [inv.word] + args
        if not name or not PY.match(name):
            continue
        prog, stdin = None, False
        skip = False
        for k, a in enumerate(args):
            v = a.value
            if skip:
                skip = False
                continue
            if v in ("-X", "-W"):
                skip = True
                continue
            if v is not None and re.fullmatch(r"-[BdEhiIOqsSuvx]*c", v):
                prog = args[k + 1] if k + 1 < len(args) else None
                break
            if v == "-":
                stdin = True
                break
            if v is None or not v.startswith("-") or v.startswith("-m"):
                break
        else:
            stdin = True
        if prog is not None:
            text = prog.value if prog.value is not None else prog.src
            if len(text.strip("\n").splitlines()) > 2:
                return True
        elif stdin and (stdin_body(cmd) or "").strip():
            return True
    return False


def push(cmds):
    for cmd in cmds:
        if type(cmd.node) is not sp.Simple or cmd.inv.name != "git":
            continue
        vals = [a.value or "" for a in cmd.inv.args]
        i = 0
        while i < len(vals) and vals[i].startswith("-"):
            i += 2 if vals[i] in GIT_VALUE_OPTS else 1
        if i < len(vals) and vals[i] == "push":
            return True
    return False


def verdicts(case):
    view = sp.View(case["tool"], case["cmd"], zsh=case["shell"] == "zsh")
    cmds = view.commands()
    return {
        "destructive": "deny" if destructive.check(view, ROOT, ROOT) else "allow",
        "monitor": "deny" if monitor.check(view) else "allow",
        "python_c": "block" if cmds is not None and python_c(cmds) else "allow",
        "push": "deny" if cmds is not None and push(cmds) else "allow",
    }


def load():
    with open(os.path.join(HERE, "corpus.jsonl"), encoding="utf-8") as f:
        return [json.loads(line) for line in f]


class Corpus(unittest.TestCase):
    def test_verdicts(self):
        bad = []
        for case in load():
            got = verdicts(case)
            for k, want in case["expect"].items():
                if got[k] != want:
                    bad.append(f"{case['id']} {k}: want {want}, got {got[k]}")
        self.assertEqual(bad, [])


def report():
    from collections import Counter
    score = Counter()
    for case in load():
        got = verdicts(case)
        for k, want in case["expect"].items():
            score[k, got[k] == want] += 1
            if got[k] != want:
                print(f"--- {case['id']} {k}: want {want}, got {got[k]}  [{case['shell']}/{case['tool']}]")
                print("    why:", (case.get("why") or "")[:300])
                print("    cmd:", case["cmd"][:600].replace("\n", "\n         "))
    for k in ("destructive", "monitor", "python_c", "push"):
        print(f"{k:12s} {score[k, True]:4d} ok  {score[k, False]:3d} wrong")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    else:
        unittest.main()
