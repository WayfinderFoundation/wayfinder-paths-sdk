# Aerodrome execution opportunities (classic pools + gauges + veAERO)

## Primary execution surface

- Adapter: `wayfinder_paths/adapters/aerodrome_adapter/adapter.py`
- Voting / rewards mixin: `wayfinder_paths/adapters/aerodrome_common.py`

## Quote before moving funds

Use quote surfaces before building a transaction:

- `quote_best_route(token_in, token_out, amount_in)` for swap routing
- `quote_add_liquidity(...)` for LP adds
- `quote_remove_liquidity(...)` for LP removals

These help you inspect route choice, expected token amounts, and slippage bounds before broadcast.

## Liquidity management

### Add liquidity

- `await adapter.add_liquidity(...)`
- Supports classic Aerodrome pool adds on Base.
- Use `quote_add_liquidity(...)` first to inspect expected minting and minimum amounts.

### Remove liquidity

- `await adapter.remove_liquidity(...)`
- Use `quote_remove_liquidity(...)` first.

### Claim unstaked pool fees

- `await adapter.claim_pool_fees_unstaked(pool=...)`
- Use when LP tokens are held unstaked and fees sit at the pool/fee contract layer.

## Gauge staking and rewards

- `await adapter.stake_lp(pool=..., amount=...)`
- `await adapter.unstake_lp(pool=..., amount=...)`
- `await adapter.claim_gauge_rewards(gauges=[...])`

Typical flow:
1. Discover pool and gauge.
2. Quote / add liquidity.
3. Stake LP in the gauge.
4. Periodically claim gauge emissions, fee rewards, or bribes as needed.

## veAERO actions

- `await adapter.create_lock(amount=..., lock_duration=...)`
- `await adapter.create_lock_for(amount=..., lock_duration=..., receiver=...)`
- `await adapter.increase_lock_amount(token_id=..., amount=...)`
- `await adapter.withdraw_lock(token_id=...)`
- `await adapter.vote(token_id=..., pools=[...], weights=[...], check_window=True)`
- `await adapter.reset_vote(token_id=...)`

Use these for veAERO governance and vote-directed incentives. Always inspect current veNFT ownership first with `get_user_ve_nfts(...)` or `get_full_user_state(...)`.

## Fees, bribes, and rebases

- `await adapter.claim_fees(token_id=..., fee_reward_contracts=[...], token_lists=None)`
- `await adapter.claim_bribes(token_id=..., bribe_reward_contracts=[...], token_lists=None)`
- `await adapter.claim_rebases(token_id=...)`
- `await adapter.claim_rebases_many(token_ids=[...])`

These claim paths are separate from gauge emission claims:
- gauge emissions use `claim_gauge_rewards`
- fee and bribe claims require the veNFT token id plus reward-contract lists
- rebases come from the rewards distributor

## Wallet + scripting pattern

Use `get_adapter(..., "main")` for write flows so `sign_callback` and the wallet address are wired automatically:

```python
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.aerodrome_adapter import AerodromeAdapter

adapter = await get_adapter(AerodromeAdapter, "main")
ok, tx = await adapter.create_lock(amount=10**18, lock_duration=7 * 24 * 60 * 60)
```

## Execution checklist

1. Confirm the pool, gauge, and token addresses on Base.
2. Quote the route or liquidity change first.
3. Confirm raw token-unit sizing.
4. Broadcast the LP / gauge / veAERO transaction.
5. Re-read wallet state to confirm balances, stakes, or veNFT ownership.
