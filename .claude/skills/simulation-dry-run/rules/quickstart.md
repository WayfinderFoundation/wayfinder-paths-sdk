# Quickstart: scenario testing on a fork (Gorlami)

## 1) Configure Gorlami

Gorlami is proxied through the Wayfinder API. If your `system.api_key` is set in `config.json`, dry-runs work out of the box — no extra config needed.

To override the default Gorlami URL:

```json
{
  "system": {
    "gorlami_base_url": "https://strategies.wayfinder.ai/api/v1/blockchain/gorlami"
  }
}
```

## 2) Run a strategy on a fork

Preferred entrypoint: `wayfinder_paths/run_strategy.py` with `--gorlami`.

This runs against a **virtual testnet fork** where each transaction updates state for the next step (sequential operations).

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
- `scripts/moonwell_dry_run.py` (Moonwell `deposit -> update` on a Base fork)

## 4) Cross-chain simulation

For flows that span multiple chains (e.g. bridge + swap), spin up **forks for each chain**. Execute the source chain tx on one fork, then seed the expected tokens on the destination fork (simulating bridge delivery) and continue there. Nest multiple `gorlami_fork()` context managers — each overrides `web3_from_chain_id` for its chain only.

The bridge relayer does not run between forks — manually seed what the bridge would deliver using `set_erc20_balance` after verifying the source tx succeeds.

See `SIMULATION.md` for full details and a cross-chain script example.
