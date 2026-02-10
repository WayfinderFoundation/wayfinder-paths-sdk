# Hyperliquid gotchas

## Minimum amounts

| Type         | Minimum          | Notes                                      |
| ------------ | ---------------- | ------------------------------------------ |
| Deposit      | $5 USD           | Deposits below this threshold are **lost** |
| Order (perp) | $10 USD notional | Applies to all perp markets                |
| Order (spot) | $10 USD notional | Applies to all spot markets                |

Constants available in `wayfinder_paths.core.constants.hyperliquid`:

- `MIN_DEPOSIT_USD = 5.0`
- `MIN_ORDER_USD_NOTIONAL = 10.0`

## HIP-3 dex abstraction

Trading on HIP-3 dexes (xyz, flx, vntl, hyna, km, etc.) requires **dex abstraction** enabled on the account. Without it, orders on non-default dexes fail with "Insufficient margin" or similar errors.

- The adapter auto-enables this before `place_market_order`, `place_limit_order`, and `place_trigger_order` via `ensure_dex_abstraction(address)`.
- One-time on-chain action per account — once enabled, it stays enabled.
- HIP-3 asset IDs use offsets: first builder dex starts at 110000, then 120000, 130000, etc.
- HIP-3 coin names are prefixed: `xyz:NVDA`, `vntl:SPACEX`, `hyna:BTC`, etc.

## Asset ID conventions

- Perp assets: `asset_id < 10000`
- Spot assets: `asset_id >= 10000`

Spot "index" is usually: `spot_index = spot_asset_id - 10000`.

## Spot trading gotchas

**Available spot pairs are limited.** Common assets like BTC and ETH are NOT directly available. Instead:

- Use `UBTC/USDC` for wrapped BTC
- Use `UETH/USDC` for wrapped ETH
- `HYPE/USDC` is native and available
- `PURR/USDC` is the OG spot pair (index 0)

**Coin name resolution:** The MCP tool resolves `coin="HYPE"` to `HYPE/USDC`. If you need a different quote (e.g., `HYPE/USDH`), use `asset_id` directly.

**`is_spot` must be explicit:** When using `hyperliquid_execute(action="place_order", ...)`:

- `is_spot=True` for spot orders
- `is_spot=False` for perp orders
- Omitting `is_spot` returns an error

**Spot orders don't use leverage:**

- `usd_amount` is always treated as notional (no `usd_amount_kind` required)
- `leverage` and `reduce_only` are ignored for spot

**Spot balance location:** Spot tokens live in your spot wallet, separate from perp margin. Use `spot_to_perp_transfer` / `perp_to_spot_transfer` to move USDC between them.

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
