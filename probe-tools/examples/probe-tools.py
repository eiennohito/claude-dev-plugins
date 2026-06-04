"""Example per-project tool probe — copy to <your-repo>/.claude/probe-tools.py

This file is imported by the probe-tools plugin at session start. Define any of
TOOLS / PKG_MANAGERS / SKIP / probe(api). All are optional; omit what you don't
need. Top-level code runs in the hook process, so keep it cheap and side-effect
free.
"""

# Extra tools to detect, on top of the built-in modern-CLI set. A cmd matching a
# built-in name overrides that built-in's description. `version` is optional and
# defaults to ("--version",).
TOOLS = [
    {"cmd": "terraform", "desc": "Terraform — IaC; plan/apply infra changes here"},
    {"cmd": "kubectl", "desc": "kubectl — cluster ops (context already set)"},
    {"cmd": "just", "desc": "just — task runner; see ./justfile for tasks", "version": ["--version"]},
    ("docker", "docker — containers; compose file at ./docker-compose.yml"),
]

# Extra package managers to probe (in addition to the defaults).
PKG_MANAGERS = ["poetry", "pixi"]

# Built-in tools to suppress even if installed (e.g. you don't want it suggested).
SKIP = ["xsv"]


def probe(api):
    """Return custom probe result(s). `api` exposes which(cmd),
    run(args, timeout=2, cwd=None), version(cmd, args), platform (dict), root.

    May return: None/"" | a markdown str | a {"title","body"} section dict |
    a list mixing those (several sections).
    """
    out = []

    # Inject every justfile recipe as its own section, so the agent runs
    # `just <target>` instead of rediscovering the task runner.
    if api.which("just"):
        # `just --summary` prints recipe names space-separated; run it from the
        # project root so it finds ./justfile regardless of the hook's cwd.
        summary = api.run(["just", "--summary"], cwd=api.root, timeout=3)
        if summary:
            body = "\n".join(f"- `just {t}`" for t in summary.split())
            out.append({"title": "just targets", "body": body})

    # Plain-string notes still work (rendered under a "## Project" heading).
    notes = []
    if api.which("docker") and api.which("docker-compose") is None:
        notes.append("- Use `docker compose` (v2 plugin), not `docker-compose`.")
    if api.platform["name"] == "Linux":
        notes.append("- CI runs on Linux; this dev box matches it.")
    if notes:
        out.append("\n".join(notes))

    return out
