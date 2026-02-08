# AerodromeAdapter

Aerodrome (Base) adapter for:

- Swaps via Aerodrome Router
- LP add-liquidity via Aerodrome Router
- Gauge deposits via Aerodrome Voter → Gauge
- veAERO lock creation via VotingEscrow
- Gauge voting via Voter
- Slipstream (CL) pool reads + position minting/staking

This adapter is intentionally minimal and focused on **building + broadcasting** EVM
transactions using the SDK's existing `encode_call`, `ensure_allowance`, and
`send_transaction` utilities.

## Concepts (quick)

- **v2 pools** (stable/volatile): LP emissions require **staking into the gauge**; veAERO voters earn epoch-based fees/bribes.
- **veAERO**: lock AERO into a **veNFT** (VotingEscrow) and vote weekly to direct emissions.
- **Epochs**: weekly cadence; voting is typically once per epoch (epoch starts Thu 00:00 UTC).
- **Slipstream**: Uniswap v3-style concentrated liquidity; positions are NFTs with tick ranges.

## Wiring (local dev)

For scripts / notebooks, the simplest way to wire signing + config is:

```python
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.aerodrome_adapter.adapter import AerodromeAdapter

adapter = get_adapter(AerodromeAdapter, "main", config_path="config.json")
```

## Testing / Smoke

There is a live smoke script in `scripts/aerodrome_smoke.py` that:

1. Swaps USDC → AERO
2. Locks AERO into a short veNFT
3. Adds AERO/USDC liquidity
4. Deposits LP tokens into the gauge
5. Votes with the veNFT

Run:

```bash
poetry run python scripts/aerodrome_smoke.py --wallet-label main
```

## APY / Deploy helpers

- `scripts/aerodrome_best_emissions_deploy.py`: ranks Aerodrome v2 gauges by **emissions APR** (onchain) and
  optionally deploys a USDC budget (swap → add-liquidity → stake).
- `scripts/aerodrome_best_vote_pools.py`: ranks pools by latest-epoch **fees+bribes per veAERO** (via Sugar
  `epochsLatest`) and can optionally `createLock` + `vote`.

## Slipstream (CL) analytics

- `scripts/slipstream_analyze_range.py`: analyzes a Slipstream CL pool range (ticks, liquidity share),
  estimates volume from Swap logs, and estimates an unstaked fee APR (range-adjusted).
- `scripts/slipstream_enter_position.py`: simple CL entry helper (swap USDC into both legs, mint a range NFT,
  optional gauge stake).

## Skill docs

See `.claude/skills/using-aerodrome-adapter/` for a deeper mental model (voting/locking, reads, scripts, gotchas).
