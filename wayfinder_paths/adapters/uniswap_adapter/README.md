# Uniswap V3 Adapter

Uniswap V3 concentrated liquidity provisioning adapter for the Wayfinder Paths SDK.

- **Type**: `UNISWAP`
- **Module**: `wayfinder_paths.adapters.uniswap_adapter`
- **Docs**: https://docs.uniswap.org/contracts/v3/overview

## Supported Chains

| Chain     | Chain ID |
|-----------|----------|
| Ethereum  | 1        |
| Arbitrum  | 42161    |
| Polygon   | 137      |
| Base      | 8453     |
| BSC       | 56       |
| Avalanche | 43114    |

## Capabilities

- **Liquidity**: add, increase, remove, collect fees
- **Positions**: read single, list all for an account
- **Rewards**: peek uncollected fees (read-only simulation)
- **Pool**: look up pool address
- **Analytics**: impermanent loss calculation (V3 concentrated + V2 simple)
- **Helpers**: price-to-tick, tick-to-price, nearest usable tick

## Usage

```python
from wayfinder_paths.adapters.uniswap_adapter import UniswapAdapter

adapter = UniswapAdapter(config={
    "strategy_wallet": {"address": "0x..."}
}, strategy_wallet_signing_callback=sign_fn)

# Read positions on Base
ok, positions = await adapter.get_positions(chain_id=8453)

# Add liquidity
ok, result = await adapter.add_liquidity(
    token0=WETH, token1=USDC, fee=500,
    tick_lower=-887220, tick_upper=887220,
    amount0_desired=10**18, amount1_desired=3000 * 10**6,
    amount0_min=0, amount1_min=0, chain_id=8453,
)

# Tick helpers
tick = UniswapAdapter.price_to_tick(3000.0, 18, 6)
price = UniswapAdapter.tick_to_price(tick, 18, 6)
```

## Testing

```bash
just test-adapter uniswap_adapter
```
