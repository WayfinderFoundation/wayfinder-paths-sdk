# Aerodrome Slipstream execution opportunities

## Primary execution surface

- Adapter: `wayfinder_paths/adapters/aerodrome_slipstream_adapter/adapter.py`
- Shared veAERO / reward helpers: `wayfinder_paths/adapters/aerodrome_common.py`

## Position lifecycle

### Mint a new position

- `await adapter.mint_position(token0=..., token1=..., tick_spacing=..., tick_lower=..., tick_upper=..., amount0_desired=..., amount1_desired=..., deployment_variant=None, position_manager=None, amount0_min=None, amount1_min=None, slippage_bps=50, recipient=None, deadline=None, sqrt_price_x96=0)`
- Use this to create a new concentrated-liquidity NFT position.
- Inputs include tick range, desired token amounts, deployment selection, and optional slippage bounds.

### Increase liquidity

- `await adapter.increase_liquidity(token_id=..., amount0_desired=..., amount1_desired=..., position_manager=None, amount0_min=None, amount1_min=None, slippage_bps=50, deadline=None)`
- Adds liquidity to an existing position NFT held by the wallet.

### Decrease liquidity

- `await adapter.decrease_liquidity(token_id=..., liquidity=..., position_manager=None, amount0_min=None, amount1_min=None, slippage_bps=50, deadline=None)`
- Removes some or all liquidity from a position.

### Collect fees

- `await adapter.collect_fees(token_id=..., position_manager=None, recipient=None, amount0_max=..., amount1_max=...)`
- Claims accrued trading fees from the position manager.

### Burn a position

- `await adapter.burn_position(token_id=..., position_manager=None)`
- Only valid once liquidity is zero and collectible state is cleared.

## Gauge staking for position NFTs

- `await adapter.stake_position(gauge=..., token_id=...)`
- `await adapter.unstake_position(gauge=..., token_id=...)`
- `await adapter.claim_position_rewards(gauge=..., token_id=...)`

Treat staking and reward claiming as separate steps from fee collection:
- fees come from `collect_fees(...)`
- gauge emissions for a staked token id come from `claim_position_rewards(...)`

## Shared veAERO and reward actions

Slipstream inherits the same veAERO actions as classic Aerodrome:
- `create_lock(...)`
- `create_lock_for(...)`
- `increase_lock_amount(...)`
- `increase_unlock_time(...)`
- `withdraw_lock(...)`
- `lock_permanent(...)`
- `unlock_permanent(...)`
- `vote(...)`
- `reset_vote(...)`
- `claim_gauge_rewards(gauges=[...])`
- `claim_fees(...)`
- `claim_bribes(...)`
- `claim_rebases(...)`
- `claim_rebases_many(...)`

These matter when the workflow includes veAERO-directed incentives, fee claims, or bribe claims around Slipstream gauges. When claiming veNFT fees or bribes, resolve reward contracts first with `get_reward_contracts(gauge=...)` or `get_vote_claimables(...)`.

## Wallet + scripting pattern

```python
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.aerodrome_slipstream_adapter import AerodromeSlipstreamAdapter

adapter = await get_adapter(AerodromeSlipstreamAdapter, "main")
ok, tx = await adapter.collect_fees(token_id=123)
```

## Execution checklist

1. Resolve the target deployment and position manager.
2. Read the pool and current position state first.
3. Convert human amounts to raw token units.
4. If a position is staked, unstake it before NPM-only operations like `increase_liquidity`, `decrease_liquidity`, `collect_fees`, or `burn_position`.
5. Submit mint, increase, decrease, collect, burn, gauge, and veAERO actions as separate steps.
6. Re-read the position and veNFT state after each state-changing transaction.
