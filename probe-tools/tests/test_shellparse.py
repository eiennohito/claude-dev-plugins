"""Parser and guard unit tests: constructs a regex-based check gets wrong.

    python3 tests/test_shellparse.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))

import destructive  # noqa: E402
import monitor  # noqa: E402
import shellparse as sp  # noqa: E402


def run(text):
    """[(name, [arg values or src]) …] of executed commands, in order."""
    return [(c.inv.name, [a.value if a.value is not None else a.src for a in c.inv.args])
            for c in sp.commands(text) if type(c.node) is sp.Simple and c.node.words]


def names(text):
    return [n for n, _ in run(text)]


def zsh_names(text):
    return [c.inv.name for c in sp.commands(text, zsh=True) if type(c.node) is sp.Simple and c.node.words]


def cmd(text, name):
    return next(c for c in sp.commands(text) if type(c.node) is sp.Simple and c.inv.name == name)


def deny_destructive(text, cwd="/proj", root="/proj"):
    return destructive.check(sp.View("Bash", text), cwd, root)


def deny_monitor(text, tool="Bash"):
    return monitor.check(sp.View(tool, text))


class Parse(unittest.TestCase):
    def test_quotes_are_data(self):
        self.assertEqual(names("git commit -m 'rm -rf / && git push'"), ["git"])
        self.assertEqual(run('echo "a; b | c && d"'), [("echo", ["a; b | c && d"])])
        self.assertEqual(run("echo $'it\\'s'"), [("echo", ["it's"])])
        self.assertEqual(run('echo "x \\"y\\" $HOME"')[0][1], ['"x \\"y\\" $HOME"'])

    def test_comments(self):
        self.assertEqual(names("ls # rm -rf /\necho a#b"), ["ls", "echo"])
        self.assertEqual(run("echo a#b"), [("echo", ["a#b"])])

    def test_operators(self):
        self.assertEqual(names("a && b || c; d | e |& f"), list("abcdef"))
        self.assertEqual(names("a 2>&1 >/dev/null &> log; b"), ["a", "b"])

    def test_substitutions_execute(self):
        self.assertEqual(names("echo $(rm -rf x) `git push` <(pgrep y)"), ["rm", "git", "pgrep", "echo"])
        self.assertEqual(names('x="$(cat <<EOF\nhi $(id)\nEOF\n)"; echo'), ["id", "cat", "echo"])

    def test_case_parens_inside_substitution(self):
        self.assertEqual(names("x=$(case $y in a) rm a;; *) ls;; esac); echo"), ["rm", "ls", "echo"])

    def test_heredoc_bodies_are_data(self):
        self.assertEqual(names("cat > f <<'EOF'\nrm -rf /\nEOF\necho done"), ["cat", "echo"])
        self.assertEqual(names("cat <<-EOF\n\trm x\n\tEOF\nls"), ["cat", "ls"])
        self.assertEqual(names("a <<A; b <<B\n1\nA\n2\nB\nc"), ["a", "b", "c"])

    def test_heredoc_to_shell_is_code(self):
        self.assertEqual(names("bash <<'EOF'\nrm -rf $D/x\nEOF"), ["bash", "rm"])
        self.assertEqual(names("ssh pod1 <<'EOF'\ngit push\nEOF"), ["ssh", "git"])

    def test_string_evaluation(self):
        self.assertEqual(names("eval 'rm a; ls'"), ["eval", "rm", "ls"])
        self.assertEqual(names("bash -lc 'cd /x && git reset --hard'"), ["bash", "cd", "git"])
        self.assertEqual(names("sudo -u me timeout 5 sh -c 'pkill -f x'"), ["sh", "pkill"])

    def test_ssh_remote(self):
        c = cmd("ssh -o BatchMode=yes -p 22 pod1 'cd /x; nohup run &'", "run")
        self.assertEqual(c.ctx.remote, "pod1")
        self.assertEqual(c.inv.wrappers, ["nohup"])

    def test_wrappers(self):
        self.assertEqual(cmd("noglob rm -rf x", "rm").inv.wrappers, ["noglob"])
        self.assertEqual(run("env -u X A=1 B=2 nice -n 5 rm -f y"), [("rm", ["-f", "y"])])
        self.assertEqual(run("uv run --project /p --with x python -c pass"), [("python", ["-c", "pass"])])
        self.assertEqual(cmd("find . | xargs -I{} -P 4 rm {}", "rm").inv.wrappers, ["xargs"])
        self.assertEqual(names("command -v rm"), ["command"])

    def test_context(self):
        c = cmd("while pgrep -f x; do sleep 1; done", "pgrep")
        self.assertEqual((c.ctx.loops, c.ctx.cond), (("while",), True))
        self.assertEqual(cmd("sleep 9 &!", "sleep").ctx.bg, "&!")
        self.assertEqual(cmd("sleep 9 &|", "sleep").ctx.bg, "&|")
        self.assertEqual(cmd("(a; sleep 9) & wait", "sleep").ctx.bg, "&")
        self.assertIsNone(cmd("a && sleep 9", "sleep").ctx.bg)
        self.assertTrue(cmd("(cd /x; ls)", "ls").ctx.subshell)

    def test_compounds(self):
        self.assertEqual(names("if a; then b; elif c; then d; else e; fi"), list("abcde"))
        self.assertEqual(names("for f in *.py; do rm $f; done"), ["rm"])
        self.assertEqual(names("for ((i=0;i<3;i++)); do a; done"), ["a"])
        self.assertEqual(names("f() { rm x; }; f"), ["rm", "f"])
        self.assertEqual(names("function g { a; }"), ["a"])
        self.assertEqual(names("[[ $(id -u) == 0 && -n $x ]] && b"), ["id", "b"])
        self.assertEqual(names("[[ $x =~ ^(a|b)$ ]] && c"), ["c"])
        self.assertEqual(names("(( n > 1 )) && a"), ["a"])
        self.assertEqual(names("time ( a; b )"), ["a", "b"])
        self.assertEqual(names("! a | b"), ["a", "b"])

    def test_zsh(self):
        self.assertEqual(run("ls *(.N) **/*.py(om[1])"), [("ls", ["*(.N)", "**/*.py(om[1])"])])
        self.assertEqual(names("arr=(a 'b c' $(id)); echo ${arr[@]}"), ["id", "echo"])
        self.assertEqual(zsh_names("for x (a b) { rm $x }"), ["rm"])
        self.assertEqual(zsh_names("{ a; b }; c"), ["a", "b", "c"])
        self.assertEqual(run("echo }"), [("echo", ["}"])])  # bash: an argument
        with self.assertRaises(sp.ParseError):
            sp.commands("echo }", zsh=True)  # zsh: a parse error, as in zsh itself
        self.assertEqual(zsh_names("bash -c '{ echo }; }'"), ["bash", "echo"])
        self.assertEqual(names("diff =(a) =(b)"), ["a", "b", "diff"])

    def test_params(self):
        w = sp.parse("rm ${D:-/}x").items[0][0].pipelines[0].cmds[0].words[1]
        self.assertEqual((w.parts[0].name, w.parts[0].op), ("D", ":-"))
        self.assertIsNone(w.expand({}))
        self.assertEqual(w.expand({"D": "/a"}), "/ax")
        self.assertEqual(w.expand({"D": ""}), "/x")
        self.assertEqual(sp.lit_word("x").expand({}), "x")

    def test_line_continuation(self):
        self.assertEqual(run("rm \\\n  -rf \\\n  /a/b"), [("rm", ["-rf", "/a/b"])])

    def test_errors(self):
        for bad in ("echo 'x", 'echo "x', "echo $(x", "if a; then b", "a |", "( a"):
            with self.assertRaises(sp.ParseError, msg=bad):
                sp.commands(bad)


class Destructive(unittest.TestCase):
    def test_unresolved_default_expansion(self):
        self.assertIn("rm", deny_destructive("rm -rf ${D:-/}x"))

    def test_wrapped_rm(self):
        self.assertTrue(deny_destructive("noglob rm -rf $D/x"))
        self.assertTrue(deny_destructive("sudo -u root rm -rf $D/x"))

    def test_resolved(self):
        self.assertIsNone(deny_destructive("D=/proj/build; rm -rf $D/x"))
        self.assertIsNone(deny_destructive('rm -rf "$tmp"'))
        self.assertIsNone(deny_destructive("rm -f /tmp/x-$(id -u).json"))
        self.assertIsNone(deny_destructive("git commit -m 'rm -rf $D/x'"))

    def test_cd_then_relative(self):
        self.assertTrue(deny_destructive("cd /elsewhere && rm -rf build"))
        self.assertTrue(deny_destructive("cd /elsewhere; git reset --hard"))
        self.assertIsNone(deny_destructive("cd /elsewhere; git status"))
        self.assertTrue(deny_destructive("bash -c 'cd /x && git clean -fd'"))

    def test_remote_not_checked(self):
        self.assertIsNone(deny_destructive("ssh pod1 'cd /x && rm -rf $D/y'"))

    def test_runtime_sets(self):
        self.assertTrue(deny_destructive("find /a -name '*.pyc' -delete"))
        self.assertTrue(deny_destructive("ls | xargs rm"))
        self.assertTrue(deny_destructive("git push --force"))
        self.assertIsNone(deny_destructive("git push --force origin main"))

    def test_catastrophic(self):
        self.assertTrue(deny_destructive("rm -rf / tmp/x"))
        self.assertTrue(deny_destructive("rm -rf ~/*"))

    def test_unparseable(self):
        self.assertTrue(deny_destructive("rm -rf 'x"))


class Monitor(unittest.TestCase):
    def test_zsh_disown(self):
        self.assertTrue(deny_monitor("sleep 100 &!"))
        self.assertTrue(deny_monitor("sleep 100 &|"))

    def test_background_reaped(self):
        self.assertIsNone(deny_monitor("a & b & wait"))
        self.assertTrue(deny_monitor("nohup a > log 2>&1"))
        self.assertIsNone(deny_monitor("a && b >&2 &>/dev/null"))
        self.assertIsNone(deny_monitor("echo 'x & y'; cat <<EOF\nnohup x &\nEOF"))

    def test_remote_detach_allowed(self):
        self.assertIsNone(deny_monitor("ssh pod1 'nohup run > log 2>&1 &'"))

    def test_waits_by_name(self):
        self.assertTrue(deny_monitor("until ! pgrep -f job; do sleep 5; done"))
        self.assertTrue(deny_monitor('until [ "$(ps aux | grep job | wc -l)" -eq 0 ]; do sleep 5; done'))
        self.assertTrue(deny_monitor("ssh pod1 'while pgrep -f job; do sleep 5; done'"))
        self.assertIsNone(deny_monitor("pgrep -fl job"))
        self.assertIsNone(deny_monitor("for i in 1 2 3; do pid=$(pgrep -n job) && break; sleep 1; done"))
        self.assertIsNone(deny_monitor("while kill -0 $PID; do sleep 5; done"))
        self.assertTrue(deny_monitor("pgrep -f job", tool="Monitor"))
        self.assertIsNone(deny_monitor("tail -F log | grep --line-buffered ERROR", tool="Monitor"))

    def test_pkill_self_match(self):
        self.assertTrue(deny_monitor("pkill -f train.py"))
        self.assertIsNone(deny_monitor("pkill train"))


if __name__ == "__main__":
    unittest.main()
