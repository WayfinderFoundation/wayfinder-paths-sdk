# Pack Skills

Wayfinder packs should treat skill support as explicit manifest behavior, not as an ad hoc `skill/SKILL.md` file that authors hand-maintain for every host.

## Canonical source

For normal packs, the source of truth is:

- `wfpack.yaml`
- `skill/instructions.md`
- optional `skill/scripts/`
- optional `skill/references/`
- optional `skill/assets/`

Recommended manifest block:

```yaml
skill:
  enabled: true
  source: generated
  name: "my-pack"
  description: "Inspect, validate, and operate the pack."
  instructions: "skill/instructions.md"
```

The `instructions` file should contain prose only. Keep host-specific metadata in `wfpack.yaml`.

## Generated exports

Run:

```bash
wayfinder pack render-skill --path .
```

This generates host-specific artifacts under `.build/skills/`:

- `claude/<skill-name>/SKILL.md`
- `codex/<skill-name>/SKILL.md`
- `codex/<skill-name>/agents/openai.yaml`
- `openclaw/<skill-name>/SKILL.md`
- `portable/<skill-name>/SKILL.md`
- `portable/<skill-name>/scripts/run_pack.py`

Optional `skill/scripts`, `skill/references`, and `skill/assets` folders are copied into each host export.

## Validation

`wayfinder pack doctor --check --path .` enforces the common cross-host contract:

- `skill.enabled: true`
- `skill.source: generated | provided`
- `skill.name` matches lowercase letters, numbers, and hyphens
- `skill.description` is non-empty
- generated mode includes a real `skill/instructions.md`
- provided mode includes a real `skill/SKILL.md`

`wayfinder pack doctor --fix --path .` can scaffold missing generated-mode stubs like `skill/instructions.md`, but it does not invent missing required manifest fields.

## Provided mode

Power users can opt into:

```yaml
skill:
  enabled: true
  source: provided
  name: "my-pack"
  description: "Custom hand-authored skill source."
```

In that mode, authors maintain `skill/SKILL.md` directly and `doctor` validates it instead of generating from `skill/instructions.md`.

## Formatting boundary

`wayfinder pack fmt --path .` is intentionally artifact-oriented.

It normalizes:
- `wfpack.yaml`
- `applet.manifest.json`
- generated skill exports
- generated host metadata

It does not rewrite arbitrary Python, TypeScript, or Solidity source files unless the user chooses separate language formatters.
