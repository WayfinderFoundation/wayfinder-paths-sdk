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
- **Requires**: `sign_callback` must be configured (use `get_adapter(PendleAdapter, "main")`)

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

## Redeem expired/matured PTs (convert PT → underlying)

After a PT passes its maturity date, redeem it using `execute_convert` (the universal convert endpoint). This is **not** a swap — it's a direct redemption of PT for the underlying asset.

**Flow:**
1. Discover positions + underlying addresses via `get_full_user_state_per_chain`
2. Call `execute_convert` with PT as input, underlying as output
3. (Optional) Swap the underlying to USDC/stables via BRAP

- Call: `PendleAdapter.execute_convert(...)`
- What it does:
  1. Builds convert payload via `sdk_convert_v2()`
  2. Checks balances (preflight)
  3. Handles token approvals automatically
  4. Broadcasts the transaction
- Inputs:
  - `chain` - chain ID or name
  - `slippage` - decimal fraction (`0.01` = 1%)
  - `inputs` - list of `{"token": PT_address, "amount": raw_balance_string}`
  - `outputs` - list of underlying token addresses to receive
  - `receiver` - optional; defaults to strategy wallet
- Output:
  - `(True, {"tx_hash": "0x...", ...})`
  - `(False, {"error": "...", "stage": "preflight|quote|approval|broadcast", ...})`

### Example: Redeem expired PTs

```python
import asyncio
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.pendle_adapter import PendleAdapter
from wayfinder_paths.core.utils.tokens import get_token_balance

adapter = get_adapter(PendleAdapter, "main")
wallet = adapter._strategy_address()

CHAIN = 42161  # Arbitrum

# Step 1: Discover positions and underlying tokens
ok, state = await adapter.get_full_user_state_per_chain(
    chain=CHAIN, account=wallet, include_prices=True,
)
if not ok:
    raise RuntimeError(f"Failed to get user state: {state}")

# Step 2: Find expired PT positions
for pos in state.get("positions", []):
    pt_addr = pos.get("pt", "")
    underlying = pos.get("underlying", "")
    pt_balance = pos.get("balances", {}).get("pt", {})
    raw_balance = str(pt_balance.get("raw", 0))

    if int(raw_balance) == 0 or not underlying:
        continue

    print(f"Redeeming {pos.get('marketName')}: PT={pt_addr} -> {underlying}")

    # Step 3: Redeem PT -> underlying
    ok, result = await adapter.execute_convert(
        chain=CHAIN,
        slippage=0.01,
        inputs=[{"token": pt_addr, "amount": raw_balance}],
        outputs=[underlying],
    )
    if ok:
        print(f"  SUCCESS: {result.get('tx_hash')}")
    else:
        print(f"  FAILED: {result}")
```

### Important notes on PT redemption

- **Expired PTs redeem to the SY underlying** (e.g. sUSDai, thBILL), not directly to USDC. You'll likely need a follow-up swap (via BRAP/`mcp__wayfinder__execute`) to convert to stables.
- **`execute_swap` won't work** for expired markets — use `execute_convert` instead.
- **`list_active_pt_yt_markets` excludes expired markets** by default. Use `get_full_user_state_per_chain` to find positions in expired markets.
- The convert endpoint is the universal Pendle SDK entrypoint — it handles swaps, mints, redeems, LP add/remove, and rolls.

## Integration checklist (for manual execution)

If not using `execute_swap()`, you still need:
- Token decimals + raw unit conversion (TokenClient / TokenAdapter)
- ERC20 approval execution for `tokenApprovals` (wallet provider / token tx helper)
- Transaction broadcast + receipt handling (wallet provider)
- Ledger recording (LedgerAdapter) if you want bookkeeping
