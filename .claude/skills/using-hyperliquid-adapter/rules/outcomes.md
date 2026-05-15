# HIP-4 outcome markets (binary / multi-outcome prediction contracts)

HIP-4 is a hypercore-native prediction-contract surface. Phase 1 ships **binary daily markets** (e.g. "BTC > $78,213 by 06:00 UTC"); the protocol generalizes to multi-outcome later. Outcomes settle daily at **06:00 UTC**, after which the `outcome_id` rolls and old ids stop trading.

**Collateral / quote: USDH** (Hyperliquid's stablecoin, token id 360). All currently-live HIP-4 outcomes settle in USDH — `outcomeMeta` doesn't expose a per-market quote field, so treat the whole surface as USDH-only until HL deploys outcomes against another token. You need a USDH balance to place orders; a USDC balance won't be debited.

## Asset id encoding

Every outcome side has its own asset id and coin name:

```
encoding   = 10 * outcome_id + side
asset_id   = 100_000_000 + encoding   # OUTCOME_ASSET_OFFSET
book_coin  = f"#{encoding}"           # used by l2Book, trades, allMids
token_coin = f"+{encoding}"           # used by spotClearinghouseState balances
```

`side` is the integer index into `outcomeMeta.outcomes[].sideSpecs`. The current binary daily ships `sideSpecs=[Yes, No]` so **0=YES, 1=NO**, but multi-outcome markets may reorder. **Always read `sideSpecs[side].name` from `get_outcome_markets()` rather than hardcoding YES/NO.**

Helpers exported from `wayfinder_paths.adapters.hyperliquid_adapter.adapter`:

- `outcome_asset_id(outcome_id, side) -> int`
- `outcome_book_coin(outcome_id, side) -> str` (`"#<encoding>"`)
- `outcome_token_coin(outcome_id, side) -> str` (`"+<encoding>"`)

## Reads

### Adapter

- `HyperliquidAdapter.get_outcome_markets()` → `(ok, list[dict])` — one entry per live outcome with parsed `description` (underlying, expiry ISO, target price, period), `sideSpecs`, and per-side `asset_id`/`book_coin`/`token_coin`.
- Per-side order book: `adapter.get_l2_book(outcome_book_coin(oid, side))`.
- Mid prices: `adapter.get_all_mid_prices()` returns a dict keyed by `#<encoding>` for outcome sides.
- **Positions live in spot user state.** `HyperliquidAdapter.get_spot_user_state(address)` returns `balances[]` with `+<encoding>` entries — there's no dedicated outcome-positions endpoint upstream.

### MCP — `hyperliquid_get_state(label)` returns all three surfaces

`mcp__wayfinder__hyperliquid_get_state(label)` returns a single dict with `perp`, `spot`, and `outcomes` keys:

- `perp.state` — perp clearinghouse (cross/isolated margin, asset positions, withdrawable).
- `spot.state.balances` — pure spot balances (`USDC`, `HYPE`, `USDH`, …). `+N` outcome entries are filtered out into the `outcomes` bucket.
- `outcomes.positions` — outcome positions only (parsed `outcome_id` / `side`, plus `total` / `hold` / `entryNtl`).

Other reads:

- `mcp__wayfinder__hyperliquid_search_market(query=...)` — search live markets including outcomes.
- `mcp__wayfinder__hyperliquid_search_mid_prices(asset_names=["#20", ...])` — fetch outcome mid prices via the `#<encoding>` key.
- For raw L2 book data on a specific outcome side, drop into a script and call `HyperliquidAdapter.get_l2_book(outcome_book_coin(oid, side))`.

## Writes

### MCP — `hyperliquid_place_outcome_order` on a `#<encoding>` market

Outcome markets have their own MCP tool; perp/spot tools reject `#<encoding>` asset names.

**Required (market):** `wallet_label`, `asset_name` (e.g. `"#200"` for outcome_id=20, side=0), `is_buy`, `size` (int contracts) or `usd_amount`.
**Required (limit):** add `order_type="limit"` and `price`; `size` is required (no `usd_amount` for limit outcomes).

**Optional:** `slippage` (market only, default 0.01), `reduce_only`, `cloid`.

**Notes:**
- `size` is **integer contracts** (`szDecimals=0`).
- HIP-4 is **zero-fee** — no builder approval flow; the dispatcher omits builder for outcome orders.
- No leverage, no `is_spot` — outcome resolution comes from the `#<encoding>` asset name.

```python
hyperliquid_place_outcome_order(
    wallet_label="main",
    asset_name="#200",   # outcome_id=20, side=0 (YES — verify via get_outcome_markets sideSpecs)
    is_buy=True,
    size=5,
)
```

### MCP — cancel an outcome order

Use `hyperliquid_cancel_order` with the same `#<encoding>` asset name:

```python
hyperliquid_cancel_order(
    wallet_label="main",
    asset_name="#200",
    order_id=123456,
)
```

### Adapter (Python)

- `HyperliquidAdapter.place_outcome_order(outcome_id, side, is_buy, size, address, price=None, slippage=0.01, tif="Ioc"|"Gtc", reduce_only=False, cloid=None)` → `(ok, result)`.
- `HyperliquidAdapter.cancel_order(asset_id=outcome_asset_id(oid, side), order_id, address)`.

## Gotchas

- **Collateral is USDH, not USDC.** Outcome buys debit USDH (token 360). If you only hold USDC, orders fail even with plenty of buying power — swap USDC → USDH on the `USDH/USDC` spot pair first.
- **Daily settlement rolls outcome ids.** A live `outcome_id=20` at 05:55 UTC may be expired by 06:05 UTC and a new id replaces it. Re-fetch `get_outcome_markets()` rather than caching ids across days.
- **Sizes are integer contracts.** The adapter rejects non-integer `size` loudly; don't pass floats.
- **Price decimals follow the spot rule** (`MAX_DECIMALS=8`, 5-sig-figs); for typical 0..1 outcome prices this means up to ~5 decimals.
- **No builder fees.** Empirically verified across thousands of fills — the builder field is not honored on outcomes. Don't try to attach one.
- **Phase 1 is binary only**, but treat `side` as an arbitrary index into `sideSpecs` so multi-outcome works without code changes.
