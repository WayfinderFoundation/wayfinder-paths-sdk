# Compound reads (markets + positions)

## Data accuracy (no guessing)

- Do **not** invent or estimate APYs, collateral factors, prices, reward amounts, or borrow minimums.
- Only report values returned by `CompoundAdapter` calls or by the configured on-chain reads behind them.
- If an RPC call fails, respond with "unavailable" and show the exact script/call needed to reproduce it.

## Primary data sources

- Adapter: `wayfinder_paths/adapters/compound_adapter/adapter.py`
- Market registry: `wayfinder_paths/core/constants/compound_contracts.py`
- Manifested capabilities: `wayfinder_paths/adapters/compound_adapter/manifest.yaml`

## Supported scope in this repo

- This adapter targets **Compound III / Comet**, not Compound v2.
- Supported configured chains currently come from `COMPOUND_COMET_BY_CHAIN`:
  - Ethereum
  - Base
  - Arbitrum
  - Polygon
- Networks that are **not** supported by this adapter in this repo:
  - Optimism
  - Scroll
  - Linea
  - Ronin
  - Unichain
  - Mantle
- Do not imply support for other Compound deployments unless that constant is expanded.

## Key read methods

| Method | Purpose | Wallet needed? |
|--------|---------|----------------|
| `get_market(chain_id, comet, include_prices=True)` | One Comet market snapshot | No |
| `get_all_markets(chain_id=None, include_prices=True, concurrency=4)` | All configured Comet markets, optionally scoped to one chain | No |
| `get_pos(chain_id, comet, account, include_prices=True, include_zero_collateral=True)` | One user's position in one Comet market | No, if `account` is passed |
| `get_full_user_state(account=None, chain_id=None, include_zero_positions=False, include_prices=True, include_zero_collateral=True, concurrency=4)` | Aggregate user state across configured Comet markets | No, if `account` is passed |

## Read shape notes

- `get_market(...)` returns market metadata such as:
  - `chain_id`, `chain_name`, `market_name`, `market_key`, `comet`
  - `base_token`, `base_token_symbol`, `base_token_decimals`
  - `base_supply_apr`, `base_supply_apy`, `base_borrow_apr`, `base_borrow_apy`
  - `base_borrow_min`, `base_min_for_rewards`, `utilization`, `pause_state`
  - `reward_token`, `reward_token_symbol`
  - `collateral_assets` with `asset`, `symbol`, `decimals`, `price_usd`, `borrow_collateral_factor`, `liquidate_collateral_factor`, `liquidation_factor`, `supply_cap`, `total_supply_asset`
- `get_pos(...)` returns:
  - base exposure: `supplied_base`, `borrowed_base`, `net_base`
  - booleans: `is_borrow_collateralized`, `is_liquidatable`
  - rewards: `reward_token`, `reward_owed`, `reward_owed_decimal`, `reward_error`
  - `collateral_positions` with raw balance, decimal balance, price, and USD value when prices are enabled
- `get_full_user_state(...)` returns:
  - top-level `account`, `position_count`, `positions`, `errors`
  - positions filtered by default to markets where the account has base supply, base debt, or collateral

## Important read limitations

- `get_pos(...)` requires both `chain_id` and the specific `comet` address. It is not a symbol-based lookup helper.
- `get_full_user_state(...)` is the cross-market helper in this adapter. If `chain_id` is omitted, it scans all configured Comet markets in the registry.
- Rewards are limited to what the configured rewards contract exposes via `getRewardOwed(...)`; do not describe extra rewards analytics that the adapter does not compute.

## Ad-hoc read scripts

### List configured Compound markets

```python
"""Fetch configured Compound III / Comet markets."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.compound_adapter import CompoundAdapter

async def main():
    adapter = await get_adapter(CompoundAdapter)  # read-only
    ok, markets = await adapter.get_all_markets()
    if not ok:
        raise RuntimeError(markets)

    for market in markets:
        print(
            market["chain_name"],
            market["market_name"],
            market["base_token_symbol"],
            "supply_apy=", market["base_supply_apy"],
            "borrow_apy=", market["base_borrow_apy"],
            "reward_token=", market["reward_token_symbol"],
        )

if __name__ == "__main__":
    asyncio.run(main())
```

### Get one user's position in one Comet market

```python
"""Fetch a user's Compound position for one Comet market."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.compound_adapter import CompoundAdapter

CHAIN_ID = 8453
COMET = "0xb125E6687d4313864e53df431d5425969c15Eb2F"  # Base USDC Comet
ACCOUNT = "0x0000000000000000000000000000000000000000"

async def main():
    adapter = await get_adapter(CompoundAdapter)
    ok, pos = await adapter.get_pos(
        chain_id=CHAIN_ID,
        comet=COMET,
        account=ACCOUNT,
    )
    if not ok:
        raise RuntimeError(pos)

    print(
        pos["chain_name"],
        pos["market_name"],
        "supplied_base=", pos["supplied_base_decimal"],
        "borrowed_base=", pos["borrowed_base_decimal"],
        "reward_owed=", pos["reward_owed_decimal"],
    )
    for item in pos["collateral_positions"]:
        print(item["symbol"], item["balance_decimal"], item["usd_value"])

if __name__ == "__main__":
    asyncio.run(main())
```

### Get aggregate user state across all configured Comet markets

```python
"""Fetch a user's Compound state across configured markets."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.compound_adapter import CompoundAdapter

ACCOUNT = "0x0000000000000000000000000000000000000000"

async def main():
    adapter = await get_adapter(CompoundAdapter)
    ok, state = await adapter.get_full_user_state(account=ACCOUNT)
    if not ok:
        raise RuntimeError(state)

    print("positions:", state["position_count"])
    for pos in state["positions"]:
        print(
            pos["chain_name"],
            pos["market_name"],
            "net_base=", pos["net_base_decimal"],
            "liquidatable=", pos["is_liquidatable"],
        )

if __name__ == "__main__":
    asyncio.run(main())
```
