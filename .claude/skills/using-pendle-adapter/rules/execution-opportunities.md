# Pendle execution opportunities (swap tx building + execution)

This repo's `PendleAdapter` builds swap payloads using Pendle's Hosted SDK endpoints. It can also execute transactions when configured with a signing callback.

Before looking up external docs, consult this repo's own adapter surfaces first:
- `wayfinder_paths/adapters/pendle_adapter/adapter.py`
- `wayfinder_paths/adapters/pendle_adapter/manifest.yaml`
- `wayfinder_paths/adapters/pendle_adapter/examples.json`

## Primary execution surface

- Adapter: `wayfinder_paths/adapters/pendle_adapter/adapter.py`

## Execute a swap (full execution)

- Call: `PendleAdapter.execute_swap(...)`
- What it does:
  1. Gets quote via `sdk_swap_v2()`
  2. Handles token approvals automatically
  3. Broadcasts the swap transaction
- Inputs (important):
  - `chain` - chain ID or name (e.g., 42161 or "arbitrum")
  - `market_address` - Pendle market address
  - `token_in` / `token_out` - ERC20 addresses (PT and YT are both valid `token_out` targets)
  - `amount_in` - **string in raw base units** (convert using token decimals)
  - `receiver` - optional; defaults to strategy wallet
  - `slippage` - **decimal fraction** (`0.01` = 1%)
  - `enable_aggregator` / `aggregators` - optional DEX aggregator settings
- Output:
  - `(True, {"tx_hash": "0x...", "chainId": ..., "quote": {...}, "tokenApprovals": [...]})`
  - `(False, {"error": "...", "stage": "quote|approval|broadcast", ...})`
- **Requires**: `signing_callback` must be configured

### Example: Swap USDC into PT

```python
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.pendle_adapter import PendleAdapter

adapter = get_adapter(PendleAdapter, "main")

success, result = await adapter.execute_swap(
    chain="base",
    market_address="0x5d6e67fce4ad099363d062815b784d281460c49b",
    token_in="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
    token_out="0x1a5c5ea50717a2ea0e4f7036fb289349deaab58b",  # PT-yoETH
    amount_in="1000000",  # 1 USDC (6 decimals)
    slippage=0.01,
)
print(f"Success: {success}, Result: {result}")
```

### Example: Swap USDC into YT

```python
# Same method, different token_out
success, result = await adapter.execute_swap(
    chain="base",
    market_address="0x5d6e67fce4ad099363d062815b784d281460c49b",
    token_in="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
    token_out="0x0ec1292d5ce7220be4c8e3a16eff7ddd165c9111",  # YT-yoETH
    amount_in="1000000",
    slippage=0.01,
)
```

### Example: Dynamic market discovery + execution

```python
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.pendle_adapter import PendleAdapter

adapter = get_adapter(PendleAdapter, "main")

# Find markets on Base
markets = await adapter.list_active_pt_yt_markets(
    chain="base",
    min_liquidity_usd=250_000,
    min_days_to_expiry=7,
    sort_by="fixed_apy",
    descending=True,
)
market = markets[0]  # Best fixed APY

# Execute swap into PT
success, result = await adapter.execute_swap(
    chain="base",
    market_address=market["marketAddress"],
    token_in="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
    token_out=market["ptAddress"],
    amount_in="1000000",
    slippage=0.01,
)
```

## Build swap payload for a specific market (quote only)

- Call: `PendleAdapter.sdk_swap_v2(...)`
- Inputs (important):
  - `amount_in` is a **string in raw base units** (convert using token decimals)
  - `slippage` is a **decimal fraction** (`0.01` = 1%)
  - `token_in` / `token_out` are **ERC20 addresses** (PTs and YTs are both valid `token_out` targets)
  - `receiver` is where output tokens will be delivered (treat `receiver != signer` as high-risk)
- Output (typical):
  - `tx`: dict with `to`, `data`, optional `value`/`from` (provider-specific)
  - `tokenApprovals`: list of `{ token, amount }` you must ensure are approved before sending `tx`
  - `data`: quote metadata (e.g., `amountOut`, `priceImpact`, `impliedApy`, `effectiveApy`)

## Select "best" PT and build its swap payload

- Call: `PendleAdapter.build_best_pt_swap_tx(...)`
- What it does:
  1) filters active markets by liquidity/volume/expiry
  2) quotes up to `max_markets_to_quote` markets
  3) selects the best by `effectiveApy` (fallbacks: implied-after, fixedApy)
- Output:
  - `tx` + `tokenApprovals` (what to execute)
  - `selectedMarket` (what was chosen)
  - `evaluated` (debug view of candidates)

## Using with get_adapter helper

When writing scripts under `.wayfinder_runs/`, use `get_adapter()` to auto-wire signing:

```python
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.pendle_adapter import PendleAdapter

# With wallet (for execution)
adapter = get_adapter(PendleAdapter, "main")

# Read-only (no wallet needed)
adapter = get_adapter(PendleAdapter)
```

## Integration checklist (for manual execution)

If not using `execute_swap()`, you still need:
- Token decimals + raw unit conversion (TokenClient / TokenAdapter)
- ERC20 approval execution for `tokenApprovals` (wallet provider / token tx helper)
- Transaction broadcast + receipt handling (wallet provider)
- Ledger recording (LedgerAdapter) if you want bookkeeping
