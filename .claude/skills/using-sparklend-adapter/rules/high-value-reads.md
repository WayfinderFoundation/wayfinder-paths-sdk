# SparkLend reads (markets + positions)

## Data accuracy (no guessing)

- Do **not** invent or estimate SparkLend APYs, caps, LTVs, or account values.
- Only report values returned by `SparkLendAdapter` calls.
- If an RPC call fails, respond with "unavailable" and show the exact script/call needed to reproduce it.

## Primary data sources

- Adapter: `wayfinder_paths/adapters/sparklend_adapter/adapter.py`
- Chain config: `wayfinder_paths/core/constants/sparklend_contracts.py`

## Supported deployment in this repo

- `SPARKLEND_BY_CHAIN` currently only configures Ethereum mainnet (`chain_id=1`).
- Do not imply SparkLend coverage on other chains unless that constant is expanded.

## Key read methods

| Method | Purpose | Wallet needed? |
|--------|---------|----------------|
| `get_all_markets(chain_id, include_caps=True)` | Market list with reserve config, supply/borrow rates, token addresses, and optional caps | No |
| `get_pos(chain_id, asset, account=None)` | One reserve position for one user | No, if `account` is passed |
| `get_full_user_state(chain_id, account, include_zero_positions=False)` | Single-chain SparkLend account snapshot | No, if `account` is passed |

## Read shape notes

- `get_all_markets(...)` returns Spark-specific market fields such as:
  - `underlying`, `symbol`, `decimals`
  - `supply_token`, `stable_debt_token`, `variable_debt_token`
  - `ltv_bps`, `liquidation_threshold_bps`, `liquidation_bonus_bps`
  - `supply_apr`, `supply_apy`, `variable_borrow_apr`, `variable_borrow_apy`
  - `stable_borrow_apr`, `stable_borrow_apy`
  - `supply_cap`, `borrow_cap`, `supply_cap_headroom`, `borrow_cap_headroom`
- `get_pos(...)` returns one asset-level snapshot with raw balances and reserve config.
- `get_full_user_state(...)` returns:
  - top-level `account_data` (`total_collateral_base`, `total_debt_base`, `available_borrows_base`, `current_liquidation_threshold`, `ltv`, `health_factor`)
  - `positions` list for reserves with balances/collateral usage

## Important read limitation

- Unlike Aave V3, SparkLend's `get_full_user_state(...)` in this repo is **single-chain** and requires `chain_id`.
- Do not describe it as a cross-chain aggregate.

## Ad-hoc read scripts

### List SparkLend markets

```python
"""Fetch SparkLend markets on Ethereum."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.sparklend_adapter.adapter import SparkLendAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM

async def main():
    adapter = get_adapter(SparkLendAdapter)  # read-only
    ok, markets = await adapter.get_all_markets(
        chain_id=CHAIN_ID_ETHEREUM,
        include_caps=True,
    )
    if not ok:
        raise RuntimeError(markets)

    for m in markets:
        print(
            m["symbol"],
            "supply_apy=", m["supply_apy"],
            "variable_borrow_apy=", m["variable_borrow_apy"],
            "stable_borrow_enabled=", m["stable_borrow_enabled"],
        )

if __name__ == "__main__":
    asyncio.run(main())
```

### Get a user's single-chain SparkLend state

```python
"""Fetch a SparkLend user snapshot on Ethereum."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.sparklend_adapter.adapter import SparkLendAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM

USER = "0x0000000000000000000000000000000000000000"

async def main():
    adapter = get_adapter(SparkLendAdapter)  # read-only if account is explicit
    ok, state = await adapter.get_full_user_state(
        chain_id=CHAIN_ID_ETHEREUM,
        account=USER,
    )
    if not ok:
        raise RuntimeError(state)

    print(state["account_data"])
    for p in state["positions"]:
        print(
            p["symbol"],
            "supply_raw=", p["supply_raw"],
            "stable_borrow_raw=", p["stable_borrow_raw"],
            "variable_borrow_raw=", p["variable_borrow_raw"],
        )

if __name__ == "__main__":
    asyncio.run(main())
```

### Get one reserve position

```python
"""Fetch one SparkLend reserve position."""
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.sparklend_adapter.adapter import SparkLendAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM

USER = "0x0000000000000000000000000000000000000000"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"

async def main():
    adapter = get_adapter(SparkLendAdapter)
    ok, pos = await adapter.get_pos(
        chain_id=CHAIN_ID_ETHEREUM,
        asset=USDC,
        account=USER,
    )
    if not ok:
        raise RuntimeError(pos)
    print(pos)

if __name__ == "__main__":
    asyncio.run(main())
```
