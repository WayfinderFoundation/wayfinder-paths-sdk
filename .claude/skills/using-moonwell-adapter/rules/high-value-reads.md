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
"""Fetch all Moonwell markets and rates."""
import asyncio
from web3 import Web3
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter
from wayfinder_paths.adapters.moonwell_adapter.adapter import COMPTROLLER_ABI

COMPTROLLER = "0xfbb21d0380bee3312b33c4353c8936a0f13ef26c"
ERC20_ABI = [{"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"}]

async def main():
    adapter = get_adapter(MoonwellAdapter)
    web3 = Web3(Web3.HTTPProvider("https://mainnet.base.org"))
    comptroller = web3.eth.contract(address=web3.to_checksum_address(COMPTROLLER), abi=COMPTROLLER_ABI)

    for mtoken_addr in comptroller.functions.getAllMarkets().call():
        mtoken_addr = web3.to_checksum_address(mtoken_addr)
        symbol = web3.eth.contract(address=mtoken_addr, abi=ERC20_ABI).functions.symbol().call()
        ok, supply = await adapter.get_apy(mtoken=mtoken_addr, apy_type="supply", include_rewards=True)
        ok, borrow = await adapter.get_apy(mtoken=mtoken_addr, apy_type="borrow", include_rewards=True)
        print(f"{symbol}: supply={supply:.2%} borrow={borrow:.2%}")

if __name__ == "__main__":
    asyncio.run(main())
```

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
| `get_apy(mtoken, apy_type, include_rewards)` | Supply/borrow APY | No |
| `get_collateral_factor(mtoken)` | Collateral factor (e.g., 0.88) | No |
| `get_pos(mtoken, account?, include_usd?)` | Single market position | Yes (or pass account) |
| `get_full_user_state(account?, include_rewards?, include_usd?, include_apy?)` | All positions + rewards | Yes (or pass account) |
| `is_market_entered(mtoken, account?)` | Check if collateral enabled | Yes (or pass account) |
| `get_borrowable_amount(account?)` | Account liquidity (USD) | Yes (or pass account) |
| `max_withdrawable_mtoken(mtoken, account?)` | Max withdraw without liquidation | Yes (or pass account) |
