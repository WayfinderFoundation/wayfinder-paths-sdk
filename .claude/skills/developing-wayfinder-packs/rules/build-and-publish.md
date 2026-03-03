# Build + Publish

## Build a bundle

From your pack directory (must contain `wfpack.yaml`):

```bash
poetry run wayfinder pack build --path . --out dist/bundle.zip
```

Output includes the bundle sha256 and writes the zip to `--out`.

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

