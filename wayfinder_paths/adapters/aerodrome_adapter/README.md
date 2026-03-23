# Aerodrome Adapter (Base)

This adapter supports Aerodrome’s classic **Pool / Gauge / veAERO** stack on **Base mainnet**.

## Supported flows (v1)

- Enumerate gauge-enabled pools via `Voter.length()` + `Voter.pools(i)`
- LP add/remove liquidity (ERC20-ERC20 and ERC20-ETH)
- Stake/unstake LP into gauges + claim emissions
- veAERO: list NFTs, create lock, increase amount/time, withdraw, permanent lock/unlock
- Voting: `Voter.vote()` / `Voter.reset()` (with optional epoch-window precheck)
- Claim fees / bribes (build reward token lists dynamically)
- Claim rebases via `RewardsDistributor`

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
