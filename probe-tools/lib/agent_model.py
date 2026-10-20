"""Deny Agent tool calls that don't set `model` explicitly.

Checked for the Agent tool ("Task" is its older name). Without an explicit model a subagent inherits the session's
model, which is usually the most expensive one; forcing the choice makes the
agent pick a cheaper model for routine delegation. Exempt:

- forks: they always run on the parent model and ignore `model`;
- agent types whose definition sets a concrete `model:` in its frontmatter
  (not `inherit`), looked up in the project's .claude/agents/, ~/.claude/agents/,
  and installed plugins' agents/ (for `plugin:name` types). A definition that
  can't be found counts as not setting one.
"""
import json
import os

REASON = (
    "specify the model explicitly. sonnet for most "
    "tasks, opus if sonnet fails to do what you expect from it, fable for "
    "complex plan validation when the user explicitly requests it (or discuss "
    "it with the user beforehand if you think that it is required)."
)


def frontmatter(path):
    """The `key: value` pairs of a markdown file's YAML frontmatter."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    out = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and not key.startswith((" ", "\t")):
            out[key.strip()] = value.strip().strip("'\"")
    return out


def plugin_agent_dirs(plugin):
    path = os.path.expanduser("~/.claude/plugins/installed_plugins.json")
    try:
        with open(path, encoding="utf-8") as f:
            plugins = json.load(f).get("plugins") or {}
    except (OSError, ValueError, AttributeError):
        return []
    dirs = []
    for key, installs in plugins.items():
        if key.split("@", 1)[0] == plugin:
            for inst in installs or []:
                if inst.get("installPath"):
                    dirs.append(os.path.join(inst["installPath"], "agents"))
    return dirs


def defined_model(agent_type, cwd):
    """The model set by agent_type's definition, or None."""
    plugin, _, name = agent_type.rpartition(":")
    if plugin:
        dirs = plugin_agent_dirs(plugin)
    else:
        root = os.environ.get("CLAUDE_PROJECT_DIR") or cwd or os.getcwd()
        dirs = [os.path.join(root, ".claude", "agents"),
                os.path.expanduser("~/.claude/agents")]
    for d in dirs:
        try:
            files = sorted(os.listdir(d))
        except OSError:
            continue
        for fname in files:
            if not fname.endswith(".md"):
                continue
            meta = frontmatter(os.path.join(d, fname))
            if meta.get("name", fname[:-3]) == name:
                model = meta.get("model", "")
                return model if model and model != "inherit" else None
    return None


def check(data):
    """Deny reason for an Agent tool call (hook input), or None."""
    tool_input = data.get("tool_input") or {}
    agent_type = str(tool_input.get("subagent_type") or "")
    if agent_type == "fork" or str(tool_input.get("model") or "").strip():
        return None
    if agent_type and defined_model(agent_type, data.get("cwd")):
        return None
    return REASON
