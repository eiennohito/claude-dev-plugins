"""End-to-end: run bin/hook.py the way Claude Code does and time it.

Every PreToolUse call pays this process, so: median under 50 ms, none over
100 ms, measured through `sh -c` with JSON on stdin, over the whole corpus.
SessionStart runs a few times per session and only has to stay reasonable.

    python3 tests/test_hook.py
"""
import json
import os
import statistics
import subprocess
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(os.path.dirname(HERE), "bin", "hook.py")
TARGET_MS, LIMIT_MS, SESSION_START_MS = 50, 100, 1000


def call(data, env=None):
    t = time.perf_counter()
    r = subprocess.run(["sh", "-c", f'python3 "{HOOK}"'], input=json.dumps(data), text=True,
                       capture_output=True, env=dict(os.environ, CLAUDE_PROJECT_DIR="/proj", **(env or {})))
    ms = (time.perf_counter() - t) * 1000
    out = json.loads(r.stdout) if r.stdout.strip() else None
    return out, ms, r


def pre(tool, tool_input):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input, "cwd": "/proj"}


def decision(out):
    return out and out["hookSpecificOutput"].get("permissionDecision")


class Hook(unittest.TestCase):
    def test_routes(self):
        out, _, r = call(pre("Bash", {"command": "cd /x && rm -rf build & "}))
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertEqual(decision(out), "deny")
        self.assertIn("relative path", reason)  # destructive
        self.assertIn("detaches a process", reason)  # monitor, same call
        self.assertIsNone(call(pre("Bash", {"command": "ls -la"}))[0])
        self.assertEqual(decision(call(pre("Monitor", {"command": "pgrep -f x"}))[0]), "deny")
        self.assertEqual(decision(call(pre("Agent", {"prompt": "x"}))[0]), "deny")
        self.assertIsNone(call(pre("Agent", {"prompt": "x", "model": "sonnet"}))[0])
        self.assertIsNone(call(pre("Read", {"file_path": "/x"}))[0])

    def test_bad_input_allows(self):
        for data in ("not json", '{"hook_event_name": "PreToolUse"}',
                     '{"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": 5}}'):
            r = subprocess.run(["python3", HOOK], input=data, text=True, capture_output=True)
            self.assertEqual((r.returncode, r.stdout), (0, ""), data)

    def test_session_start(self):
        out, ms, _ = call({"hook_event_name": "SessionStart", "source": "startup"})
        self.assertIn("## Available tools", out["hookSpecificOutput"]["additionalContext"])
        self.assertLess(ms, SESSION_START_MS)

    def test_speed_over_corpus(self):
        with open(os.path.join(HERE, "corpus.jsonl"), encoding="utf-8") as f:
            cases = [json.loads(line) for line in f]
        times = []
        for case in cases:
            _, ms, r = call(pre(case["tool"], {"command": case["cmd"]}),
                            env={"SHELL": "/bin/zsh" if case["shell"] == "zsh" else "/bin/bash"})
            self.assertEqual(r.returncode, 0, r.stderr)
            times.append((ms, case["id"]))
        times.sort()
        median = statistics.median(t for t, _ in times)
        print(f"\nhook over {len(times)} corpus commands: median {median:.0f} ms, "
              f"max {times[-1][0]:.0f} ms ({times[-1][1]})")
        self.assertLess(median, TARGET_MS)
        self.assertLess(times[-1][0], LIMIT_MS)


if __name__ == "__main__":
    unittest.main()
