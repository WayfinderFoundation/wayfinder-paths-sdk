# Execution opportunities (what can be broadcast)

## Required wiring

Any onchain write needs a signing callback:

- `strategy_wallet_signing_callback` (preferred name in adapters)

For local dev scripts, `get_adapter()` auto-wires this from `config.json` wallets.

## v2 swaps / LP / gauges

- Swap:
  - `swap_exact_tokens_for_tokens(...)`
  - `swap_exact_tokens_for_tokens_best_route(...)`
- Add liquidity:
  - `add_liquidity(...)`
- Stake LP in gauge:
  - `deposit_gauge(gauge=..., lp_token=..., amount=...)`
- ve lock:
  - `create_lock(...)`
- vote:
  - `vote(token_id=..., pools=..., weights=...)`

## Slipstream (CL)

- Find a “best” pool for a pair:
  - `slipstream_best_pool_for_pair(token_a=..., token_b=...)`
- Mint a position NFT:
  - `slipstream_mint_position(pool=..., tick_lower=..., tick_upper=..., amount0_desired=..., amount1_desired=..., ...)`
- Optional: stake position NFT into gauge (if a gauge exists):
  - `slipstream_approve_position(spender=gauge, token_id=...)`
  - `slipstream_gauge_deposit(gauge=..., token_id=..., approve=False)`

## Safety / correctness checks

- Don’t assume a tx hash implies success — check the receipt (`status=1`).
- Use conservative slippage and deadlines for swaps.
- Ensure you understand “fees to voters” vs “fees to LPs” depending on pool type.

