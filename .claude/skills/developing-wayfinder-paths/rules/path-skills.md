# Path Skills

Wayfinder paths now support two connected authoring layers:

- the **skill layer**: instructions, references, scripts, runtime metadata
- the **agent layer**: named worker roles that a host-specific orchestrator can invoke

Keep both layers host-neutral in the source path. Host adapters compile them into Claude-native or OpenCode-native layouts.

## Canonical source

For standard paths, the source of truth is still:

- `wfpath.yaml`
- `skill/instructions.md`
- optional `skill/scripts/`
- optional `skill/references/`
- optional `skill/assets/`

For compiled strategy-pipeline paths, the canonical authoring set is:

- `wfpath.yaml`
- `policy/default.yaml`
- `pipeline/graph.yaml`
- `skill/instructions.md`
- `skill/references/`
- `skill/scripts/`
- `skill/agents/`
- `inputs/`
- `schemas/`
- `tests/fixtures/`
- `tests/evals/`

Pipeline logic belongs in `policy/default.yaml` and `pipeline/graph.yaml`, not in a long freeform `instructions.md`.

## Manifest model

Use these first-class blocks in `wfpath.yaml` for pipeline paths:

```yaml
pipeline:
  archetype: "conditional-router"
  graph: "pipeline/graph.yaml"
  artifacts_dir: ".wf-artifacts"
  entry_command: "conditional-bet"
  primary_hosts:
    - claude
    - opencode
  output_contract:
    - signal_snapshot
    - selected_playbook
    - candidate_expressions
    - null_state
    - risk_checks
    - job
    - next_invalidation

inputs:
  slots:
    thesis:
      type: "markdown"
      path: "inputs/thesis.md"
      schema: "schemas/thesis.schema.json"
      required: true

agents:
  - id: "thesis-normalizer"
    phase: "normalize_thesis"
    description: "Normalize rough thesis text into structured thresholds."
    tools: ["read", "glob", "grep", "bash"]
    output: ".wf-artifacts/$RUN_ID/normalize_thesis.json"
    host_mode: "worker"

host:
  claude:
    rules_file: ".claude/CLAUDE.md"
    skill_dir: ".claude/skills"
    agent_dir: ".claude/agents"
    settings_file: ".claude/settings.json"
  opencode:
    rules_file: "AGENTS.md"
    config_file: "opencode.json"
    skill_dir: ".opencode/skills"
    agent_dir: ".opencode/agents"
    command_dir: ".opencode/commands"
    plugin_dir: ".opencode/plugins"
    tool_dir: ".opencode/tools"
```

## Skill layer

`skill/instructions.md` should stay short. It should explain:

- when the path should trigger
- which references to read first
- the fixed phase order
- which worker agents to use
- the required output contract

Move methodology into `skill/references/` and deterministic logic into `skill/scripts/`.

## Agent layer

Each file in `skill/agents/` defines one host-neutral worker role. Keep them leaf-scoped:

- one phase only
- one artifact only
- no subagent spawning
- no final synthesis

The orchestrator owns fan-out, synthesis, null-state ranking, and final output assembly.

## Generated exports

Run:

```bash
poetry run wayfinder path render-skill --path .
```

This generates host-specific artifacts under `.build/skills/`.

Primary orchestration targets:

- `claude/<skill-name>/SKILL.md`
- `claude/<skill-name>/install/.claude/skills/<skill-name>/...`
- `claude/<skill-name>/install/.claude/agents/<skill-name>-*.md`
- `claude/<skill-name>/install/.claude/CLAUDE.md`
- `claude/<skill-name>/install/.claude/settings.json`
- `opencode/<skill-name>/SKILL.md`
- `opencode/<skill-name>/install/.opencode/skills/<skill-name>/...`
- `opencode/<skill-name>/install/.opencode/agents/<skill-name>-*.md`
- `opencode/<skill-name>/install/.opencode/commands/*.md`
- `opencode/<skill-name>/install/.opencode/plugins/*.ts`
- `opencode/<skill-name>/install/.opencode/tools/*.ts`
- `opencode/<skill-name>/install/AGENTS.md`
- `opencode/<skill-name>/install/opencode.json`

Secondary thin exports remain available for:

- `codex/<skill-name>/...`
- `openclaw/<skill-name>/...`
- `portable/<skill-name>/...`

Optional `skill/scripts`, `skill/references`, and `skill/assets` folders are copied into each host export.

## Validation

`poetry run wayfinder path doctor --check --path .` enforces the shared contract:

- valid `skill` block
- valid registered `pipeline.archetype`
- existing `policy/default.yaml`
- existing `pipeline/graph.yaml`
- required policy sections for the chosen archetype
- schema-backed input slots
- unique one-artifact-per-agent outputs under `artifacts_dir`
- at least three fixtures and three evals for pipeline paths

For pipeline paths, also run:

```bash
poetry run wayfinder path eval --path .
```

That checks fixture output shape, null-state behavior, risk-gate behavior, and host-render coverage.

## Provided mode

`skill.source: provided` is still allowed for power users, but it is the escape hatch, not the default. Use it only when you need hand-authored host-specific skill sources.

If you use provided mode:

- maintain `skill/SKILL.md` directly
- keep pipeline logic in source files, not only in markdown
- expect `doctor` to validate the provided skill source instead of generating one
