# Polymarket Adapter

Adapter for Polymarket market discovery (Gamma), market data + history (CLOB), user activity (Data API), and trading/redemption on Polygon.

- **Type**: `POLYMARKET`
- **Module**: `wayfinder_paths.adapters.polymarket_adapter.adapter.PolymarketAdapter`
- **Default chain**: Polygon mainnet (`137`)
- **Trading collateral**: **pUSD** on Polygon (`0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`, 6 decimals, proxy)

## Overview

The adapter wraps these public services:

- **Gamma API** (`https://gamma-api.polymarket.com`): markets/events metadata + search
- **CLOB API** (`https://clob.polymarket.com`): prices, orderbooks, and historic price time series
- **Data API** (`https://data-api.polymarket.com`): positions, trades, activity (useful for PnL/exposure views)
- **Bridge API** (`https://bridge.polymarket.com`): fallback helper endpoints for asynchronous Polymarket deposit/withdraw flows. On Polygon, the adapter can wrap USDC.e -> pUSD directly, or swap native Polygon USDC -> USDC.e via BRAP and then wrap to pUSD. For other supported assets/chains, it can fall back to Polymarket’s deposit/withdraw address flow, which is asynchronous and settles as pUSD on Polygon.

Trading uses the Python V2 client installed in this repo:

- `py-clob-client-v2-wayfinder` (import path: `py_clob_client_v2`)

## Assets & Identifiers

Polymarket has a few “ID types” you’ll see in responses:

- **`slug`** (Gamma): human-friendly market identifier used in URLs and `GET /markets/slug/{slug}`
- **`conditionId`** (Gamma): on-chain conditional token condition ID (used for redemption after resolution)
- **`clobTokenIds`** (Gamma): outcome token IDs used by the CLOB (use these for orderbooks, price history, and placing orders)

Collateral tokens on Polygon:

| Token | Address | Notes |
| --- | --- | --- |
| **pUSD** | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` | **Required CLOB collateral on Polymarket V2** |
| USDC.e | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` | Not accepted as CLOB collateral; the adapter can wrap it into pUSD on Polygon |
| USDC (native Polygon) | `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359` | Not accepted as CLOB collateral; the adapter can swap it to USDC.e, then wrap into pUSD |

Outcome “shares” are **not ERC20s**. They’re represented on-chain as Conditional Tokens (ERC1155 positions), and on the CLOB as **`token_id` strings** from `clobTokenIds`.

Important clarification:
- For Polymarket V2 trading, **pUSD** is the actual collateral used by the exchange.
- On Polygon, many wallets/UIs label `0x2791...` as “USDC”. In this repo we refer to it as **USDC.e** because it’s the bridged USDC token. It must be wrapped into **pUSD** before trading.
- `0x3c499c...` is native Polygon USDC and must be converted to **USDC.e**, then wrapped into **pUSD**, before trading.


## Usage

### Read-only (no wallet needed)

```python
from wayfinder_paths.adapters.polymarket_adapter.adapter import PolymarketAdapter

adapter = PolymarketAdapter()
ok, markets = await adapter.search_markets_fuzzy(query="bitcoin february 9", limit=10)
await adapter.close()
```

Most methods return `(ok: bool, data_or_error: Any | str)`.

### Trading / bridging / redemption (wallet + Polygon RPC needed)

You need:

- A configured wallet. Trading uses the Polymarket deposit wallet derived from the
  owner wallet and signs through wallet callbacks.
- A Polygon RPC URL (`strategy.rpc_urls["137"]`)
- Some native Polygon gas token for owner-wallet transactions such as pUSD funding

Convenient pattern used by repo scripts:

```python
from wayfinder_paths.adapters.polymarket_adapter.adapter import PolymarketAdapter
from wayfinder_paths.mcp.scripting import get_adapter

adapter = await get_adapter(PolymarketAdapter, wallet_label="main")  # loads `config.json`
```

## End-to-end cycle (USDC / USDC.e -> pUSD -> fund deposit wallet -> buy -> sell/redeem -> withdraw -> pUSD -> USDC.e / USDC)

Typical lifecycle for an automated agent:

1) **Acquire pUSD** on the owner EOA (Polymarket V2 collateral). If you hold Polygon USDC or USDC.e, the adapter can prepare pUSD for you during `bridge_deposit`.
2) **Search and select a market** (Gamma `public-search` / `markets`, filter for `enableOrderBook` + `acceptingOrders`).
3) **Resolve the CLOB token id** for the desired outcome (`resolve_clob_token_id`).
4) **Fund the deposit wallet** with pUSD via `fund_deposit_wallet(amount_raw=...)`. Trading happens from a per-user smart contract wallet (deposit wallet), not the owner EOA — order placement does not auto-fund.
5) **Set up trading** (idempotent, cached). `place_market_order` / `place_limit_order` / `place_prediction` call `ensure_trading_setup` automatically: deploy the deposit wallet if missing, grant pUSD + ConditionalTokens approvals through the relayer, derive CLOB API creds, sync balance allowances.
6) **Buy** outcome shares (CLOB market order BUY from the deposit wallet).
7) **Exit** either by:
   - **Selling** shares back on the orderbook (CLOB market order SELL), or
   - **Redeeming** after resolution (`redeem_positions` using the market’s `conditionId`).
8) **Withdraw** pUSD from the deposit wallet back to the owner EOA via `withdraw_deposit_wallet(amount_raw=...)` (omit `amount_raw` to drain).
9) **Convert back** to native Polygon USDC if desired (`bridge_withdraw`).

## Market discovery & search

### Recommended search flow (fuzzy → filter → fallback to trending)

1) Use Gamma full-text search via `search_markets_fuzzy()` (wraps `GET /public-search` and locally re-ranks results).

Practical note: Gamma often returns `outcomes`, `outcomePrices`, and `clobTokenIds` as JSON-encoded strings. The adapter normalizes these fields into Python lists for you.

2) Filter locally for tradability (agents should do this **every time**):

- `enableOrderBook == True`
- `clobTokenIds` is present/non-empty
- `acceptingOrders == True`
- `active == True` and `closed != True`

3) If search results aren’t tradable, fall back to “trending” via:

```python
ok, rows = await adapter.list_markets(
    closed=False,
    order="volume24hr",
    ascending=False,
    limit=50,
)
```

### Outcome selection (“YES/NO” vs multi-outcome)

Not all markets are `YES/NO`. Sports/player markets often have outcomes like player names or “Yes” may not exist.

Use `resolve_clob_token_id(market=..., outcome=...)` with either:

- `outcome="YES"` / `outcome="NO"` for binary markets
- `outcome="<exact outcome string>"` for multi-outcome
- `outcome=0` (or another index) as a robust fallback for agents

## Market data & historic time series

### Orderbook + price

```python
ok, price = await adapter.get_price(token_id=token_id, side="BUY")
ok, book = await adapter.get_order_book(token_id=token_id)
ok, books = await adapter.get_order_books(token_ids=[token_id1, token_id2])
ok, quote = await adapter.quote_market_order(token_id=token_id, side="BUY", amount=100.0)
ok, slug_quote = await adapter.quote_prediction(
    market_slug="bitcoin-above-70k-on-february-9",
    outcome="YES",
    side="BUY",
    amount=100.0,
)
```

Important distinction:

- `get_price(...)` returns the current quoted price from the CLOB API.
- `quote_market_order(...)` walks the live book and estimates the actual average execution price, worst fill price, and partial-fill depth for a market-sized trade.
- For `BUY`, `amount` is pUSD notional to spend.
- For `SELL`, `amount` is shares to sell.

### Price history (time series)

Use `get_prices_history()` directly if you already have a CLOB token id:

```python
ok, hist = await adapter.get_prices_history(
    token_id=token_id,
    interval="1d",     # or None + (start_ts/end_ts)
    fidelity=5,        # resolution hint (minutes)
)
```

Or use `get_market_prices_history()` if you only have a market slug + outcome:

```python
ok, hist = await adapter.get_market_prices_history(
    market_slug="bitcoin-above-70k-on-february-9",
    outcome="YES",
    interval="1h",
)
```

Interpretation:

- `hist["history"]` is a list of `{ "t": <unix_ts>, "p": <price> }`
- `p` is best treated as an **implied probability** (0–1)

## Getting pUSD (required trading collateral)

Polymarket V2 trades use **pUSD** as the actual CLOB collateral. The adapter can prepare pUSD from Polygon USDC or USDC.e for you.

### Funding from other chains (Base, Arbitrum, etc.)

If funds are not on Polygon, use a BRAP swap to go directly to Polygon USDC.e first, then wrap into pUSD on Polygon:

```python
# Example: Base USDC -> Polygon USDC.e via BRAP
mcp__wayfinder__execute(kind="swap", wallet_label="main", amount="10",
    from_token="usd-coin-base",
    to_token="polygon_0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
```

### Option A (recommended): BRAP swap (fast, on-chain)

The adapter’s `bridge_deposit()` / `bridge_withdraw()` methods prepare and unwind **Polymarket V2 collateral**:

- Polygon USDC -> USDC.e -> pUSD
- Polygon USDC.e -> pUSD
- pUSD -> USDC.e
- pUSD -> USDC.e -> Polygon USDC

Only run `bridge_deposit()` if you want the adapter to prepare pUSD for trading. If you already hold pUSD, you can trade directly after approvals.

If BRAP quoting/execution fails (no route / API error), it falls back to the **Polymarket Bridge** deposit/withdraw flow.

Prepare Polymarket V2 collateral from native Polygon USDC:

```python
from wayfinder_paths.core.constants.polymarket import POLYGON_CHAIN_ID, POLYGON_USDC_ADDRESS

ok, res = await adapter.bridge_deposit(
    from_chain_id=POLYGON_CHAIN_ID,
    from_token_address=POLYGON_USDC_ADDRESS,  # native Polygon USDC (0x3c499c...)
    amount=10.0,                              # human units
    recipient_address="0xYourWallet",
)
```

Unwind Polymarket V2 collateral back to native Polygon USDC:

```python
from wayfinder_paths.core.constants.polymarket import POLYGON_CHAIN_ID, POLYGON_USDC_ADDRESS

ok, res = await adapter.bridge_withdraw(
    amount_usdce=10.0,                        # human units
    to_chain_id=POLYGON_CHAIN_ID,
    to_token_address=POLYGON_USDC_ADDRESS,
    recipient_addr="0xYourWallet",
)
```

Notes:

- If the result has `method="brap"`, the adapter performed a direct Polygon swap leg only.
- If the result has `method="pusd_wrap"` or `method="pusd_unwrap"`, the adapter wrapped or unwrapped Polymarket V2 collateral directly on Polygon.
- If the result has `method="brap_then_wrap"`, the adapter swapped Polygon USDC to USDC.e via BRAP, then wrapped USDC.e into pUSD.
- If the result has `method="unwrap_then_brap"`, the adapter unwrapped pUSD into USDC.e, then swapped USDC.e to Polygon USDC via BRAP.
- If the result has `method="polymarket_bridge"`, the flow is asynchronous and uses the Polymarket bridge deposit/withdraw path; use `bridge_status(address=...)` and/or poll balances.
- `mcp__wayfinder__polymarket_execute(action="bridge_deposit", ...)` defaults `from_token_address` to native Polygon USDC (`0x3c499c...`). If your wallet only has USDC.e (`0x2791...`), override the token address or check balances first.

### Option B: Polymarket Bridge conversion (fallback)

The Polymarket Bridge fallback works by transferring tokens to bridge-generated deposit/withdraw addresses and waiting for settlement.

## Trading cycle (buy → sell/cash-out)

### 1) Fund the deposit wallet (explicit, separate from setup)

Trading uses a per-user smart contract wallet (deposit wallet) derived from the owner EOA. Fund it with pUSD before placing orders — order placement does **not** auto-fund.

```python
ok, res = await adapter.fund_deposit_wallet(amount_raw=2_000_000)  # 2.0 pUSD (6 decimals)
# returns (True, {"deposit_wallet", "amount_raw", "tx_hash"})
```

To withdraw back to the owner EOA (drain by default):

```python
ok, res = await adapter.withdraw_deposit_wallet()              # full balance
ok, res = await adapter.withdraw_deposit_wallet(amount_raw=1_000_000)  # partial
```

### 2) Deposit wallet setup (idempotent, cached)

`place_prediction()`, `place_market_order()`, and `place_limit_order()` call `ensure_trading_setup()` automatically. On the first call it:

- deploys the deposit wallet if it does not exist (relayer-mediated)
- grants pUSD ERC20 + ConditionalTokens ERC1155 approvals from the deposit wallet through the relayer, batched in one signed call
- derives CLOB API creds
- syncs CLOB balance allowances (COLLATERAL + CONDITIONAL for the order's `token_id`)

Subsequent calls short-circuit via the `_setup_complete` flag.

```python
ok, res = await adapter.ensure_trading_setup(token_id="<clob token id>")
```

Normally only used directly for preflight checks; order placement invokes it.

### 2) Place a prediction (market buy)

```python
ok, res = await adapter.place_prediction(
    market_slug="bitcoin-above-70k-on-february-9",
    outcome="YES",
    amount_collateral=2.0,  # dollar-denominated buy amount; spent as pUSD collateral under V2
)
```

Or use the lower-level market order method (BUY uses collateral amount; SELL uses shares):

```python
ok, res = await adapter.place_market_order(token_id=token_id, side="BUY", amount=2.0)
```

### 3) Cash out (sell shares)

```python
ok, res = await adapter.cash_out_prediction(
    market_slug="bitcoin-above-70k-on-february-9",
    outcome="YES",
    shares=1.0,
)
```

Practical note (important): after a BUY, there can be a **settlement lag** before shares are sellable. If you’re immediately selling back in automation, wait for the buy response’s `transactionsHashes[0]` to confirm before placing a SELL.

## Redemption cycle (resolved markets)

If you held shares through resolution, you can redeem on-chain to get collateral back.

1) Get `conditionId` from Gamma (from a market object; e.g. `get_market_by_slug()`).
2) Call `redeem_positions()`:

```python
ok, res = await adapter.redeem_positions(
    condition_id=condition_id,  # from Gamma market metadata
    holder="0xYourWallet",      # must match signing wallet
)
```

The adapter:

- Preflights possible redemption paths (`collateral`, `parentCollectionId`, `indexSets`)
- Calls ConditionalTokens `redeemPositions()`
- If payout is an “adapter collateral” wrapper token, it attempts to `unwrap()` automatically

## Search + analysis strategies (what worked in practice)

- **Always filter for tradable markets**: check `enableOrderBook`, `clobTokenIds`, `acceptingOrders`, `active`, `closed`.
- **Use event slugs for “sets of markets”**: for MVP-style questions, `get_event_by_slug()` returns all the markets in that event; then iterate and pull time series per outcome.
- **For daily markets**: search by date string (e.g. “February 9”), then locally filter slugs containing that date.
- **Rerank locally for better fuzziness**: run multiple queries (normalized variants), then rerank by similarity over `question`, `slug`, and event title (the adapter does basic reranking via `search_markets_fuzzy(rerank=True)`).
- **Find movers**: pull `prices-history` for each candidate token over a window (24h/7d/max) and compute deltas; combine with Gamma’s `volume24hr`/`liquidityNum` to avoid illiquid noise.
- **Binary markets**: prefer pulling both outcome token series; if you only pull one, `(1 - p)` is an approximation for the other side (spread/fees can make it imperfect).
- **Use Data API for “what happened”**: `get_positions`, `get_trades`, and `get_activity` are more direct than reconstructing positions from on-chain events.

## Key methods

Status snapshot:

- `get_full_user_state(account, ...)` - positions + balances + open orders + aggregated PnL (optional activity/trades)

Market discovery (Gamma):

- `list_markets`, `list_events`, `get_market_by_slug`, `get_event_by_slug`, `public_search`, `search_markets_fuzzy`

Market data (CLOB):

- `get_price`, `get_order_book`, `get_order_books`, `quote_market_order`, `quote_prediction`, `get_prices_history`, `get_market_prices_history`

User data (Data API):

- `get_positions`, `get_activity`, `get_trades`

Collateral conversion (Bridge API):

- `bridge_quote`, `bridge_deposit`, `bridge_withdraw`, `bridge_status`

Trading (authenticated CLOB):

- `deposit_wallet_address`, `fund_deposit_wallet`, `withdraw_deposit_wallet`
- `ensure_trading_setup` (idempotent; deploys deposit wallet + grants approvals + syncs CLOB state)
- `place_market_order`, `place_limit_order`, `cancel_order`, `list_open_orders`
- Convenience: `place_prediction`, `cash_out_prediction`

## Builder attribution

Optional builder attribution is supported.

- Set `system.polymarket_builder_code` in `config.json` to attach your builder code on every order.
- `config.example.json` includes the same field as `null` so users know where to put it.

Redemption (on-chain):

- `preflight_redeem`, `redeem_positions`

## Testing / Example script

This repo includes an end-to-end smoketest (dry-run by default):

```bash
poetry run python scripts/polymarket_smoketest.py
poetry run python scripts/polymarket_smoketest.py --execute
```
