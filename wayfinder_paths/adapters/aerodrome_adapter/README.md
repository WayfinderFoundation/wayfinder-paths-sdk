# Aerodrome Adapter (Base)

This adapter supports Aerodrome’s classic **Pool / Gauge / veAERO** stack on **Base mainnet**.

## Supported flows (v1)

- Enumerate gauge-enabled pools via `Voter.length()` + `Voter.pools(i)`
- Sugar pool + epoch reads for analytics (`list_pools()`, `sugar_epochs_latest()`)
- Pool ranking by latest epoch fees/bribes per veAERO vote (`rank_pools_by_usdc_per_ve()`)
- Router exact-in route quoting (`quote_best_route()`, `get_amounts_out()`)
- LP add/remove liquidity (ERC20-ERC20 and ERC20-ETH)
- Stake/unstake LP into gauges + claim emissions
- veAERO: list NFTs, create lock, increase amount/time, withdraw, permanent lock/unlock
- Voting: `Voter.vote()` / `Voter.reset()` (with optional epoch-window precheck)
- Claim fees / bribes (build reward token lists dynamically)
- Claim rebases via `RewardsDistributor`

## Current protocol notes

- Use this adapter for classic stable/volatile Aerodrome pools. Use
  `aerodrome_slipstream_adapter` for concentrated-liquidity pools and NFPM
  positions.
- Current Sugar reads use Base LP Sugar for pool rows and Rewards Sugar for
  epoch rows. `sugar_all(..., pool_filter=0)` calls the current
  `LpSugar.all(limit, offset, filter)` shape.
- `SugarPool` includes current LP metadata such as `emissions_cap`, `locked`,
  `emerging`, `created_at`, `nfpm`, `alm`, and `root` when the deployed Sugar
  contract returns them.
- The adapter quotes classic router routes but does not execute swaps. Fund
  movement in this adapter is LP, gauge, veAERO, and reward-claim oriented.

## Quick usage

```python
from eth_account import Account
from wayfinder_paths.adapters.aerodrome_adapter import AerodromeAdapter

acct = Account.create()

async def sign_cb(tx: dict) -> bytes:
    return acct.sign_transaction(tx).raw_transaction

adapter = AerodromeAdapter(
    sign_callback=sign_cb,
    wallet_address=acct.address,
)
```
