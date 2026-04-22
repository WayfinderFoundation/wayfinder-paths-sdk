# Build + Publish

## Scaffold a path

Standard path:

```bash
poetry run wayfinder path init my-path --kind monitor --applet --dir examples/paths
```

Compiled strategy pipeline:

```bash
poetry run wayfinder path init my-router \
  --dir examples/paths \
  --template pipeline \
  --archetype conditional-router
```

Notes:

- `path init` uses `--dir`; the later path commands use `--path`.
- pipeline templates scaffold `policy/default.yaml`, `pipeline/graph.yaml`, `inputs/`, `schemas/`, `skill/agents/`, and `tests/evals/`.
- the repo ships a gold reference example at `examples/paths/conditional-router-reference`.

## Validate a path

Run this from the path directory:

```bash
poetry run wayfinder path fmt --path .
poetry run wayfinder path doctor --check --path .
```

For pipeline paths, also run:

```bash
poetry run wayfinder path eval --path .
```

That checks fixture output shape, null-state behavior, risk-gate behavior, and host-render coverage.

## Render skills

Generate host exports under `.build/skills/`:

```bash
poetry run wayfinder path render-skill --path .
```

Primary orchestration exports:

- Claude install tree under `.build/skills/claude/<skill>/install/.claude/...`
- OpenCode install tree under `.build/skills/opencode/<skill>/install/.opencode/...`

Secondary exports:

- Codex
- OpenClaw
- portable

## Build a bundle

```bash
poetry run wayfinder path build --path . --out dist/bundle.zip
```

Notes:

- `build` reruns validation and skill rendering before packaging.
- generated host exports are not included in `bundle.zip`.
- source archives keep tests and evals; bundle archives exclude runtime artifacts such as `.wf-artifacts/`.

## Publish

Set a Paths API base URL if needed:

```bash
export WAYFINDER_PATHS_API_URL="http://localhost:8000"
```

Then publish:

```bash
poetry run wayfinder path publish --path . --owner-wallet 0xYourWallet
```

Notes:

- publishing requires a valid `WAYFINDER_API_KEY` or `config.json -> system.api_key`
- bonded publishes still require `--owner-wallet`
- `publish` uploads `bundle.zip`, `source.zip`, and rendered skill exports when the path has a skill

## Activate a rendered export

Install a rendered export into a host scope:

```bash
poetry run wayfinder path activate --host claude --scope project --path .
poetry run wayfinder path activate --host opencode --scope project --path .
```

When an export includes install targets, `activate` applies those install operations instead of doing a raw directory copy.
