# claude-dev-plugins

A Claude Code **plugin marketplace** for development workflows. The marketplace
manifest is [`.claude-plugin/marketplace.json`](.claude-plugin/marketplace.json);
each plugin lives in its own subdirectory.

## Plugins

| Plugin                            | What it does                                                                                                                            |
| --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| [`precheck`](precheck/README.md)       | Parallel pre-commit code review — spawns independent subagents to review the current diff across dimensions, then synthesizes a ranked report. |
| [`probe-tools`](probe-tools/README.md) | Detects installed tools at session start and injects a capability summary into context, so the agent reaches for the fast/available tool. |

## Install

Add the marketplace, then install the plugins you want:

```sh
# Directly from GitHub (owner/repo):
/plugin marketplace add eiennohito/claude-dev-plugins

# Or from a local clone of this repo:
/plugin marketplace add /path/to/claude-dev-plugins
```

```sh
/plugin install precheck@claude-dev-plugins
/plugin install probe-tools@claude-dev-plugins
```

Update the listing later with `/plugin marketplace update claude-dev-plugins`.

### Local development

While iterating on a single plugin you can load it directly without the
marketplace:

```sh
claude --plugin-dir /path/to/claude-dev-plugins/probe-tools
```

Hot-reload edits with `/reload-plugins`.

---

Development notes and conventions for building these plugins live in
[`CLAUDE.md`](CLAUDE.md).
