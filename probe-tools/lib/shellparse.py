"""Shell command parser for hooks that inspect Bash tool commands.

Parses what an agent passes to the Bash (or Monitor) tool — bash or zsh — into
a tree, then flattens it into the simple commands that would execute, each
tagged with the context hooks care about: inside which loop, backgrounded,
in a subshell or pipeline, nested in `$(…)`/`eval`/`sh -c`, or run remotely
through `ssh`. Words keep their structure (literal, quoted, `$param`,
`$(…)`), so callers can tell code from data and resolve paths without
regexes over the raw text.

Not a shell: nothing is expanded, and aliases, functions or variables defined
outside the command are unknown. Input it can't parse raises ParseError, so
each hook picks its own fallback (deny or allow). Linear in the input size:
plain runs, quoted strings and heredoc bodies are consumed with regexes or
str.find. Python 3.8+ stdlib only.

    import shellparse
    for cmd in shellparse.commands("cd /x && rm -rf $D/y &"):
        cmd.inv.name, [w.value for w in cmd.inv.args], cmd.ctx.bg
"""
import os
import re

MAX_LEN = 300_000  # characters; longer input raises ParseError
MAX_NEST = 60      # syntactic nesting depth
MAX_STRINGS = 6    # nested string parses: eval, sh -c, ssh, …


class ParseError(ValueError):
    pass


# ---------------------------------------------------------------- words

class Lit:
    """Literal text. quoted: None (bare), "'", '"', "$'", "\\" (escaped char)."""
    __slots__ = ("text", "quoted")

    def __init__(self, text, quoted=None):
        self.text, self.quoted = text, quoted

    def __repr__(self):
        return f"Lit({self.text!r}{', ' + repr(self.quoted) if self.quoted else ''})"


class Param:
    """`$name` / `${name<op><arg>}`. name None when the expansion is too
    complex to model (zsh flags, nested `${${…}}`); op e.g. ':-', '#', '%'."""
    __slots__ = ("name", "op", "arg", "quoted")

    def __init__(self, name, op=None, arg=None, quoted=None):
        self.name, self.op, self.arg, self.quoted = name, op, arg, quoted

    def __repr__(self):
        return f"Param({self.name!r}, {self.op!r})"


class Sub:
    """Command/process substitution: kind '$(', '`', '<(', '>(', '=('."""
    __slots__ = ("kind", "script", "quoted")

    def __init__(self, kind, script, quoted=None):
        self.kind, self.script, self.quoted = kind, script, quoted

    def __repr__(self):
        return f"Sub({self.kind!r})"


class Arith:
    """`$(( … ))`: always unknown."""
    __slots__ = ("text", "quoted")

    def __init__(self, text, quoted=None):
        self.text, self.quoted = text, quoted


class Tilde:
    """Leading `~` or `~user`."""
    __slots__ = ("user",)

    def __init__(self, user):
        self.user = user


class Array:
    """`name=( … )` value."""
    __slots__ = ("words",)

    def __init__(self, words):
        self.words = words


GLOB = re.compile(r"[*?\[]|\{[^{}]*(,|\.\.)[^{}]*\}")


class Word:
    __slots__ = ("parts", "src")

    def __init__(self, parts, src):
        self.parts, self.src = parts, src

    def __repr__(self):
        return f"Word({self.src!r})"

    @property
    def value(self):
        """The word's text after quote removal, or None if it has expansions."""
        if len(self.parts) == 1 and type(self.parts[0]) is Lit:
            return self.parts[0].text
        out = []
        for p in self.parts:
            if type(p) is not Lit:
                return None
            out.append(p.text)
        return "".join(out)

    @property
    def quoted(self):
        return any(type(p) is not Tilde and type(p) is not Array and p.quoted for p in self.parts)

    def has_glob(self):
        return any(type(p) is Lit and not p.quoted and GLOB.search(p.text) for p in self.parts)

    def sole_param(self):
        """Name if the word is exactly one plain `$name` (quoted or not)."""
        if len(self.parts) == 1 and type(self.parts[0]) is Param and self.parts[0].op is None:
            return self.parts[0].name
        return None

    def subs(self):
        """Command substitutions inside the word, outermost first."""
        for p in self.parts:
            t = type(p)
            if t is Sub:
                yield p
            elif t is Param and p.arg is not None:
                yield from p.arg.subs()
            elif t is Array:
                for w in p.words:
                    yield from w.subs()

    def expand(self, env):
        """Expanded text given env {name: value or None (unknown)}, or None if
        anything can't be resolved. `~` uses env['HOME']."""
        out = []
        for p in self.parts:
            t = type(p)
            if t is Lit:
                out.append(p.text)
            elif t is Tilde:
                v = env.get("HOME") if p.user == "" else None
                if v is None:
                    return None
                out.append(v)
            elif t is Param:
                v = _expand_param(p, env)
                if v is None:
                    return None
                out.append(v)
            else:
                return None
        return "".join(out)

    def split_last(self):
        """(head Word, last component Word) split at the last unquoted or
        quoted literal '/', or None when there is no '/' in literal text."""
        for k in range(len(self.parts) - 1, -1, -1):
            p = self.parts[k]
            if type(p) is Lit and "/" in p.text:
                cut = p.text.rindex("/")
                head = self.parts[:k] + ([Lit(p.text[:cut] or "/", p.quoted)])
                tail = ([Lit(p.text[cut + 1:], p.quoted)] if p.text[cut + 1:] else []) + self.parts[k + 1:]
                return Word(head, ""), Word(tail, "")
        return None


def _expand_param(p, env):
    if p.name is None:
        return None
    if p.op is None:
        return env.get(p.name)
    v = env.get(p.name)
    if p.op in (":-", "-", ":=", "="):
        if v is None:
            return None  # unknown: could be empty or set
        if v == "" and p.op[0] == ":" or p.name not in env:
            return p.arg.expand(env) if p.arg is not None else ""
        return v
    if p.op in (":+", "+"):
        if v is None:
            return None
        return (p.arg.expand(env) if p.arg is not None else "") if v != "" or p.op == "+" else ""
    return None


def lit_word(text):
    return Word([Lit(text)], text)


# ---------------------------------------------------------------- tree

class Simple:
    __slots__ = ("assigns", "words", "redirs")

    def __init__(self, assigns, words, redirs):
        self.assigns, self.words, self.redirs = assigns, words, redirs


class Compound:
    """kind: '(', '{', 'while', 'until', 'for', 'select', 'if', 'case',
    'func', '[[', '(('. Fields used depend on kind."""
    __slots__ = ("kind", "body", "cond", "var", "words", "clauses", "else_", "items", "name", "text", "redirs")

    def __init__(self, kind, **kw):
        self.kind = kind
        self.body = self.cond = self.var = self.else_ = self.name = self.text = None
        self.words, self.clauses, self.items, self.redirs = [], [], [], []
        for k, v in kw.items():
            setattr(self, k, v)


class Redir:
    __slots__ = ("op", "fd", "target", "heredoc")

    def __init__(self, op, fd, target, heredoc=None):
        self.op, self.fd, self.target, self.heredoc = op, fd, target, heredoc


class Heredoc:
    __slots__ = ("delim", "quoted", "strip", "body", "parts")

    def __init__(self, delim, quoted, strip):
        self.delim, self.quoted, self.strip = delim, quoted, strip
        self.body, self.parts = "", []


class Pipeline:
    __slots__ = ("cmds", "ops", "negated")

    def __init__(self, cmds, ops, negated):
        self.cmds, self.ops, self.negated = cmds, ops, negated


class AndOr:
    __slots__ = ("pipelines", "ops")

    def __init__(self, pipelines, ops):
        self.pipelines, self.ops = pipelines, ops


class Script:
    """items: [(AndOr, separator)], separator one of ';', '&', '&!', '&|',
    '\\n' or None."""
    __slots__ = ("items",)

    def __init__(self, items):
        self.items = items


# ---------------------------------------------------------------- parser

PLAIN = re.compile(r"[^\s'\"\\$`;&|<>()]+")
RAW_PLAIN = re.compile(r"[^'\"\\$`]+")
DQ_PLAIN = re.compile(r'[^"\\$`]+')
HD_PLAIN = re.compile(r"[^\\$`]+")
BLANK = re.compile(r"(?:[ \t\r\f\v]+|\\\n)+")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ASSIGN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(\[[^\]]*\])?\+?=")
FUNC_NAME = re.compile(r"[^\s'\"\\$`;&|<>()=]+$")
FD = re.compile(r"(\d+|\{[A-Za-z_]\w*\})(?=[<>])")
PARAM = re.compile(
    r"(?P<pre>[#!]?)(?P<name>[A-Za-z_]\w*|\d+|[@*#?$!-])(?P<sub>\[[^\]]*\])?"
    r"(?P<op>:[-=?+]|[-=?+]|##|#|%%|%|//|/#|/%|/|\^\^|\^|,,|,|:|@)?")
OPS = (";;&", "&>>", "<<<", "<<-", ";;", ";&", ";|", "&&", "||", "|&", "&!", "&|",
       ">>", "<<", "<&", ">&", "<>", ">|", "&>", ";", "&", "|", "<", ">", "(", ")", "\n")
REDIR_OPS = frozenset(("<", ">", ">>", ">|", "<>", "&>", "&>>", ">&", "<&", "<<", "<<-", "<<<"))
CASE_END = frozenset((";;", ";&", ";;&", ";|"))
WORD_STOP = frozenset(" \t\r\n\f\v;&|<>)")
COMPOUND_START = frozenset(("{", "while", "until", "for", "select", "if", "case", "[[", "function"))
NO_COMMAND = frozenset(("then", "do", "done", "fi", "esac", "elif", "else", "}", "]]"))
ANSI = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b",
        "f": "\f", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}


class Parser:
    def __init__(self, s, nest=0, zsh=False):
        if len(s) > MAX_LEN:
            raise ParseError("command too long to check")
        self.s, self.i, self.n = s, 0, len(s)
        self.pending = []  # heredocs whose bodies start after the next newline
        self.nest = nest
        self.zsh = zsh  # zsh: a bare `}` closes a brace group even in argument position

    def error(self, msg):
        raise ParseError(f"{msg} at offset {self.i}")

    def enter(self):
        self.nest += 1
        if self.nest > MAX_NEST:
            self.error("nesting too deep")

    # -- low level

    def blank(self):
        m = BLANK.match(self.s, self.i)
        if m:
            self.i = m.end()
        if self.i < self.n and self.s[self.i] == "#":
            j = self.s.find("\n", self.i)
            self.i = self.n if j < 0 else j

    def newline(self):
        self.i += 1
        if self.pending:
            pending, self.pending = self.pending, []
            for h in pending:
                self.heredoc_body(h)

    def linebreak(self):
        while True:
            self.blank()
            if self.i < self.n and self.s[self.i] == "\n":
                self.newline()
            else:
                return

    def op(self):
        s, i = self.s, self.i
        if i >= self.n or s[i] not in ";&|<>()\n":
            return None
        if s[i] in "<>" and s.startswith("(", i + 1):
            return None  # process substitution: a word
        for o in OPS:
            if s.startswith(o, i):
                return o
        return None

    def peek_word(self):
        """Text of the next word if it is a bare literal (for reserved words)."""
        m = PLAIN.match(self.s, self.i)
        if not m:
            return None
        e = m.end()
        if e < self.n and self.s[e] not in WORD_STOP and self.s[e] != "(":
            return None
        return m.group()

    def at_term(self, terms):
        if self.i >= self.n:
            return True
        o = self.op()
        if o is not None:
            return o in terms
        w = self.peek_word()
        return w is not None and w in terms

    def expect(self, tok):
        self.linebreak()
        if self.s.startswith(tok, self.i) and (tok in OPS or self.peek_word() == tok):
            self.i += len(tok)
            return
        self.error(f"expected {tok!r}")

    # -- grammar

    def parse(self):
        script = self.script(frozenset())
        if self.i < self.n:
            self.error(f"unexpected {self.s[self.i]!r}")
        for h in self.pending:  # heredoc with no body before end of input
            h.body = ""
        return script

    def script(self, terms):
        items = []
        while True:
            self.linebreak()
            if self.at_term(terms):
                return Script(items)
            ao = self.and_or()
            self.blank()
            o = self.op()
            if o in (";", "&", "&!", "&|"):
                self.i += len(o)
                items.append((ao, o))
            elif o == "\n":
                self.newline()
                items.append((ao, "\n"))
            else:
                items.append((ao, None))
                self.linebreak()
                if not self.at_term(terms):
                    self.error("unexpected token")
                return Script(items)

    def and_or(self):
        pipes, ops = [self.pipeline()], []
        while True:
            self.blank()
            o = self.op()
            if o not in ("&&", "||"):
                return AndOr(pipes, ops)
            self.i += 2
            self.linebreak()
            ops.append(o)
            pipes.append(self.pipeline())

    def pipeline(self):
        self.blank()
        neg = False
        while True:
            w = self.peek_word()
            if w == "!":
                neg = not neg
                self.i += 1
            elif w == "time" and self._timed_compound():
                self.i = self._timed_compound()  # `time ( … )`: times a compound command
            else:
                break
            self.blank()
        cmds, ops = [self.command()], []
        while True:
            self.blank()
            o = self.op()
            if o not in ("|", "|&"):
                return Pipeline(cmds, ops, neg)
            self.i += len(o)
            self.linebreak()
            ops.append(o)
            cmds.append(self.command())

    def _timed_compound(self):
        """End of `time [-p]` if it prefixes a compound command, else None."""
        save = self.i
        self.i += 4
        self.blank()
        if self.peek_word() == "-p":
            self.i += 2
            self.blank()
        end = self.i if self.s.startswith("(", self.i) or self.peek_word() in COMPOUND_START else None
        self.i = save
        return end

    def command(self):
        self.enter()
        try:
            self.blank()
            s, i = self.s, self.i
            if s.startswith("((", i) and self.arith_end(i + 2):
                end = self.arith_end(i + 2)
                node = Compound("((", text=s[i + 2:end - 2])
                self.i = end
            elif s.startswith("(", i):
                self.i += 1
                node = Compound("(", body=self.script(frozenset(")")))
                self.expect(")")
            else:
                w = self.peek_word()
                if w == "{":
                    self.i += 1
                    node = Compound("{", body=self.script(frozenset(("}",))))
                    self.expect("}")
                elif w in ("while", "until"):
                    self.i += len(w)
                    cond = self.script(frozenset(("do",)))
                    self.expect("do")
                    node = Compound(w, cond=cond, body=self.script(frozenset(("done",))))
                    self.expect("done")
                elif w in ("for", "select"):
                    node = self.for_loop(w)
                elif w == "if":
                    node = self.if_()
                elif w == "case":
                    node = self.case()
                elif w == "function":
                    self.i += len(w)
                    self.blank()
                    name = self.word().value
                    self.blank()
                    if self.s.startswith("(", self.i):
                        self.i += 1
                        self.blank()
                        self.expect(")")
                    self.linebreak()
                    node = Compound("func", name=name, body=self.command())
                elif w == "[[":
                    node = self.cond_expr()
                elif w in NO_COMMAND:
                    self.error(f"unexpected {w!r}")
                else:
                    return self.simple()
            node.redirs = self.redirs()
            return node
        finally:
            self.nest -= 1

    def redirs(self):
        out = []
        while True:
            self.blank()
            m = FD.match(self.s, self.i)
            start = self.i
            fd = None
            if m:
                fd = m.group(1)
                self.i = m.end()
            o = self.op()
            if o not in REDIR_OPS:
                self.i = start
                return out
            self.i += len(o)
            out.append(self.redir(o, fd))

    def for_loop(self, kw):
        self.i += len(kw)
        self.blank()
        var, words = None, []
        if self.s.startswith("((", self.i):
            end = self.arith_end(self.i + 2, semis=True)
            if not end:
                self.error("unterminated (( in for")
            self.i = end
        else:
            var = self.peek_word()
            if not var or not NAME.fullmatch(var):
                self.error("bad for variable")
            self.i += len(var)
            self.linebreak()
            if self.peek_word() == "in":
                self.i += 2
                while True:
                    self.blank()
                    o = self.op()
                    if o == ";":
                        self.i += 1
                        break
                    if o == "\n":
                        self.newline()
                        break
                    if o is not None or self.i >= self.n:
                        self.error("bad for word list")
                    words.append(self.word())
            elif self.s.startswith("(", self.i):  # zsh: for x (a b c)
                self.i += 1
                while True:
                    self.linebreak()
                    if self.s.startswith(")", self.i):
                        self.i += 1
                        break
                    if self.i >= self.n:
                        self.error("unterminated for (")
                    words.append(self.word())
        self.blank()
        if self.s.startswith(";", self.i):
            self.i += 1
        self.linebreak()
        if self.peek_word() == "do":
            self.i += 2
            body = self.script(frozenset(("done",)))
            self.expect("done")
        elif self.peek_word() == "{":  # zsh short form
            self.i += 1
            body = self.script(frozenset(("}",)))
            self.expect("}")
        else:
            self.error("expected 'do'")
        return Compound(kw, var=var, words=words, body=body)

    def if_(self):
        self.i += 2
        clauses, else_ = [], None
        while True:
            cond = self.script(frozenset(("then",)))
            self.expect("then")
            clauses.append((cond, self.script(frozenset(("elif", "else", "fi")))))
            w = self.peek_word()
            if w not in ("elif", "else", "fi"):
                self.error("expected 'fi'")
            self.i += len(w)
            if w == "elif":
                continue
            if w == "else":
                else_ = self.script(frozenset(("fi",)))
                self.expect("fi")
            return Compound("if", clauses=clauses, else_=else_)

    def case(self):
        self.i += 4
        self.blank()
        word = self.word()
        self.linebreak()
        if self.peek_word() != "in":
            self.error("expected 'in'")
        self.i += 2
        items = []
        while True:
            self.linebreak()
            if self.peek_word() == "esac":
                self.i += 4
                return Compound("case", words=[word], items=items)
            if self.i >= self.n:
                self.error("unterminated case")
            if self.s.startswith("(", self.i):
                self.i += 1
            pats = []
            while True:
                self.blank()
                pats.append(self.word())
                self.blank()
                if self.s.startswith("|", self.i):
                    self.i += 1
                    continue
                if self.s.startswith(")", self.i):
                    self.i += 1
                    break
                self.error("bad case pattern")
            body = self.script(frozenset(CASE_END | {"esac"}))
            o = self.op()
            if o in CASE_END:
                self.i += len(o)
            items.append((pats, body))

    def cond_expr(self):
        self.i += 2
        words = []
        while True:
            self.linebreak()
            if self.peek_word() == "]]":
                self.i += 2
                return Compound("[[", words=words)
            if self.i >= self.n:
                self.error("unterminated [[")
            o = self.op()
            if o in ("&&", "||", "(", ")", "<", ">"):
                self.i += len(o)
                continue
            if o is not None:
                self.error(f"unexpected {o!r} in [[")
            w = self.word()
            words.append(w)
            if w.value == "=~":
                self.blank()
                words.append(self.regex_word())

    def regex_word(self):
        """RHS of =~: parentheses and | are part of the regex."""
        s, j, depth, start = self.s, self.i, 0, self.i
        while j < self.n:
            c = s[j]
            if c == "\\":
                j += 2
                continue
            if c in "'\"":
                k = s.find(c, j + 1)
                j = self.n if k < 0 else k + 1
                continue
            if c == "(":
                depth += 1
            elif c == ")":
                if not depth:
                    break
                depth -= 1
            elif c in " \t\n" and not depth:
                break
            j += 1
        self.i = j
        return Word([Lit(s[start:j])], s[start:j])

    def arith_end(self, j, semis=False):
        """Index just past the `))` closing an arithmetic expression starting
        at j, or None if the parens close as two separate `)` (subshells).
        semis: `;` is part of the expression (C-style for header)."""
        s, depth = self.s, 0
        while j < self.n:
            c = s[j]
            if c == "(":
                depth += 1
            elif c == ")":
                if depth:
                    depth -= 1
                elif s.startswith("))", j):
                    return j + 2
                else:
                    return None
            elif c in "'\"`&|\n" and not depth or c == ";" and not semis:
                return None
            j += 1
        return None

    def simple(self):
        assigns, words, redirs = [], [], []
        s = self.s
        while True:
            self.blank()
            if self.i >= self.n:
                break
            m = FD.match(s, self.i)
            if m:
                save = self.i
                self.i = m.end()
                o = self.op()
                if o in REDIR_OPS:
                    self.i += len(o)
                    redirs.append(self.redir(o, m.group(1)))
                    continue
                self.i = save
            o = self.op()
            if o in REDIR_OPS:
                self.i += len(o)
                redirs.append(self.redir(o, None))
                continue
            if o == "(" and len(words) == 1 and not assigns and not redirs \
                    and words[0].value and FUNC_NAME.match(words[0].value) \
                    and re.match(r"\(\s*\)", s[self.i:self.i + 32]):
                self.i = s.index(")", self.i) + 1
                self.linebreak()
                return Compound("func", name=words[0].value, body=self.command())
            if o is not None or self.zsh and self.peek_word() == "}":
                break
            w = self.word()
            a = None if words else split_assign(w)
            if a:
                assigns.append(a)
            else:
                words.append(w)
        if not (assigns or words or redirs):
            self.error("expected a command")
        return Simple(assigns, words, redirs)

    def redir(self, op, fd):
        self.blank()
        if self.i >= self.n:
            self.error("missing redirection target")
        w = self.word()
        if op in ("<<", "<<-"):
            delim = "".join(p.text if type(p) is Lit else "$" for p in w.parts)
            h = Heredoc(delim, w.quoted, op == "<<-")
            self.pending.append(h)
            return Redir(op, fd, w, h)
        return Redir(op, fd, w)

    def heredoc_body(self, h):
        tabs = r"\t*" if h.strip else ""
        m = re.compile(rf"^{tabs}{re.escape(h.delim)}$", re.M).search(self.s, self.i)
        if m:
            h.body = self.s[self.i:m.start()]
            self.i = min(m.end() + 1, self.n)
        else:  # unterminated: the body runs to end of input
            h.body = self.s[self.i:]
            self.i = self.n
        if not h.quoted and ("$" in h.body or "`" in h.body):
            sub = Parser(h.body, self.nest, self.zsh)
            h.parts = sub.heredoc_parts()

    def heredoc_parts(self):
        s, parts = self.s, []
        while self.i < self.n:
            c = s[self.i]
            if c == "\\":
                parts.append(Lit(s[self.i + 1:self.i + 2], "\\"))
                self.i += 2
            elif c == "$":
                parts.append(self.dollar("<<"))
            elif c == "`":
                parts.append(self.backtick("<<"))
            else:
                m = HD_PLAIN.match(s, self.i)
                parts.append(Lit(m.group(), "<<"))
                self.i = m.end()
        return parts

    # -- words

    def word(self, raw=False):
        """One word. raw: read to end of input (parameter-expansion args),
        where blanks and operators are literal."""
        s, n = self.s, self.n
        start = self.i
        parts = []
        plain = RAW_PLAIN if raw else PLAIN
        while self.i < n:
            i = self.i
            c = s[i]
            if not raw:
                if c in WORD_STOP:
                    if c in "<>" and s.startswith("(", i + 1):
                        parts.append(self.subst(c + "(", i + 2, None))
                        continue
                    break
                if c == "(":
                    if self.paren_in_word(parts, start):
                        continue
                    break
            if c == "'":
                j = s.find("'", i + 1)
                if j < 0:
                    self.error("unterminated '")
                parts.append(Lit(s[i + 1:j], "'"))
                self.i = j + 1
            elif c == '"':
                parts.extend(self.dquote())
            elif c == "\\":
                if s.startswith("\n", i + 1):
                    self.i += 2
                else:
                    parts.append(Lit(s[i + 1:i + 2], "\\"))
                    self.i += 2
            elif c == "$":
                if s.startswith('$"', i):
                    self.i += 1
                    parts.extend(self.dquote())
                else:
                    parts.append(self.dollar(None))
            elif c == "`":
                parts.append(self.backtick(None))
            elif c == "~" and i == start and not raw:
                m = re.compile(r"~[\w.+-]*").match(s, i)
                parts.append(Tilde(m.group()[1:]))
                self.i = m.end()
            else:
                m = plain.match(s, i)
                if m:
                    parts.append(Lit(m.group()))
                    self.i = m.end()
                else:
                    parts.append(Lit(c))
                    self.i += 1
        if self.i == start and not raw:
            self.error("expected a word")
        return Word(_merge(parts), s[start:self.i])

    def paren_in_word(self, parts, start):
        """`(` inside a word: array assignment, zsh `=(…)`, or a glob
        qualifier / extglob group. Consumes it and returns True, or False."""
        s, i = self.s, self.i
        last = parts[-1] if parts else None
        bare = type(last) is Lit and not last.quoted
        if len(parts) == 1 and bare and ASSIGN.fullmatch(last.text):
            self.i += 1
            words = []
            while True:
                self.linebreak()
                if s.startswith(")", self.i):
                    self.i += 1
                    break
                if self.i >= self.n:
                    self.error("unterminated array")
                words.append(self.word())
            parts.append(Array(words))
            return True
        if len(parts) == 1 and bare and last.text == "=":
            parts.pop()
            parts.append(self.subst("=(", i + 1, None))
            return True
        if bare and (last.text[-1] in "*?@+!]" or GLOB.search(last.text)):
            depth, j = 0, i
            while j < self.n:
                if s[j] == "(":
                    depth += 1
                elif s[j] == ")":
                    depth -= 1
                    if not depth:
                        break
                elif s[j] in "\n'\"":
                    self.error("bad glob group")
                j += 1
            parts.append(Lit(s[i:j + 1]))
            self.i = j + 1
            return True
        return False

    def dquote(self):
        s = self.s
        self.i += 1
        parts = []
        while True:
            if self.i >= self.n:
                self.error('unterminated "')
            c = s[self.i]
            if c == '"':
                self.i += 1
                return parts or [Lit("", '"')]
            if c == "\\":
                nx = s[self.i + 1:self.i + 2]
                if nx in ("$", "`", '"', "\\"):
                    parts.append(Lit(nx, '"'))
                    self.i += 2
                elif nx == "\n":
                    self.i += 2
                else:
                    parts.append(Lit("\\", '"'))
                    self.i += 1
            elif c == "$":
                parts.append(self.dollar('"'))
            elif c == "`":
                parts.append(self.backtick('"'))
            else:
                m = DQ_PLAIN.match(s, self.i)
                parts.append(Lit(m.group(), '"'))
                self.i = m.end()

    def dollar(self, quoted):
        s, i = self.s, self.i
        nx = s[i + 1:i + 2]
        if nx == "'" and quoted is None:
            return self.ansi_c()
        if s.startswith("$((", i):
            end = self.arith_end(i + 3)
            if end:
                self.i = end
                return Arith(s[i + 3:end - 2], quoted)
        if nx == "(":
            return self.subst("$(", i + 2, quoted)
        if nx == "{":
            return self.param_braced(quoted)
        m = NAME.match(s, i + 1)
        if m:
            self.i = m.end()
            return Param(m.group(), quoted=quoted)
        if nx and (nx.isdigit() or nx in "@*#?$!-"):
            self.i += 2
            return Param(nx, quoted=quoted)
        self.i += 1
        return Lit("$", quoted)

    def ansi_c(self):
        s, j, out = self.s, self.i + 2, []
        while True:
            if j >= self.n:
                self.error("unterminated $'")
            c = s[j]
            if c == "'":
                break
            if c == "\\" and j + 1 < self.n:
                out.append(ANSI.get(s[j + 1], "\\" + s[j + 1]))
                j += 2
            else:
                out.append(c)
                j += 1
        self.i = j + 1
        return Lit("".join(out), "$'")

    def subst(self, kind, body_start, quoted):
        self.i = body_start
        self.enter()
        body = self.script(frozenset(")"))
        self.nest -= 1
        if not self.s.startswith(")", self.i):
            self.error(f"unterminated {kind}")
        self.i += 1
        return Sub(kind, body, quoted)

    def backtick(self, quoted):
        s, j, buf = self.s, self.i + 1, []
        while True:
            if j >= self.n:
                self.error("unterminated `")
            c = s[j]
            if c == "\\" and j + 1 < self.n and s[j + 1] in "`\\$\"":
                buf.append(s[j + 1])
                j += 2
            elif c == "`":
                break
            else:
                buf.append(c)
                j += 1
        self.i = j + 1
        return Sub("`", Parser("".join(buf), self.nest + 1, self.zsh).parse(), quoted)

    def param_braced(self, quoted):
        s, start = self.s, self.i + 2
        j, depth = start, 1
        while j < self.n:
            c = s[j]
            if c == "\\":
                j += 2
                continue
            if c in "'\"" and (quoted is None or c == '"'):
                k = s.find(c, j + 1)
                if k < 0:
                    self.error("unterminated quote in ${")
                j = k + 1
                continue
            if c == "$" and s.startswith(("${", "$("), j):
                depth += 1
                j += 2
                continue
            if c == "(" and depth > 1:
                depth += 1
            elif c in "})":
                depth -= 1
                if not depth:
                    break
            j += 1
        if j >= self.n:
            self.error("unterminated ${")
        inner = s[start:j]
        self.i = j + 1
        m = PARAM.match(inner)
        if not m or inner.startswith("(") or (m.end() < len(inner) and not m.group("op")):
            return Param(None, "?", self._subword(inner), quoted)
        op = m.group("op")
        if m.group("pre"):
            op = m.group("pre") + (op or "")
        arg = self._subword(inner[m.end():]) if m.group("op") else None
        return Param(m.group("name"), op, arg, quoted)

    def _subword(self, text):
        if not text:
            return Word([], "")
        p = Parser(text, self.nest + 1, self.zsh)
        return p.word(raw=True)


def _merge(parts):
    """Join adjacent bare literals (the lexer splits on `(` handling)."""
    out = []
    for p in parts:
        if out and type(p) is Lit and type(out[-1]) is Lit and p.quoted == out[-1].quoted and not p.quoted:
            out[-1] = Lit(out[-1].text + p.text)
        else:
            out.append(p)
    return out


def split_assign(w):
    """(name, value Word) if w is `NAME=value` / `NAME+=value`, else None."""
    first = w.parts[0] if w.parts else None
    if type(first) is not Lit or first.quoted:
        return None
    m = ASSIGN.match(first.text)
    if not m:
        return None
    rest = first.text[m.end():]
    parts = ([Lit(rest)] if rest else []) + w.parts[1:]
    if parts and type(parts[0]) is Lit and not parts[0].quoted and parts[0].text.startswith("~"):
        t = re.match(r"~([\w.+-]*)(.*)", parts[0].text, re.S)
        parts[0:1] = [Tilde(t.group(1))] + ([Lit(t.group(2))] if t.group(2) else [])
    return m.group(1), Word(parts, w.src[len(first.text) - len(rest):] if rest or len(w.parts) > 1 else "")


def parse(s, zsh=False):
    return Parser(s, zsh=zsh).parse()


# ---------------------------------------------------------------- invocation

# wrapper -> (short options taking a value, long options taking a value,
#             positional arguments before the wrapped command)
WRAPPERS = {
    "sudo": ("ughpCDrtUT", {"--user", "--group", "--host", "--prompt", "--chdir", "--role", "--type", "--other-user", "--close-from"}, 0),
    "doas": ("uC", set(), 0),
    "env": ("uCS", {"--unset", "--chdir", "--split-string"}, 0),
    "command": ("", set(), 0),
    "builtin": ("", set(), 0),
    "exec": ("a", set(), 0),
    "nohup": ("", set(), 0),
    "noglob": ("", set(), 0),
    "nocorrect": ("", set(), 0),
    "time": ("fo", {"--format", "--output"}, 0),
    "nice": ("n", {"--adjustment"}, 0),
    "ionice": ("cnp", {"--class", "--classdata", "--pid"}, 0),
    "timeout": ("sk", {"--signal", "--kill-after"}, 1),
    "gtimeout": ("sk", {"--signal", "--kill-after"}, 1),
    "caffeinate": ("tw", set(), 0),
    "setsid": ("", set(), 0),
    "stdbuf": ("ioe", {"--input", "--output", "--error"}, 0),
    "taskset": ("", set(), 1),
    "chrt": ("", set(), 1),
    "numactl": ("CNmipP", {"--cpunodebind", "--membind", "--physcpubind", "--interleave", "--preferred"}, 0),
    "strace": ("eoOpPsSuEIabX", set(), 0),
    "unbuffer": ("", set(), 0),
    "xargs": ("IinLlsdEPa", {"--replace", "--max-args", "--max-lines", "--max-procs", "--delimiter", "--eof", "--arg-file", "--max-chars"}, 0),
}
# runner -> (subcommand, short options taking a value, long options taking a value)
RUNNERS = {
    "uv": ("run", "pw", {"--with", "--with-editable", "--with-requirements", "--python", "--project", "--directory",
                         "--package", "--group", "--extra", "--env-file", "--index", "--default-index",
                         "--index-url", "--extra-index-url", "--cache-dir", "--config-file", "--only-group",
                         "--no-group", "--python-preference", "--prerelease", "--resolution", "--find-links"}),
    "poetry": ("run", "C", {"--directory", "--project"}),
    "pdm": ("run", "p", {"--project"}),
    "rye": ("run", "", set()),
    "conda": ("run", "np", {"--name", "--prefix", "--cwd"}),
    "mamba": ("run", "np", {"--name", "--prefix", "--cwd"}),
    "micromamba": ("run", "np", {"--name", "--prefix", "--cwd"}),
    "pixi": ("run", "e", {"--environment", "--manifest-path"}),
    "mise": ("exec", "", {"--cd", "-C"}),
}
SHELLS = frozenset(("sh", "bash", "zsh", "dash", "ksh", "mksh", "fish"))


class Invocation:
    """What a simple command runs after stripping wrappers. name is the
    basename of the program (None if it isn't a literal, e.g. `$PY`); args
    are its argument Words; wrappers lists the stripped prefixes in order."""
    __slots__ = ("name", "path", "word", "args", "wrappers")

    def __init__(self, name, path, word, args, wrappers):
        self.name, self.path, self.word, self.args, self.wrappers = name, path, word, args, wrappers

    def values(self):
        """Argument texts, None for arguments with expansions."""
        return [a.value for a in self.args]

    def __repr__(self):
        return f"Invocation({self.name!r}, {[a.src for a in self.args]!r}, wrappers={self.wrappers!r})"


def skip_options(words, i, short_vals, long_vals):
    while i < len(words):
        v = words[i].value
        if v is None or not v.startswith("-") or v == "-":
            return i
        if v == "--":
            return i + 1
        if v.startswith("--"):
            i += 2 if "=" not in v and v in long_vals else 1
            continue
        took = False
        for j in range(1, len(v)):
            if v[j] in short_vals:
                took = j + 1 == len(v)
                break
        i += 2 if took else 1
    return i


def invocation(words):
    wrappers, i = [], 0
    while i < len(words):
        v = words[i].value
        if v is None:
            return Invocation(None, None, words[i], words[i + 1:], wrappers)
        base = os.path.basename(v)
        if base == "command" and i + 1 < len(words) and words[i + 1].value in ("-v", "-V"):
            return Invocation(base, v, words[i], words[i + 1:], wrappers)
        if base in WRAPPERS:
            short_vals, long_vals, npos = WRAPPERS[base]
            j = skip_options(words, i + 1, short_vals, long_vals)
            if base == "env":
                while j < len(words) and words[j].value is not None and ASSIGN.match(words[j].value):
                    j += 1
            j += npos
        elif base in RUNNERS and i + 1 < len(words) and words[i + 1].value == RUNNERS[base][0]:
            _, short_vals, long_vals = RUNNERS[base]
            j = skip_options(words, i + 2, short_vals, long_vals)
            if base == "uv" and j > i + 2 and words[j - 1].value in ("-m", "--module"):
                pass  # `uv run -m mod`: handled below as python -m
        else:
            return Invocation(base, v, words[i], words[i + 1:], wrappers)
        wrappers.append(base)
        if j >= len(words):
            return Invocation(None, None, None, [], wrappers)
        i = j
    return Invocation(None, None, None, [], wrappers)


def shell_c_arg(args):
    """The -c script Word of `sh [opts] -c script …`, else None."""
    for k, a in enumerate(args):
        v = a.value
        if v is None or not v.startswith("-") or v in ("-", "--"):
            return None
        if not v.startswith("--") and "c" in v[1:]:
            return args[k + 1] if k + 1 < len(args) else None
        if v in ("-o", "+o", "-O", "+O"):
            continue
    return None


SSH_VALUE_OPTS = "BbcDEeFIiJLlmOoPpQRSWw"


def ssh_remote(args):
    """(host, remote command Words) for `ssh [opts] host [command…]`."""
    j = skip_options(args, 0, SSH_VALUE_OPTS, set())
    if j >= len(args):
        return None, []
    return args[j].value, args[j + 1:]


# ---------------------------------------------------------------- flattening

class Ctx:
    """Where a command runs. loops: enclosing loop kinds ('while', 'until',
    'for', 'select'), outermost first; cond: in an if/while/until condition;
    bg: the '&'/'&!'/'&|' that backgrounds it; subshell: inside `( … )` or a
    substitution; pipe: (pipeline id, index, length) or None; nested: kinds
    of enclosing substitutions/string evals ('$(', '`', '<(', 'eval', 'sh -c',
    'sh <<', 'ssh', …); remote: ssh host or None; func: enclosing function
    definition name (runs only if called); negated: under `!`; zsh: parsed
    with zsh rules."""
    __slots__ = ("loops", "cond", "bg", "subshell", "pipe", "nested", "remote", "func", "negated", "zsh")

    def __init__(self, zsh=False):
        self.loops, self.nested = (), ()
        self.cond = self.subshell = self.negated = False
        self.zsh = zsh
        self.bg = self.pipe = self.remote = self.func = None

    def but(self, **kw):
        c = Ctx.__new__(Ctx)
        for k in Ctx.__slots__:
            setattr(c, k, kw[k] if k in kw else getattr(self, k))
        return c


class Cmd:
    """A simple command that would execute (or, with node a 'for'/'select'
    Compound, a loop header that assigns `node.var`)."""
    __slots__ = ("node", "ctx", "inv", "seq", "opaque")

    def __init__(self, node, ctx, inv, seq):
        self.node, self.ctx, self.inv, self.seq = node, ctx, inv, seq
        self.opaque = None  # why nested code (eval "$x", sh -c "$x") couldn't be inspected

    @property
    def assigns(self):
        return self.node.assigns if type(self.node) is Simple else []

    @property
    def redirs(self):
        return self.node.redirs

    def __repr__(self):
        return f"Cmd({self.inv!r})"


def commands(script_or_text, ctx=None, zsh=False):
    """Flatten a Script (or command text) into executed commands, in order.
    zsh: parse with zsh rules (the Bash tool runs the user's $SHELL)."""
    script = parse(script_or_text, zsh) if isinstance(script_or_text, str) else script_or_text
    out = []
    _walk(script, ctx or Ctx(zsh), out, 0)
    return out


BG_OPS = frozenset(("&", "&!", "&|"))


def _walk(script, ctx, out, depth):
    for ao, sep in script.items:
        c = ctx.but(bg=sep) if sep in BG_OPS else ctx
        for pl in ao.pipelines:
            n = len(pl.cmds)
            for k, node in enumerate(pl.cmds):
                cc = c
                if n > 1:
                    cc = cc.but(pipe=(id(pl), k, n))
                if pl.negated:
                    cc = cc.but(negated=True)
                _walk_node(node, cc, out, depth)


def _subs_of_words(words, ctx, out, depth):
    for w in words:
        for sub in w.subs():
            _walk(sub.script, ctx.but(nested=ctx.nested + (sub.kind,), subshell=True), out, depth)


def _walk_redirs(redirs, ctx, out, depth):
    _subs_of_words([r.target for r in redirs if not r.heredoc], ctx, out, depth)
    for r in redirs:
        if r.heredoc:
            for p in r.heredoc.parts:
                if type(p) is Sub:
                    _walk(p.script, ctx.but(nested=ctx.nested + (p.kind,), subshell=True), out, depth)
                elif type(p) is Param and p.arg is not None:
                    _subs_of_words([p.arg], ctx, out, depth)


def _walk_node(node, ctx, out, depth):
    if type(node) is Simple:
        _subs_of_words([v for _, v in node.assigns] + node.words, ctx, out, depth)
        _walk_redirs(node.redirs, ctx, out, depth)
        inv = invocation(node.words)
        cmd = Cmd(node, ctx, inv, len(out))
        out.append(cmd)
        _walk_inner(cmd, out, depth)
        return
    k = node.kind
    _walk_redirs(node.redirs, ctx, out, depth)
    if k == "(":
        _walk(node.body, ctx.but(subshell=True), out, depth)
    elif k == "{":
        _walk(node.body, ctx, out, depth)
    elif k in ("while", "until"):
        lc = ctx.but(loops=ctx.loops + (k,))
        _walk(node.cond, lc.but(cond=True), out, depth)
        _walk(node.body, lc, out, depth)
    elif k in ("for", "select"):
        _subs_of_words(node.words, ctx, out, depth)
        out.append(Cmd(node, ctx, None, len(out)))
        _walk(node.body, ctx.but(loops=ctx.loops + (k,)), out, depth)
    elif k == "if":
        for cond, body in node.clauses:
            _walk(cond, ctx.but(cond=True), out, depth)
            _walk(body, ctx, out, depth)
        if node.else_:
            _walk(node.else_, ctx, out, depth)
    elif k == "case":
        _subs_of_words(node.words, ctx, out, depth)
        for pats, body in node.items:
            _walk(body, ctx, out, depth)
    elif k == "func":
        _walk_node(node.body, ctx.but(func=node.name), out, depth)
    elif k == "[[":
        _subs_of_words(node.words, ctx, out, depth)


def _stdin_script(cmd):
    """Body of a heredoc/here-string on stdin, as text, or None."""
    for r in cmd.node.redirs:
        if r.fd not in (None, "0"):
            continue
        if r.heredoc is not None:
            return r.heredoc.body
        if r.op == "<<<":
            return r.target.value
    return None


def _walk_inner(cmd, out, depth):
    """Code a command runs from its arguments: eval, sh -c, ssh, shell <<EOF."""
    inv = cmd.inv
    name = inv.name
    if name is None:
        return
    text, kind, remote = None, None, cmd.ctx.remote
    if name == "eval":
        vals = inv.values()
        if None in vals:
            cmd.opaque = "eval of an expansion"
            return
        text, kind = " ".join(vals), "eval"
    elif name in SHELLS:
        w = shell_c_arg(inv.args)
        if w is not None:
            text, kind = w.value, "sh -c"
            if text is None:
                cmd.opaque = f"{name} -c of an expansion"
                return
        elif not any(a.value is None or not a.value.startswith("-") for a in inv.args):
            text, kind = _stdin_script(cmd), "sh <<"
    elif name == "ssh":
        host, rest = ssh_remote(inv.args)
        if host is None:
            return
        remote = host
        if rest:
            vals = [a.value for a in rest]
            if None in vals:
                cmd.opaque = "ssh command with local expansions"
                return
            text, kind = " ".join(vals), "ssh"
        else:
            text, kind = _stdin_script(cmd), "ssh <<"
    if text is None:
        return
    if depth >= MAX_STRINGS:
        raise ParseError("too many nested command strings")
    # eval keeps the current shell's rules; a named shell uses its own; ssh
    # runs the remote login shell, assumed POSIX-like.
    zsh = cmd.ctx.zsh if kind == "eval" else name == "zsh"
    inner = Parser(text, zsh=zsh).parse()
    ctx = cmd.ctx.but(nested=cmd.ctx.nested + (kind,), remote=remote, zsh=zsh,
                      subshell=cmd.ctx.subshell or kind != "eval")
    _walk(inner, ctx, out, depth + 1)


class View:
    """One tool call's command, parsed at most once and shared by guards.
    commands() is None when the text can't be parsed (error says why)."""
    __slots__ = ("tool", "text", "zsh", "error", "_cmds")

    def __init__(self, tool, text, zsh=None):
        if zsh is None:
            zsh = os.path.basename(os.environ.get("SHELL", "")) == "zsh"
        self.tool, self.text, self.zsh, self.error, self._cmds = tool, text, zsh, None, False

    def commands(self):
        if self._cmds is False:
            try:
                self._cmds = commands(self.text, zsh=self.zsh)
            except (ParseError, RecursionError) as e:
                self._cmds, self.error = None, str(e)
        return self._cmds
