---
name: developing-wayfinder-strategies
description: Best practices for developing, testing, and iterating Wayfinder Paths strategies/adapters in this repo (data sources, manifests, safety rails).
metadata:
  tags: wayfinder, defi, strategies, adapters, python
---

## When to use

Use this skill when you are:
- Designing a new strategy (or major refactor) in `wayfinder_paths/strategies/`
- Exploring adapter capabilities to build strategy logic
- Adding tests, manifests, examples, or debugging strategy behavior

## How to use

Follow the repo-specific workflow and patterns in these rule docs:

- [rules/workflow.md](rules/workflow.md) - Setup, common commands, how to run strategies locally
- [rules/manifests-and-tests.md](rules/manifests-and-tests.md) - Manifest rules, required tests, `examples.json` discipline
- [rules/data-sources.md](rules/data-sources.md) - Where data comes from (clients/adapters), read vs write conventions
- [rules/reference-strategies.md](rules/reference-strategies.md) - **Canonical reference strategies to copy/adapt from** (perps, etc.)

When designing a new strategy, **start from the canonical reference for that style** — see [rules/reference-strategies.md](rules/reference-strategies.md). It shows file layout, signal/decide separation, snapshot conventions, and reconcile-friendly patterns the SDK expects.

## On Shells Instance -- IMPORTANT

Only `.wayfinder_runs/` persists across restarts, you can use `just create-strategy` — but this writes to `wayfinder_paths/strategies/` which won't survive. Move the strategy files under `$WAYFINDER_RUNS_DIR/strategies/<name>/`:


`core_run_strategy`, `core_get_adapters_and_strategies`, and `python -m wayfinder_paths.run_strategy <name>` all auto-resolve runs-dir strategies via the shared loader — no invocation changes.

