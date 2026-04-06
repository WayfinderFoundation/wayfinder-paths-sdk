# Aerodrome gotchas

## Base only

- `AerodromeAdapter` is Base-only in this repo.
- Do not invent multi-chain support or accept arbitrary `chain_id`.

## Pagination matters

- `get_all_markets(start=0, limit=50, ...)` returns a dict with `total` and `markets`.
- It is not a plain list like some lending adapters.
- Set `limit=None` only when you intentionally want a full scan.
- `get_full_user_state(account=..., start=..., limit=...)` is also paginated across voter pools.
- `include_vote_claimables=True` can add extra work because the adapter resolves vote reward contracts and claimables.

## Zero-address gauge is normal

- Some pools may resolve to `ZERO_ADDRESS` for the gauge.
- Treat that as "no live gauge or incentive contract", not as a broken address.
- Check `get_gauge(...)` or the `gauge` field before recommending staking.

## `stake_lp()` takes a gauge, not a pool

- The classic staking path is `stake_lp(gauge=..., amount=...)`.
- Resolve the gauge first with `get_gauge(pool=...)` or from `get_all_markets(...)`.

## Raw integer units

- Route quotes, token amounts, LP amounts, and lock amounts use raw on-chain units.
- Always resolve decimals before turning user input into call parameters.

## Quote vs execute

- `quote_best_route`, `get_amounts_out`, `quote_add_liquidity`, and `quote_remove_liquidity` do not move funds.
- `add_liquidity`, `remove_liquidity`, `stake_lp`, `create_lock`, `vote`, and the claim methods do.
- The adapter does **not** implement swap execution even though it can quote routes.

## Native token handling is limited

- Native-token support exists on classic liquidity quote, add, and remove flows.
- Route quoting expects token addresses, not an automatic native-token swap path.

## veAERO vote timing

- Aerodrome voting is restricted in the first hour of an epoch.
- It is also restricted in the last hour unless the veNFT is whitelisted.
- Use `vote(..., check_window=True)` or `reset_vote(..., check_window=True)` and surface a timing error rather than guessing.

## Reward types are separate

- Wallet-held LP fees: `claim_pool_fees_unstaked(...)`
- Gauge emissions: `claim_gauge_rewards(...)`
- Fee rewards: `claim_fees(...)`
- Bribes: `claim_bribes(...)`
- Rebases: `claim_rebases(...)`

Do not treat these as one interchangeable claim path.

## Reward-contract discovery is a separate read

- `claim_fees(...)` and `claim_bribes(...)` need reward-contract addresses.
- Resolve them first with `get_reward_contracts(gauge=...)` or `get_vote_claimables(...)`.
