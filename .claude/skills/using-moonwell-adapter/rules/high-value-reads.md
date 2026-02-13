# Moonwell reads (markets + positions)

## Data accuracy (no guessing)

- Do **not** invent or estimate APYs, borrow rates, or collateral factors.
- Only report values fetched from Moonwell contracts via the adapter.
- If you can't fetch data (RPC failure), respond with "unavailable" and show the exact script needed.

## Primary data source

- Adapter: `wayfinder_paths/adapters/moonwell_adapter/adapter.py`
- Chain: Base (chain_id 8453)
- Comptroller: `0xfbb21d0380bee3312b33c4353c8936a0f13ef26c`

## Ad-hoc read scripts

All read scripts go under `.wayfinder_runs/` and use `get_adapter()`:

### Get APY for a market

```python
"""Fetch Moonwell APY for a market."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter

USDC_MTOKEN = "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22"

async def main():
    adapter = get_adapter(MoonwellAdapter)  # read-only, no wallet needed
    ok, supply_apy = await adapter.get_apy(mtoken=USDC_MTOKEN, apy_type="supply", include_rewards=True)
    ok, borrow_apy = await adapter.get_apy(mtoken=USDC_MTOKEN, apy_type="borrow", include_rewards=True)
    print(f"Supply: {supply_apy:.2%}, Borrow: {borrow_apy:.2%}")

if __name__ == "__main__":
    asyncio.run(main())
```

### Get all markets with rates

```python
"""Fetch all Moonwell markets with reward-inclusive APYs."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter

async def main():
    adapter = get_adapter(MoonwellAdapter)  # read-only, no wallet needed
    ok, markets = await adapter.get_all_markets()
    if not ok:
        raise RuntimeError(f"Failed to fetch markets: {markets}")
    for m in markets:
        print(
            f"{m.get('symbol', '')}: "
            f"supply={m.get('supplyApy', 0.0):.2%} (base={m.get('baseSupplyApy', 0.0):.2%}, rewards={m.get('rewardSupplyApy', 0.0):.2%}) "
            f"borrow={m.get('borrowApy', 0.0):.2%} (base={m.get('baseBorrowApy', 0.0):.2%}, rewards={m.get('rewardBorrowApy', 0.0):.2%})"
        )

if __name__ == "__main__":
    asyncio.run(main())
```

> **Note:** `get_all_markets(include_rewards=False)` skips reward incentives and returns base-only yields.

### Get user position

```python
"""Fetch user position on Moonwell."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter

USDC_MTOKEN = "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22"

async def main():
    adapter = get_adapter(MoonwellAdapter, "main")  # wallet needed for account lookup

    # For all positions, use get_full_user_state()
    ok, state = await adapter.get_full_user_state()
    print(f"Liquidity: {state['accountLiquidity']}")
    for p in state.get("positions", []):
        print(f"  {p['mtoken'][:10]}... supplied={p['suppliedUnderlying']} borrowed={p['borrowedUnderlying']}")

    # For single market position, use get_pos(mtoken=...)
    ok, pos = await adapter.get_pos(mtoken=USDC_MTOKEN)
    print(f"Supplied: {pos['underlying_balance'] / 1e6:.2f} USDC")

if __name__ == "__main__":
    asyncio.run(main())
```

## Key read methods

| Method | Purpose | Wallet needed? |
|--------|---------|----------------|
| `get_all_markets(include_apy?, include_usd?)` | All markets with symbols, rates, TVL | No |
| `get_apy(mtoken, apy_type, include_rewards)` | Supply/borrow APY for single market | No |
| `get_collateral_factor(mtoken)` | Collateral factor (e.g., 0.88) | No |
| `get_pos(mtoken, account?, include_usd?)` | Single market position | Yes (or pass account) |
| `get_full_user_state(account?, include_rewards?, include_usd?, include_apy?)` | All positions + rewards | Yes (or pass account) |
| `is_market_entered(mtoken, account?)` | Check if collateral enabled | Yes (or pass account) |
| `get_borrowable_amount(account?)` | Account liquidity (USD) | Yes (or pass account) |
| `max_withdrawable_mtoken(mtoken, account?)` | Max withdraw without liquidation | Yes (or pass account) |
