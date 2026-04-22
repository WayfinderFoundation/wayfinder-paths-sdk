# Conditional Router Reference

Reference path for the conditional-router strategy pipeline. This is the canonical in-repo example for policy-driven orchestration, null-state gating, and Claude/OpenCode exports.

## Why this exists

This path is the in-repo gold reference for compiled strategy pipelines. It shows the canonical authoring shape, fixed artifact contract, fixture-driven evals, and host-specific renders for Claude and OpenCode.

## Core files

- `wfpath.yaml` defines the manifest, pipeline metadata, inputs, agents, and host targets.
- `policy/default.yaml` holds the strategy policy and risk gates as data.
- `pipeline/graph.yaml` defines the ordered workflow graph and failure edges.
- `scripts/main.py` is the local reference component for the path.
- `skill/instructions.md`, `skill/references/`, and `skill/agents/` define the canonical skill layer.
- `tests/fixtures/` and `tests/evals/` define output-shape, null-state, risk-gate, and host-render checks.

## Workflow shape

1. intake and normalize user intent
2. gather signals and supporting research
3. generate candidate expressions
4. rank against a mandatory null state
5. apply risk and execution gates
6. compile the job or degrade to draft/null
7. emit the standard response envelope

## Develop

```bash
poetry run wayfinder path doctor --path .
poetry run wayfinder path eval --path .
poetry run wayfinder path render-skill --path .
poetry run wayfinder path build --path . --out dist/bundle.zip
```
