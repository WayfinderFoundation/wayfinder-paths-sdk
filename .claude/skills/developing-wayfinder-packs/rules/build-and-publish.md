# Build + Publish

## Scaffold a new pack

Use `init` before any of the `--path`-based pack commands:

```bash
poetry run wayfinder pack init my-pack --kind monitor --applet --dir examples/packs
```

This creates `examples/packs/my-pack/`.

Important:
- `pack init` uses `--dir` for the base directory, not `--path`.
- The pack slug is the last path segment that gets created under `--dir`.
- After scaffolding, change into the new pack directory and use `--path .` for the rest of the workflow.

## Build a bundle

From your pack directory (must contain `wfpack.yaml`):

```bash
poetry run wayfinder pack fmt --path .
poetry run wayfinder pack doctor --check --path .
poetry run wayfinder pack render-skill --path .
poetry run wayfinder pack build --path . --out dist/bundle.zip
```

Output includes the bundle sha256 and writes the zip to `--out`.

Notes:
- `build` and `publish` automatically rerun pack validation and skill rendering before packaging.
- Generated skill exports are written under `.build/skills/...` and are not included in `bundle.zip`.
- `poetry run wayfinder pack preview --check --path .` validates applet preview readiness without starting servers.

## Configure Packs API base URL

The CLI publishes to a Packs API base URL (no trailing `/api`).

Set one of:

- `config.json` → `system.packs_api_base_url`
- or env var: `WAYFINDER_PACKS_API_URL`

Example for local dev:

```bash
export WAYFINDER_PACKS_API_URL="http://localhost:8000"
```

## Publish

```bash
poetry run wayfinder pack publish --path . --owner-wallet 0xYourWallet
```

Notes:
- Publishing requires a valid `WAYFINDER_API_KEY` (or `config.json` → `system.api_key`).
- `--owner-wallet` must match a wallet associated with your account (how ownership is enforced will evolve).

## Git hooks

Install pack-focused pre-commit hooks into the current pack directory:

```bash
poetry run wayfinder pack hooks install --path .
```

That writes `.pre-commit-config.yaml` with:
- `wayfinder pack fmt --path .`
- `wayfinder pack doctor --check --path .`
- `wayfinder pack preview --check --path .` on `pre-push`
