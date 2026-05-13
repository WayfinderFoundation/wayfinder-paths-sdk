# Polymarket collateral: pUSD (deposit / withdraw preparation)

This page covers **collateral conversion** — moving between Polygon USDC / USDC.e and pUSD on the **owner EOA**. For funding the per-user **deposit wallet** (the actual trading address under V2), see `rules/deposit-wallet.md`. A full trade lifecycle uses both flows.

## Key requirement

- Polymarket V2 CLOB trading collateral is **pUSD** on Polygon:
  - `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` (proxy, 6 decimals)
- Polygon **USDC.e** is the direct wrap asset for pUSD:
  - `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` (6 decimals)
- Native Polygon **USDC** must first be converted to **USDC.e** before wrapping.
  - `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359`

## MCP shortcuts (Claude Code)

- Prepare collateral for trading: `mcp__wayfinder__polymarket_execute(action="bridge_deposit", wallet_label="main", amount=10)`
- Unwind collateral back to Polygon native USDC: `mcp__wayfinder__polymarket_execute(action="bridge_withdraw", wallet_label="main", amount_usdce=10)`
- Monitor bridge status: `mcp__wayfinder__polymarket_read(action="bridge_status", wallet_label="main")`

## Adapter behavior

The adapter prepares Polymarket V2 collateral like this:

- Polygon **USDC.e** → **pUSD** via the Polymarket onramp
- Polygon native **USDC** → **USDC.e** via BRAP, then wrap to **pUSD**
- Other supported source assets / chains → Polymarket Bridge deposit flow (async)

For withdrawals:

- **pUSD** → **USDC.e** via the Polymarket offramp
- **pUSD** → **USDC.e** → Polygon native **USDC** via BRAP
- Other supported destination assets / chains → Polymarket Bridge withdraw flow (async)

If BRAP quoting/execution fails (no route / API error), the adapter falls back to the **Polymarket Bridge** deposit/withdraw flow.

### Polygon native USDC → pUSD (deposit)

```python
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.polymarket_adapter.adapter import PolymarketAdapter
from wayfinder_paths.core.constants.polymarket import POLYGON_CHAIN_ID, POLYGON_USDC_ADDRESS

adapter = await get_adapter(PolymarketAdapter, wallet_label="main")
ok, res = await adapter.bridge_deposit(
    from_chain_id=POLYGON_CHAIN_ID,
    from_token_address=POLYGON_USDC_ADDRESS,
    amount=10.0,
    recipient_address="0xYourWallet",  # usually the same wallet that is sending
)
```

### pUSD → Polygon native USDC (withdraw)

`bridge_withdraw()` starts from Polymarket collateral (`pUSD`) and will unwrap to `USDC.e` internally before delivering the requested destination asset.

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

- If the result has `method="pusd_wrap"`, `method="pusd_unwrap"`, `method="brap_then_wrap"`, or `method="unwrap_then_brap"`, the conversion completed through direct on-chain steps.
- If the result has `method="polymarket_bridge"`, the conversion is **asynchronous**; use `bridge_status(address=...)` and/or poll balances until it completes.

## Alternative: Polymarket Bridge (explicit)

If you want to force the bridge-style flow (even when BRAP could work), use the Polymarket Bridge endpoints directly:

- `bridge_deposit_addresses(...)` + ERC20 transfer (supported asset → deposit address)
- `bridge_withdraw_addresses(...)` + ERC20 transfer (USDC.e → withdraw address)
