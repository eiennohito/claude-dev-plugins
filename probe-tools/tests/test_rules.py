"""Command rules (lib/rules.py): match the parsed command, not its text.

    python3 tests/test_rules.py
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))

import rules  # noqa: E402
import shellparse as sp  # noqa: E402


class Rules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = os.path.join(self.tmp.name, "config")
        self.root = os.path.join(self.tmp.name, "proj")
        os.environ["CLAUDE_CONFIG_DIR"] = self.config

    def tearDown(self):
        del os.environ["CLAUDE_CONFIG_DIR"]
        self.tmp.cleanup()

    def write(self, where, data):
        d = os.path.join(where, "probe-tools") if where == self.config else os.path.join(where, ".claude", "probe-tools")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "rules.json"), "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def deny(self, text, tool="Bash"):
        return rules.check(sp.View(tool, text, zsh=False), self.root)

    def test_git_push(self):
        self.write(self.config, {"deny": [{"command": "git push", "reason": "the user pushes"}]})
        for text in ("git push", "git push origin main", "git --no-pager push", "git -C /r -c a=b push -u o b",
                     "command git push", "env X=1 git push", "sudo -u me git push", "cd /r && git push",
                     "sh -c 'git push'", "eval git push", "ssh pod1 'cd r; git push'", "x=$(git push 2>&1)",
                     "bash <<'EOF'\ngit push\nEOF"):
            self.assertIn("the user pushes", self.deny(text) or "", text)
        for text in ("git log --grep push", "git commit -m 'fix push'", "echo git push", "git pull",
                     "# git push", "cat <<'EOF'\ngit push\nEOF", "git stash push", "git $CMD"):
            self.assertIsNone(self.deny(text), text)
        self.assertIn("on pod1", self.deny("ssh pod1 git push"))

    def test_flags_and_alternatives(self):
        self.write(self.config, {"deny": [{"command": "git push -f|--force|--force-with-lease"},
                                          {"command": "git reset --hard"}, {"command": "rm|unlink -r"}]})
        for text in ("git push -fu origin b", "git push --force-with-lease=main origin", "git reset HEAD~1 --hard",
                     "rm -rf /tmp/x", "unlink -r x"):
            self.assertTrue(self.deny(text), text)
        for text in ("git push origin b", "git reset --soft HEAD~1", "rm -f /tmp/x", "git log --force"):
            self.assertIsNone(self.deny(text), text)

    def test_project_rules_and_errors(self):
        self.write(self.root, {"deny": [{"command": "make deploy"}]})
        self.assertTrue(self.deny("make deploy"))
        self.write(self.config, '{"deny": [{"command": "git push"}, {"cmd": "oops"}]')
        loaded, errors = rules.load(self.root)
        self.assertEqual([r.command for r in loaded], ["make deploy"])  # the broken file applies not at all
        self.assertEqual(len(errors), 1)

    def test_no_rules_no_parse(self):
        view = sp.View("Bash", "git push 'unterminated", zsh=False)
        self.assertIsNone(rules.check(view, self.root))
        self.assertIs(view._cmds, False)  # never parsed
        self.write(self.config, {"deny": [{"command": "git push"}]})
        self.assertIn("couldn't be parsed", rules.check(view, self.root))


if __name__ == "__main__":
    unittest.main()
