---
name: developing-wayfinder-paths
description: Create, build, and publish Wayfinder Paths (wfpath.yaml + optional static applet + signals).
metadata:
  tags: paths, applets, signals, wfpath, publishing, wayfinder
---

## When to use

Use this skill when you are:
- Creating a new Path (bundle) to share a strategy/script/skill/contract UI
- Adding a static applet UI to a path
- Publishing a new path version
- Emitting signals/receipts to make a path page feel “alive”
- Defining canonical path skill source in `wfpath.yaml` + `skill/instructions.md`
- Generating host-specific skill exports for Claude Code, Codex, OpenClaw, and portable use
- Adding live data fetching or interactive controls to a path applet

## How to use

- To scaffold a new path, start with `poetry run wayfinder path init <slug> --dir <base-dir>`.
- `path init` uses `--dir`, while the later path commands (`fmt`, `doctor`, `render-skill`, `build`, `publish`) use `--path`.
- [rules/manifest.md](rules/manifest.md) - `wfpath.yaml` schema + required fields for MVP
- [rules/applet.md](rules/applet.md) - Static applet requirements + build output expectations
- [rules/build-and-publish.md](rules/build-and-publish.md) - `wayfinder path build/publish` + required config/env vars
- [rules/signals.md](rules/signals.md) - `wayfinder path signal emit` patterns
- [rules/security.md](rules/security.md) - MVP hosting constraints and safe defaults
- [rules/path-skills.md](rules/path-skills.md) - Defining canonical skill source and generating host-specific exports
