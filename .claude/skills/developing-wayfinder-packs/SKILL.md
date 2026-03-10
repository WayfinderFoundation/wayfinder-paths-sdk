---
name: developing-wayfinder-packs
description: Create, build, and publish Wayfinder Packs (wfpack.yaml + optional static applet + signals).
metadata:
  tags: packs, applets, signals, wfpack, publishing, wayfinder
---

## When to use

Use this skill when you are:
- Creating a new Pack (bundle) to share a strategy/script/skill/contract UI
- Adding a static applet UI to a pack
- Publishing a new pack version
- Emitting signals/receipts to make a pack page feel “alive”
- Writing a skill/SKILL.md to make your pack usable by AI agents (Claude Code, OpenClaw, Codex)

## How to use

- [rules/manifest.md](rules/manifest.md) - `wfpack.yaml` schema + required fields for MVP
- [rules/applet.md](rules/applet.md) - Static applet requirements + build output expectations
- [rules/build-and-publish.md](rules/build-and-publish.md) - `wayfinder pack build/publish` + required config/env vars
- [rules/signals.md](rules/signals.md) - `wayfinder pack signal emit` patterns
- [rules/security.md](rules/security.md) - MVP hosting constraints and safe defaults
- [rules/pack-skills.md](rules/pack-skills.md) - Writing skill/SKILL.md so AI agents can understand and operate your pack

