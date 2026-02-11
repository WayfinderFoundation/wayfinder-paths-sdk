# Polymarket collateral: USDC.e (bridge conversion)

## Key requirement

- Polymarket CLOB trading collateral is **USDC.e** on Polygon:
  - `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` (6 decimals)
- Native Polygon USDC is **not** accepted as CLOB collateral:
  - `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359`

## MCP shortcuts (Claude Code)

- Convert **USDC → USDC.e**: `mcp__wayfinder__polymarket_execute(action="bridge_deposit", wallet_label="main", amount=10)`
- Convert **USDC.e → USDC**: `mcp__wayfinder__polymarket_execute(action="bridge_withdraw", wallet_label="main", amount_usdce=10)`
- Monitor bridge status: `mcp__wayfinder__polymarket(action="bridge_status", wallet_label="main")`

## Recommended conversion: BRAP swap (fallback to Polymarket Bridge)

The adapter implements conversion with a **preferred BRAP swap** on Polygon:

- USDC (native Polygon) → **USDC.e** (bridged)
- **USDC.e** → USDC (native Polygon)

If BRAP quoting/execution fails (no route / API error), it falls back to the **Polymarket Bridge** deposit/withdraw flow.

### USDC → USDC.e (deposit)

```python
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.polymarket_adapter.adapter import PolymarketAdapter
from wayfinder_paths.core.constants.polymarket import POLYGON_CHAIN_ID, POLYGON_USDC_ADDRESS

adapter = get_adapter(PolymarketAdapter, wallet_label="main")
ok, res = await adapter.bridge_deposit(
    from_chain_id=POLYGON_CHAIN_ID,
    from_token_address=POLYGON_USDC_ADDRESS,
    amount=10.0,
    recipient_address="0xYourWallet",  # usually the same wallet that is sending
)
```

### USDC.e → USDC (withdraw)

```python
from wayfinder_paths.core.constants.polymarket import POLYGON_CHAIN_ID, POLYGON_USDC_ADDRESS

ok, res = await adapter.bridge_withdraw(
    amount_usdce=10.0,
    to_chain_id=POLYGON_CHAIN_ID,
    to_token_address=POLYGON_USDC_ADDRESS,
    recipient_addr="0xYourWallet",  # must match where you want USDC delivered
)
```

### Monitoring (important)

- If the result has `method="brap"`, the conversion is a normal on-chain swap (no bridge status needed).
- If the result has `method="polymarket_bridge"`, the conversion is **asynchronous**; use `bridge_status(address=...)` and/or poll balances until it completes.

## Alternative: Polymarket Bridge (explicit)

If you want to force the bridge-style conversion (even when BRAP could work), use the Polymarket Bridge endpoints directly:

- `bridge_deposit_addresses(...)` + ERC20 transfer (USDC → deposit address)
- `bridge_withdraw_addresses(...)` + ERC20 transfer (USDC.e → withdraw address)
