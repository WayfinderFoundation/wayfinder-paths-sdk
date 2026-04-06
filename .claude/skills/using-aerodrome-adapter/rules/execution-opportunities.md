# Aerodrome execution opportunities (classic pools + gauges + veAERO)

## Primary execution surface

- Adapter: `wayfinder_paths/adapters/aerodrome_adapter/adapter.py`
- Shared veAERO / reward helpers: `wayfinder_paths/adapters/aerodrome_common.py`

## Quote before moving funds

Use quote surfaces before building a transaction:

- `quote_best_route(amount_in=..., token_in=..., token_out=..., intermediates=None)` for swap routing
- `quote_add_liquidity(...)` for LP adds
- `quote_remove_liquidity(...)` for LP removals

These help you inspect route choice, expected token amounts, and slippage bounds before broadcast. The classic adapter does **not** implement swap execution; route helpers are quoting-only.

## Liquidity management

### Add liquidity

- `await adapter.add_liquidity(...)`
- Supports classic ERC20-ERC20 and ERC20-native pool adds on Base.
- Use `quote_add_liquidity(...)` first to inspect expected minting and minimum amounts.

### Remove liquidity

- `await adapter.remove_liquidity(...)`
- Supports classic ERC20-ERC20 and ERC20-native pool exits for wallet-held LP.
- Use `quote_remove_liquidity(...)` first.

### Claim unstaked pool fees

- `await adapter.claim_pool_fees_unstaked(pool=...)`
- Use when LP tokens are held unstaked and fees sit at the pool layer.

## Gauge staking and rewards

- `await adapter.stake_lp(gauge=..., amount=..., recipient=None)`
- `await adapter.unstake_lp(gauge=..., amount=...)`
- `await adapter.claim_gauge_rewards(gauges=[...])`

Typical flow:
1. Discover pool and gauge.
2. Quote or add liquidity.
3. Stake LP in the gauge.
4. Periodically claim gauge emissions or unstake when you need wallet-held LP again.

## veAERO actions

- `await adapter.create_lock(amount=..., lock_duration=...)`
- `await adapter.create_lock_for(amount=..., lock_duration=..., receiver=...)`
- `await adapter.increase_lock_amount(token_id=..., amount=...)`
- `await adapter.increase_unlock_time(token_id=..., lock_duration=...)`
- `await adapter.withdraw_lock(token_id=...)`
- `await adapter.lock_permanent(token_id=...)`
- `await adapter.unlock_permanent(token_id=...)`
- `await adapter.vote(token_id=..., pools=[...], weights=[...], check_window=True)`
- `await adapter.reset_vote(token_id=..., check_window=True)`

Use these for veAERO governance and vote-directed incentives. Inspect current veNFT ownership first with `get_user_ve_nfts(...)` or `get_full_user_state(...)`.

## Fees, bribes, and rebases

- `ok, reward_contracts = await adapter.get_reward_contracts(gauge=...)`
- `await adapter.claim_fees(token_id=..., fee_reward_contracts=[...], token_lists=None)`
- `await adapter.claim_bribes(token_id=..., bribe_reward_contracts=[...], token_lists=None)`
- `await adapter.claim_rebases(token_id=...)`
- `await adapter.claim_rebases_many(token_ids=[...])`

These claim paths are separate from gauge emission claims:
- gauge emissions use `claim_gauge_rewards`
- unstaked wallet LP fees use `claim_pool_fees_unstaked`
- fee and bribe claims require the veNFT token id plus reward-contract lists
- `token_lists` can be omitted and the adapter will discover reward tokens from the reward contracts
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
4. Resolve fee and bribe reward contracts before veNFT reward claims.
5. Broadcast the LP, gauge, or veAERO transaction.
6. Re-read wallet state to confirm balances, stakes, votes, or veNFT ownership.
