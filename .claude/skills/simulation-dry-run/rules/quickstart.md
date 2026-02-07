# Quickstart: scenario testing on a fork (Gorlami)

## 1) Configure Gorlami

Add to `config.json`:

```json
{
  "system": {
    "gorlami_base_url": "https://app.wayfinder.ai/gorlami/api/v1/gornet",
    "gorlami_api_key": "gorlami_..."
  }
}
```

Notes:
- Header is `Authorization: <key>` (raw key; not `Bearer ...`).
- Keep `config.json` local (gitignored).

## 2) Run a strategy on a fork

Preferred entrypoint: `wayfinder_paths/run_strategy.py` with `--gorlami`.

```bash
poetry run python wayfinder_paths/run_strategy.py moonwell_wsteth_loop_strategy \
  --action status \
  --gorlami \
  --config config.json
```

For deposit flows:

```bash
poetry run python wayfinder_paths/run_strategy.py moonwell_wsteth_loop_strategy \
  --action deposit \
  --main-token-amount 20 \
  --gorlami \
  --config config.json

poetry run python wayfinder_paths/run_strategy.py moonwell_wsteth_loop_strategy \
  --action update \
  --gorlami \
  --config config.json
```

Fork funding controls (optional):
- `--gorlami-fund-native-eth ADDRESS:ETH`
- `--gorlami-fund-erc20 TOKEN:WALLET:AMOUNT:DECIMALS`
- `--gorlami-no-default-gas` (disables default ETH seeding)

## 3) Run a script on a fork

Examples in this repo:
- `scripts/brap_swap.py --gorlami` (requires `--confirm-live` for real broadcast)
- `scripts/moonwell_dry_run.py` (Moonwell `deposit -> update` on a Base fork)

See `SIMULATION.md` for details.

