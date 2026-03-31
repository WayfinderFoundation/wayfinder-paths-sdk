# Aerodrome gotchas

## Base only

- `AerodromeAdapter` is Base-only in this repo.
- Do not invent multi-chain support or accept arbitrary `chain_id`.

## `get_all_markets()` is paginated

- `get_all_markets(start=0, limit=50, ...)` returns a dict with `total` and `markets`.
- It is not a plain list like some lending adapters.
- Set `limit=None` only when you intentionally want a full scan.

## Zero-address gauge is normal

- Some pools may resolve to `ZERO_ADDRESS` for the gauge.
- Treat that as "no live gauge / incentive contract", not as a broken address.
- Check `get_gauge(...)` or the `gauge` field before recommending staking.

## Raw integer units

- Route quotes, token amounts, LP amounts, and lock amounts use raw on-chain units.
- Always resolve decimals before turning user input into call parameters.

## Quote vs execute

- `quote_best_route`, `get_amounts_out`, `quote_add_liquidity`, and `quote_remove_liquidity` do not move funds.
- `add_liquidity`, `remove_liquidity`, `stake_lp`, `create_lock`, `vote`, and the claim methods do.

## veAERO vote timing

- Aerodrome voting is restricted in the first hour of an epoch.
- It is also restricted in the last hour unless the veNFT is whitelisted.
- Use `vote(..., check_window=True)` and surface a timing error rather than guessing.

## `get_full_user_state()` is also paged

- The user-state scan pages across voteable pools.
- If you need a broader portfolio snapshot, raise `limit` or iterate `start`.

## Reward types are separate

- Gauge emissions: `claim_gauge_rewards(...)`
- Fee rewards: `claim_fees(...)`
- Bribes: `claim_bribes(...)`
- Rebases: `claim_rebases(...)`

Do not treat these as one interchangeable claim path.
