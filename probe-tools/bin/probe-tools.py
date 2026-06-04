#!/usr/bin/env python3
"""SessionStart hook — probe the machine for available tools and inject a concise
capability summary into context.

Registered (hooks/hooks.json) for SessionStart matcher "startup|resume|compact".
It detects which modern CLI tools and package managers are actually installed,
plus basic platform facts, and returns them as `additionalContext` so the agent
reaches for the fast/available tool (rg over grep -r, fd over find, gh, etc.)
instead of guessing or spending tool calls discovering what exists. Re-runs on
compact because the summary is lost when context is summarized.

Per-project extension (plugin-like): if ${CLAUDE_PROJECT_DIR}/.claude/probe-tools.py
exists it is imported and may define any of:

    TOOLS        list of extra tools to probe. Each item is a dict
                 {"cmd": str, "desc": str, "version": [args]?} or a
                 ("cmd", "desc") tuple. A cmd that matches a default's name
                 overrides that default's description.
    PKG_MANAGERS list of extra package-manager commands to probe (str names).
    SKIP         list of default cmd names to suppress (don't report).
    probe(api)   optional function returning custom probe results to inject. May
                 return any of:
                   - None / "" — nothing,
                   - a markdown string — appended under a "## Project" heading,
                   - a {"title": str, "body": str} dict — its own "## <title>"
                     section (e.g. {"title": "just targets", "body": "- ..."}),
                   - a list mixing the above — several sections.
                 `api` exposes .which(cmd), .run(args, timeout=2, cwd=None),
                 .version(cmd, args=("--version",)), .platform (a dict) and
                 .root (the project dir).

The project file is the user's own repo code and runs in this process — same
trust model as .claude/hooks/. It is sandboxed in try/except: any error degrades
to the built-in probe plus a short warning, never a hard failure.

Pure stdlib (no jq). JSON is emitted with ensure_ascii=False so arrows/em-dashes
survive intact.
"""
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys

# Default modern-CLI tools worth preferring when present. (cmd, description)
DEFAULT_TOOLS = [
    ("rg", "ripgrep — use instead of grep -r (faster, respects .gitignore)"),
    ("fd", "fd — use instead of find (faster, respects .gitignore)"),
    ("jq", "jq — JSON processor"),
    ("yq", "yq — YAML processor"),
    ("bat", "bat — use instead of cat for syntax-highlighted output"),
    ("eza", "eza — modern ls replacement"),
    ("delta", "delta — better git diff viewer"),
    ("fzf", "fzf — fuzzy finder"),
    ("hyperfine", "hyperfine — benchmarking tool"),
    ("tokei", "tokei — code statistics (faster than cloc)"),
    ("dust", "dust — disk usage (better du)"),
    ("sd", "sd — use instead of sed for simple replacements"),
    ("xsv", "xsv — fast CSV toolkit"),
    ("gh", "gh — GitHub CLI"),
    ("watchexec", "watchexec — file watcher"),
]

DEFAULT_PKG_MANAGERS = ["brew", "npm", "pnpm", "yarn", "bun", "pip", "uv", "cargo", "go"]


def which(cmd):
    return shutil.which(cmd)


def run(args, timeout=2, cwd=None):
    """Run args, returning the first line of stdout (stripped) or None."""
    try:
        out = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False, cwd=cwd
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    line = out.strip().splitlines()[0] if out.strip() else ""
    return line or None


def version(cmd, args=("--version",)):
    if not which(cmd):
        return None
    return run([cmd, *args]) or "installed"


def _platform_info():
    system = platform.system()
    if system == "Darwin":
        release = platform.mac_ver()[0] or platform.release()
        name = "macOS"
    elif system == "Linux":
        name, release = "Linux", platform.release()
    else:
        name, release = system, platform.release()
    return {
        "name": name,
        "release": release,
        "arch": platform.machine(),
        "shell": os.environ.get("SHELL", "unknown"),
    }


class _Api:
    """Helper surface passed to a project probe() function."""

    def __init__(self, plat, root):
        self.platform = plat
        self.root = root

    which = staticmethod(which)
    run = staticmethod(run)
    version = staticmethod(version)


def _normalize_sections(result):
    """A probe() return -> list of (title|None, body) sections.

    Accepts None/"" (drop), a markdown str (untitled), a {"title","body"} dict,
    or a list mixing those. Empty bodies are dropped.
    """
    if not result:
        return []
    items = result if isinstance(result, (list, tuple)) else [result]
    sections = []
    for item in items:
        if not item:
            continue
        if isinstance(item, dict):
            body = str(item.get("body", "")).rstrip("\n")
            title = item.get("title")
            title = str(title) if title else None
        else:
            body, title = str(item).rstrip("\n"), None
        if body:
            sections.append((title, body))
    return sections


def _normalize_tool(item):
    """Accept a dict {cmd, desc, version?} or a (cmd, desc) tuple -> dict."""
    if isinstance(item, dict):
        cmd = item.get("cmd")
        if not cmd:
            return None
        return {
            "cmd": cmd,
            "desc": item.get("desc", cmd),
            "version": item.get("version", ("--version",)),
        }
    try:
        cmd, desc = item
    except (TypeError, ValueError):
        return None
    return {"cmd": cmd, "desc": desc, "version": ("--version",)}


def _load_project_config(root):
    """Import ${root}/.claude/probe-tools.py if present. Returns (module, error)."""
    path = os.path.join(root, ".claude", "probe-tools.py")
    if not os.path.isfile(path):
        return None, None
    try:
        spec = importlib.util.spec_from_file_location("_probe_tools_project", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, None
    except Exception as exc:  # noqa: BLE001 - never hard-fail the session
        return None, f"{type(exc).__name__}: {exc}"


def _collect_tool_specs(project):
    """Merge default + project TOOLS/SKIP into an ordered, deduped spec list."""
    specs = [_normalize_tool(t) for t in DEFAULT_TOOLS]
    specs = [s for s in specs if s]

    skip = set()
    extra = []
    if project is not None:
        skip = {str(c) for c in getattr(project, "SKIP", []) or []}
        for item in getattr(project, "TOOLS", []) or []:
            spec = _normalize_tool(item)
            if spec:
                extra.append(spec)

    by_cmd = {}
    order = []
    for spec in specs + extra:
        cmd = spec["cmd"]
        if cmd in skip:
            continue
        if cmd not in by_cmd:
            order.append(cmd)
        by_cmd[cmd] = spec  # later (project) wins on description
    return [by_cmd[c] for c in order]


def _build(root):
    """Probe everything and return a report dict (shared by hook + diagnose)."""
    plat = _platform_info()
    project, proj_error = _load_project_config(root)
    config_path = os.path.join(root, ".claude", "probe-tools.py")

    # --- probe tools ---
    tool_specs = _collect_tool_specs(project)
    available = []  # (cmd, desc, version)
    for spec in tool_specs:
        if which(spec["cmd"]):
            ver = run([spec["cmd"], *spec["version"]]) or "installed"
            available.append((spec["cmd"], spec["desc"], ver))

    # --- probe package managers ---
    pms = list(DEFAULT_PKG_MANAGERS)
    if project is not None:
        for pm in getattr(project, "PKG_MANAGERS", []) or []:
            if pm not in pms:
                pms.append(pm)
    pms_found = [(pm, version(pm) or "installed") for pm in pms if which(pm)]

    # --- optional custom probe ---
    sections = []
    probe_fn = getattr(project, "probe", None) if project is not None else None
    if callable(probe_fn):
        try:
            sections = _normalize_sections(probe_fn(_Api(plat, root)))
        except Exception as exc:  # noqa: BLE001
            proj_error = proj_error or f"probe() raised {type(exc).__name__}: {exc}"

    return {
        "plat": plat,
        "available": available,
        "pms_found": pms_found,
        "sections": sections,
        "proj_error": proj_error,
        "config_path": config_path,
        "config_exists": os.path.isfile(config_path),
        "has_probe": callable(probe_fn),
    }


def _render_context(report):
    plat, available, pms_found, sections = (
        report["plat"],
        report["available"],
        report["pms_found"],
        report["sections"],
    )
    lines = [
        "## Environment",
        "",
        f"- OS: {plat['name']} {plat['release']} ({plat['arch']})",
        f"- Shell: {plat['shell']}",
        "",
        "## Available tools",
        "",
    ]
    if available:
        lines.append("Prefer these over slower alternatives when available:")
        lines.append("")
        for cmd, desc, ver in available:
            lines.append(f"- {desc} ({ver})")
    else:
        lines.append("(none of the probed modern-CLI tools were found)")
    lines.append("")
    lines.append("## Package managers")
    lines.append("")
    if pms_found:
        for pm, ver in pms_found:
            lines.append(f"- {pm} ({ver})")
    else:
        lines.append("(none detected)")
    for title, body in sections:
        lines.append("")
        lines.append(f"## {title or 'Project'}")
        lines.append("")
        lines.append(body)
    return "\n".join(lines)


def _render_message(report):
    plat, available = report["plat"], report["available"]
    names = [c for c, _, _ in available]
    msg = (
        f"Environment detected: {plat['name']} {plat['release']} ({plat['arch']}) | "
        f"{len(names)} tool(s) available"
    )
    if names:
        msg += ": " + ", ".join(names)
    if report["proj_error"]:
        msg += f" | .claude/probe-tools.py error — {report['proj_error']}"
    return msg


def main():
    root = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    report = _build(root)
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": _render_context(report),
            "systemMessage": _render_message(report),
        }
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


def diagnose():
    """Human-readable state for /probe-tools-configure (not the hook envelope)."""
    root = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    report = _build(root)
    out = []
    out.append(f"PROJECT_DIR={root}")
    out.append(f"CONFIG_PATH={report['config_path']}")
    out.append(f"CONFIG_EXISTS={'yes' if report['config_exists'] else 'no'}")
    out.append(f"CONFIG_HAS_PROBE_FN={'yes' if report['has_probe'] else 'no'}")
    out.append(f"CONFIG_ERROR={report['proj_error'] or 'none'}")
    out.append("")
    out.append("DETECTED_TOOLS:")
    if report["available"]:
        for cmd, _desc, ver in report["available"]:
            out.append(f"  - {cmd} ({ver})")
    else:
        out.append("  (none)")
    out.append("DETECTED_PACKAGE_MANAGERS:")
    if report["pms_found"]:
        for pm, ver in report["pms_found"]:
            out.append(f"  - {pm} ({ver})")
    else:
        out.append("  (none)")
    out.append("CUSTOM_PROBE_SECTIONS:")
    if report["sections"]:
        for title, _body in report["sections"]:
            out.append(f"  - {title or 'Project'}")
    else:
        out.append("  (none)")
    out.append("")
    out.append("--- context that would be injected ---")
    out.append(_render_context(report))
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    if "--diagnose" in sys.argv[1:]:
        diagnose()
    else:
        main()
