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
- [rules/safety-and-execution.md](rules/safety-and-execution.md) - Approvals and safe execution patterns
- [rules/reference-strategies.md](rules/reference-strategies.md) - **Canonical reference strategies to copy/adapt from** (perps, etc.)

## Reference strategies (start here when building new ones)

When designing a new strategy, **read the canonical reference for that style first** — it shows the file layout, signal/decide separation, snapshot conventions, and reconcile-friendly patterns the SDK expects.

| Strategy style | Canonical reference | Why |
|---|---|---|
| **Perp (HL, ActivePerps)** | [`wayfinder_paths/strategies/apex_gmx_velocity/`](../../../wayfinder_paths/strategies/apex_gmx_velocity/) | Single-pair velocity-z-score on HL perps. Clean `signal.py`/`decide.py` split, schema-compliant `backtest_ref.json` with realistic 25 bps slippage, parity-validated against the original audit code. The smallest correct example of the perp pattern. |

If a new perp strategy starts to deviate from the apex_gmx_velocity layout (file roles, ref schema, signal-returns-SignalFrame contract), that's a smell — either the reference is wrong, or the new strategy is. Check before diverging.

