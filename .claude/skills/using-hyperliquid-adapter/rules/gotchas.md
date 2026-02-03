# Hyperliquid gotchas

## Asset ID conventions

- Perp assets: `asset_id < 10000`
- Spot assets: `asset_id >= 10000`

Spot “index” is usually: `spot_index = spot_asset_id - 10000`.

## Spot L2 naming quirks

The adapter implements special naming for spot orderbooks:
- spot_index == 0 uses `"PURR/USDC"`
- otherwise uses `"@{spot_index}"`

If you request spot data by coin string, prefer the helper mapping from `get_spot_assets()`.

## Executor wiring

Execution is intentionally separated from data:
- Read methods work with `Info` only.
- Write methods require an executor with signing configured.

## Funding history API surface

- There is no `HyperliquidAdapter.get_funding_history(...)` method in this repo.
- Funding time-series lives in:
  - `HyperliquidDataClient.get_funding_history(...)` (Wayfinder API), or
  - the underlying SDK `Info.funding_history(...)` via `adapter.info.funding_history(...)`.

## Builder fee approvals

Hyperliquid builder fees are opt-in per **user ↔ builder** pair:
- You must approve a max builder fee via `approve_builder_fee(builder, max_fee_rate, address)` before trades can include a builder.
- The fee value `f` is in **tenths of a basis point** (e.g. `30` → `0.030%`).
- This repo attributes trades to `0xaA1D89f333857eD78F8434CC4f896A9293EFE65c` (builder wallet is fixed; other addresses are rejected).

## USD sizing: notional vs margin (collateral)

When a user asks for “a **$X bet** at **Y× leverage**”, clarify whether `$X` is:
- **notional** (position size): `margin ≈ notional / leverage`
- **margin** (collateral): `notional = margin * leverage`

Claude Code MCP:
- `hyperliquid_execute(action="place_order", usd_amount=..., usd_amount_kind="notional"|"margin", leverage=...)`
- If `usd_amount_kind="margin"`, `leverage` is required.
- If you provide `size`, it is **coin units**, not USD.

Best practice:
- Keep execution behind a single, clearly named entrypoint (strategy method or one-off `.wayfinder_runs/` script) and gate it with clear user intent + safety checks.
