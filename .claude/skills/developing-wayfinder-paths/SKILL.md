---
name: developing-wayfinder-paths
description: Create, build, and publish Wayfinder Paths, including compiled strategy-pipeline paths with Claude and OpenCode skill exports.
metadata:
  tags: paths, signals, wfpath, publishing, wayfinder, pipelines, claude, opencode
---

## When to use

Use this skill when you are:
- Creating a new Path to share a strategy, monitor, script, skill, policy, tool or contract
- Creating a compiled strategy-pipeline path with fixed phases, artifacts, and evals
- Publishing a new path version
- Emitting signals/receipts to make a path page feel “alive”
- Defining canonical path skill source in `wfpath.yaml`, `policy/default.yaml`, `pipeline/graph.yaml`, `skill/instructions.md`, and `skill/agents/`
- Generating host-specific skill exports for Claude Code and OpenCode, with Codex/OpenClaw/portable thin exports as secondary targets

## How to use

- To scaffold a normal path, start with `poetry run wayfinder path init <slug> --dir <base-dir> --kind <kind>`. Pick the kind from what the path ships ([rules/manifest.md](rules/manifest.md#primary_kind)): `strategy` only for a working `Strategy` subclass that trades; signals, scores and research are `monitor` or `tool`.
- To scaffold a compiled strategy pipeline, use `poetry run wayfinder path init <slug> --dir <base-dir> --template pipeline --archetype <conditional-router|hedge-finder|spread-radar>`.
- `path init` uses `--dir`, while the later path commands (`fmt`, `doctor`, `render-skill`, `build`, `publish`) use `--path`.
- For pipeline paths, run `poetry run wayfinder path eval --path .` before publishing or exporting skills.
- [rules/manifest.md](rules/manifest.md) - `wfpath.yaml` schema + required fields for MVP
- [rules/applet.md](rules/applet.md) - Applets are retired: don't add one
- [rules/build-and-publish.md](rules/build-and-publish.md) - `wayfinder path build/publish` + required config/env vars
- [rules/signals.md](rules/signals.md) - `wayfinder path signal emit` patterns
- [rules/security.md](rules/security.md) - MVP hosting constraints and safe defaults
- [rules/path-skills.md](rules/path-skills.md) - Defining the skill layer, agent layer, and host-specific exports
