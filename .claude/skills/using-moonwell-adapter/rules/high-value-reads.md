# Moonwell Reads

## Data Accuracy

- Do not invent APYs, borrow rates, collateral factors, caps, or pause state.
- Fetch values through the adapter or Moonwell contracts.
- If RPC or dependencies are unavailable, say the value is unavailable and show the exact call needed.

## Primary Sources

- Adapter: `wayfinder_paths/adapters/moonwell_adapter/adapter.py`
- Constants: `wayfinder_paths/core/constants/moonwell_contracts.py`
- Official docs: `https://docs.moonwell.fi/`
- Official contract docs: `https://docs.moonwell.fi/moonwell/protocol-information/contracts`

## Supported Networks

| Network | chain_id | Notes |
|---------|----------|-------|
| Base | `8453` | Default chain; rewards read/claim configured |
| OP Mainnet | `10` | Rewards read/claim configured |
| Moonbeam | `1284` | No Multi-Reward Distributor configured; some markets are bad-debt/deprecated |
| Moonriver | `1285` | Official SDK marks all Core markets deprecated |

Pass `chain_id=` on reads when you do not want the Base default.

## Market Discovery

```python
import asyncio

from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter
from wayfinder_paths.core.constants.moonwell_contracts import CHAIN_ID_OPTIMISM
from wayfinder_paths.mcp.scripting import get_adapter


async def main():
    adapter = await get_adapter(MoonwellAdapter)
    ok, markets = await adapter.get_all_markets(
        chain_id=CHAIN_ID_OPTIMISM,
        include_apy=True,
        include_rewards=True,
        include_usd=False,
    )
    if not ok:
        raise RuntimeError(markets)
    for market in markets:
        print(
            market["chainName"],
            market.get("symbol"),
            market.get("underlyingSymbol"),
            market.get("supplyApy"),
            market.get("borrowApy"),
            market.get("deprecated"),
            market.get("badDebt"),
        )


if __name__ == "__main__":
    asyncio.run(main())
```

`get_all_markets()` returns live contract state plus local metadata such as
`chainId`, `chainName`, `underlyingSymbol`, `deprecated`, `badDebt`, and
`nativeUnderlying`.

## Position Reads

```python
import asyncio

from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter
from wayfinder_paths.mcp.scripting import get_adapter


async def main():
    adapter = await get_adapter(MoonwellAdapter, "main")
    ok, state = await adapter.get_full_user_state(
        chain_id=8453,
        include_rewards=True,
        include_apy=True,
        include_usd=False,
    )
    if not ok:
        raise RuntimeError(state)
    print(state["accountLiquidity"])
    for position in state.get("positions", []):
        print(position["mtoken"], position["suppliedUnderlying"], position["borrowedUnderlying"])


if __name__ == "__main__":
    asyncio.run(main())
```

## Key Read Methods

| Method | Purpose | Wallet needed? |
|--------|---------|----------------|
| `get_all_markets(chain_id?, include_apy?, include_usd?, include_rewards?)` | All Core markets with rates and caps | No |
| `get_apy(mtoken, chain_id?, apy_type?, include_rewards?)` | Supply or borrow APY for one Core market | No |
| `get_collateral_factor(mtoken, chain_id?)` | Collateral factor | No |
| `get_pos(mtoken, chain_id?, account?, include_usd?)` | One market position | Yes, unless `account` is passed |
| `get_full_user_state(chain_id?, account?, include_rewards?, include_usd?, include_apy?)` | All positions, liquidity, and rewards | Yes, unless `account` is passed |
| `is_market_entered(mtoken, chain_id?, account?)` | Whether collateral is enabled | Yes, unless `account` is passed |
| `get_borrowable_amount(chain_id?, account?)` | Account liquidity | Yes, unless `account` is passed |
| `max_withdrawable_mtoken(mtoken, chain_id?, account?)` | Max mToken redeem amount without shortfall | Yes, unless `account` is passed |

## Vaults And Isolated Markets

Moonwell Morpho Vaults and isolated markets are Morpho-based. Use `MorphoAdapter`
for deposits, withdrawals, collateral, borrowing, and Morpho reward flows. Do not
reimplement those flows inside `MoonwellAdapter`.
