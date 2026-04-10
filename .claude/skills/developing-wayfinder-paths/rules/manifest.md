# `wfpath.yaml` (Path Manifest)

`wfpath.yaml` is the source of truth for publishing a Wayfinder Path bundle.

## Required core fields

- `slug`
- `name`
- `version`

Recommended top-level metadata:

- `schema_version`
- `summary`
- `primary_kind`
- `tags`
- `components`

Minimal example:

```yaml
schema_version: "0.1"

slug: "basis-board"
name: "Basis Board"
version: "0.1.0"
summary: "Monitor basis spreads and publish a live signal feed."

primary_kind: "monitor"
tags:
  - basis
  - hyperliquid
  - monitor
```

## Skill block

If the path should export skill artifacts, add a `skill` block.

Generated mode is the default:

```yaml
skill:
  enabled: true
  source: generated
  name: "basis-board"
  description: "Inspect, validate, and operate the Basis Board path."
  instructions: "skill/instructions.md"
  runtime:
    mode: thin
    package: "wayfinder-paths"
    version: "0.8.0"
    python: ">=3.12,<3.13"
    component: "main"
```

Rules:

- `enabled: true` opts the path into skill validation and rendering.
- `source` must be `generated` or `provided`.
- `name` must be lowercase letters, numbers, and hyphens.
- `description` must be non-empty.
- `instructions` is required for `source: generated`.

## Pipeline block

Compiled strategy-pipeline paths add a top-level `pipeline` block:

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
```

Rules:

- `archetype` must be a registered pipeline archetype.
- `graph` must point to a real `pipeline/graph.yaml`.
- `artifacts_dir` scopes all worker artifact writes.
- `primary_hosts` declares the orchestration-first exports.
- `output_contract` should keep the standard response envelope intact.

## Inputs and schemas

Declare user-provided material in `inputs.slots`:

```yaml
inputs:
  slots:
    thesis:
      type: "markdown"
      path: "inputs/thesis.md"
      schema: "schemas/thesis.schema.json"
      required: true
    mappings:
      type: "yaml"
      path: "inputs/mappings.yaml"
      schema: "schemas/mappings.schema.json"
      required: false
```

Rules:

- every slot needs a real file path
- pipeline paths should give every slot a schema file
- use slots instead of burying critical config inside freeform prompts

## Agents

Worker roles are declared in `agents[]`:

```yaml
agents:
  - id: "poly-scout"
    phase: "market_research"
    description: "Find candidate markets and score quality."
    tools:
      - "read"
      - "glob"
      - "grep"
      - "bash"
      - "webfetch"
      - "websearch"
    output: ".wf-artifacts/$RUN_ID/market_research.json"
    host_mode: "worker"
```

Rules:

- each agent must have a unique `id`
- each agent should own exactly one artifact path
- outputs must stay under `pipeline.artifacts_dir`
- `skill/agents/<id>.md` must exist for every declared agent

## Host targets

Host install metadata belongs in a top-level `host` block:

```yaml
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

Use this only for install and render metadata. The source path remains host-neutral.

## Applet block

If you include a static applet UI, add:

```yaml
applet:
  build_dir: "applet/dist"
  manifest: "applet/applet.manifest.json"
```

`build_dir` must contain the HTML entry file declared by the applet manifest.
