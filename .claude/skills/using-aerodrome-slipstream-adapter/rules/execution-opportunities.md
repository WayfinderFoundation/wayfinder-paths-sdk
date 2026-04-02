# Aerodrome Slipstream execution opportunities

## Primary execution surface

- Adapter: `wayfinder_paths/adapters/aerodrome_slipstream_adapter/adapter.py`
- Voting / rewards mixin: `wayfinder_paths/adapters/aerodrome_common.py`

## Position lifecycle

### Mint a new position

- `await adapter.mint_position(...)`
- Use this to create a new concentrated-liquidity NFT position.
- Inputs include tick range, desired token amounts, and optional slippage bounds.

### Increase liquidity

- `await adapter.increase_liquidity(token_id=..., ...)`
- Adds liquidity to an existing position NFT.

### Decrease liquidity

- `await adapter.decrease_liquidity(token_id=..., liquidity=..., ...)`
- Removes some or all liquidity from a position.

### Collect fees

- `await adapter.collect_fees(token_id=...)`
- Claims accrued trading fees from the position manager.

### Burn a position

- `await adapter.burn_position(token_id=...)`
- Only valid once liquidity is zero and collectible state is cleared.

## Gauge staking for position NFTs

- `await adapter.stake_position(gauge=..., token_id=...)`
- `await adapter.unstake_position(gauge=..., token_id=...)`
- `await adapter.claim_position_rewards(gauge=..., token_id=...)`

Treat staking and reward claiming as separate steps from fee collection:
- fees come from `collect_fees(...)`
- gauge emissions come from `claim_position_rewards(...)`

## veAERO-linked actions

Slipstream inherits the same veAERO actions as classic Aerodrome:
- `create_lock(...)`
- `create_lock_for(...)`
- `increase_lock_amount(...)`
- `withdraw_lock(...)`
- `vote(...)`
- `reset_vote(...)`
- `claim_fees(...)`
- `claim_bribes(...)`
- `claim_rebases(...)`
- `claim_rebases_many(...)`

These matter when the workflow includes veAERO-directed incentives, fee claims, or bribe claims around Slipstream gauges.

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
4. Submit mint / increase / decrease / collect / burn as separate steps.
5. Re-read the position after each state-changing transaction.
