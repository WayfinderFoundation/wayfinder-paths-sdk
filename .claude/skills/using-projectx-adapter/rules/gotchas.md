# ProjectX adapter gotchas

## `pool_address` is optional (pool-agnostic vs pool-scoped)

`ProjectXLiquidityAdapter` can run in two modes:

**Pool-agnostic (no `pool_address`):** Works for cross-pool reads and operations that don't need a specific pool:
- `get_full_user_state()` — returns positions + points (skips overview/balances)
- `_list_all_positions()` — all active positions across all pools
- `fetch_prjx_points()` — points lookup
- `burn_position()` — close any position by token_id
- `swap_exact_in()` — routes via `_find_pool_for_pair` (no fee hint from configured pool)

**Pool-scoped (with `pool_address`):** Required for pool-specific operations:
- `pool_overview()` / `current_balances()` / `list_positions()` — read that pool
- `fetch_swaps()` — subgraph queries for that pool
- `live_fee_snapshot()` — fee calculation against that pool
- `mint_from_balances()` / `increase_liquidity_balanced()` — use pool tick_spacing/fee

These methods raise `ValueError("pool_address is required …")` if called without a pool.

Provide `pool_address` via config (or `config_overrides`) when you need pool-scoped operations.

## ProjectX pools can have non-standard tick spacing

The shared base adapter has a standard Uniswap tick-spacing map by fee tier.
Some ProjectX pools do **not** follow those defaults.

Best practice:
- Prefer `mint_from_balances()` / `increase_liquidity_balanced()` (they use the pool’s `tick_spacing`)
- If calling `add_liquidity(...)` directly, pass `tick_spacing=...` explicitly

## ProjectX fee tiers include non-standard V3 tiers

The ProjectX app and live factory currently expose fee tiers:

- `100` (0.01%)
- `500` (0.05%)
- `1000` (0.1%)
- `2000` (0.2%)
- `3000` (0.3%)
- `10000` (1%)
- `20000` (2%)

Use `PROJECTX_DEFAULT_FEE_TIERS` for ProjectX route searches. Do not reuse a generic
Uniswap fee list that omits `2000` or `20000`.

## `fetch_swaps()` is HTTP + subgraph (handle failures)

Swap history reads can fail due to subgraph downtime or missing config.
Always check `(ok, swaps)` and fall back to on-chain `slot0().tick` if needed.

## Points are updated daily (eventual consistency)

`fetch_prjx_points()` reads a points endpoint that updates daily. If points don’t show up
immediately after activity, treat it as normal (not a strategy/adaptor bug).

## `swap_exact_in()` is ERC20-only

`swap_exact_in()` rejects "native" token inputs/outputs. Use wrapped HYPE (WHYPE) for native-like swaps.

The ProjectX app displays native HYPE as `0x0000000000000000000000000000000000000000`,
but ProjectX V3 factory/router/NPM contracts use WHYPE (`0x5555555555555555555555555555555555555555`)
as `WETH9`. Pass WHYPE to adapter methods. Native unwrap/refund flows are not implemented here.

## `swap_exact_in()` is direct PRJX router support, not app aggregator routing

The SDK helper builds PRJX router `exactInputSingle` transactions. The ProjectX web app also
loads Reown/AppKit swap provider code for quote/calldata generation. Do not assume app UI
routes, provider fees, or aggregator paths are reproduced by `swap_exact_in()`.

## `swap_exact_in()` routes through `_find_pool_for_pair` with liquidity checks

`swap_exact_in()` **always** calls `_find_pool_for_pair` — even when the swap tokens match the
configured pool's pair. When tokens match and no `prefer_fees` is passed, it prepends the
configured pool's fee tier so that pool is tried first, but falls through to other fee tiers
if it has zero liquidity.

`_find_pool_for_pair` checks `pool.liquidity()` on-chain and prefers pools with non-zero
liquidity. If all candidate pools have zero liquidity it falls back to the first existing pool.

This means `swap_exact_in` will find the deepest pool automatically — you don't need to manually
specify `prefer_fees` unless you want to override the search order.

## Tuple-return convention: always destructure

All adapter methods return `(ok, data|str)`:

```python
ok, positions = await adapter.list_positions()
if not ok:
    raise RuntimeError(positions)
```

Do **not** treat the tuple like a list/dict; that causes classic bugs like accessing `.token_id` on the `ok` boolean.

## Units are raw ints

All amounts are raw base units (wei). Convert human → raw using token decimals.

## RPC for chain 999 must be configured

If `web3_from_chain_id(999)` raises, add HyperEVM RPC URLs under `config.json`:

- `strategy.rpc_urls["999"] = ["https://..."]`

For local smoke validation, set the chain 999 strategy RPC URL to the public HyperEVM RPC
or run the env-gated smoke test:

```bash
PROJECTX_HYPEREVM_SMOKE=1 poetry run pytest -o addopts= wayfinder_paths/adapters/projectx_adapter/test_hyperevm_smoke.py -q
```
