# Aerodrome helper scripts (what they do)

These scripts are intentionally thin wrappers around `AerodromeAdapter` methods.

## User state

- `scripts/aerodrome_user_state.py`
  - Pulls `get_full_user_state(...)` and prints JSON.
  - Useful flags:
    - `--include-usd`
    - `--no-slipstream`
    - `--multicall-chunk`

## Voting / veAPR exploration

- `scripts/aerodrome_best_vote_pools.py`
  - Ranks pools by **latest epoch fees+bribes per veAERO** (Sugar `epochsLatest`).
  - Can optionally:
    - swap USDC→AERO
    - `createLock`
    - vote for the selected pool
  - Safe mode:
    - `--dry-run` prints ranking only.

## Emissions deployment (v2)

- `scripts/aerodrome_best_emissions_deploy.py`
  - Ranks v2 gauge pools by **emissions APR** (onchain).
  - Optional deploy:
    - swap USDC into pool tokens
    - add liquidity
    - stake minted LP into the gauge
  - Safe mode:
    - `--dry-run`

## Live smoke test (writes)

- `scripts/aerodrome_smoke.py`
  - End-to-end sanity test:
    1. swap USDC→AERO
    2. create ve lock (veNFT)
    3. add AERO/USDC liquidity
    4. stake LP into gauge
    5. vote with the veNFT

## Slipstream analytics

- `scripts/slipstream_analyze_range.py`
  - Computes range metrics and crude fee APR for a proposed band.
  - Requires an RPC that supports `eth_getLogs` for the lookback window.

## Slipstream position entry (writes)

- `scripts/slipstream_enter_position.py`
  - Selects a Slipstream pool (best liquidity across tick spacings)
  - Swaps USDC into token0/token1
  - Mints a range position NFT
  - Optionally stakes the NFT into a gauge
  - Optional funding helper: bridge Arbitrum USDC → Base USDC (uses BRAP)

## Running

```bash
poetry run python scripts/aerodrome_best_vote_pools.py --wallet-label main --dry-run
poetry run python scripts/aerodrome_best_emissions_deploy.py --wallet-label main --dry-run
poetry run python scripts/slipstream_analyze_range.py --pool 0x...
```

