# ProjectX adapter gotchas

## `pool_address` is required (adapter is pool-scoped)

`ProjectXLiquidityAdapter` is intentionally scoped to **one pool**:

- `_pool_meta()` / `pool_overview()` read that pool
- `fetch_swaps()` queries that pool in the subgraph
- `list_positions()` filters wallet positions to those matching this pool’s token0/token1/fee
- `current_balances()` returns balances for the pool tokens only

Provide `pool_address` via config (or `config_overrides`) when constructing the adapter.

## ProjectX pools can have non-standard tick spacing

The shared base adapter has a standard Uniswap tick-spacing map by fee tier.
Some ProjectX pools do **not** follow those defaults.

Best practice:
- Prefer `mint_from_balances()` / `increase_liquidity_balanced()` (they use the pool’s `tick_spacing`)
- If calling `add_liquidity(...)` directly, pass `tick_spacing=...` explicitly

## `fetch_swaps()` is HTTP + subgraph (handle failures)

Swap history reads can fail due to subgraph downtime or missing config.
Always check `(ok, swaps)` and fall back to on-chain `slot0().tick` if needed.

## `swap_exact_in()` is ERC20-only

`swap_exact_in()` rejects “native” token inputs/outputs. Use wrapped HYPE (WHYPE) for native-like swaps.

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

- `rpcs["999"] = ["https://..."]`

