# Boros gotchas (read this before trading)

## Withdrawals have cooldowns

Boros withdrawals can be a **two-step** process:
- request a withdrawal
- wait out a cooldown
- finalize the withdrawal

In orchestration code:
- treat “pending withdrawals” as a hard stop for redeploying funds
- build explicit “wait + finalize” steps with timeouts

Reference (withdrawal status + cooldown):
- https://docs.pendle.finance/boros-dev/Contracts/MarketHub

## Units are not uniform

Different Boros calls use different unit conventions:
- `withdraw_collateral`: **native token decimals** (explicitly documented in adapter)
- `deposit_to_cross_margin`: `amount_wei` is **native decimals** for the collateral token
- `cash_transfer`: uses **1e18 internal cash units** (documented in adapter)
- Order sizing uses “YU wei” in several paths (see adapter docs and helper functions)

YU note (critical for sizing):
- For **HYPE-collateral** markets (token_id=5), treat **1 YU ≈ 1 HYPE** exposure.
- For **USDT-collateral** markets (token_id=3), treat **1 YU ≈ $1** exposure.

Best practice:
- Resolve decimals and convert explicitly at the boundary of each call.

## Collateral vs YU sizing (critical - read before implementing)

**Collateral is margin, YU is notional rate exposure.** Your deposited collateral does **not** cap YU 1:1.

Max YU is determined by the protocol's initial/maintenance margin formula:
```
margin_required ∝ |position_size| × max(|mark_implied_APR|, thresholds) × time_to_maturity × factors
```
...and is subject to margin floors and time thresholds.

**Liquidation risk:** Positions become liquidatable when `Total Value / Net Balance` falls below maintenance margin (health ratio ≤ 1).
- **Net Balance** = Collateral + Unrealized PnL
- **Unrealized PnL** is computed off mark implied APR (TWAP)
- Collateral also changes at each settlement as fixed vs underlying funding is realized

**Implementation guidance:**
- **DO NOT** set `target_yu = deposit_amount` — this is wrong and dangerous
- Instead: compute a margin-based max size from current mark rate, time to maturity, and protocol thresholds/factors
- Apply a safety buffer (e.g., 50-70% of theoretical max) to avoid liquidation on mark APR moves
- Monitor health ratio and be prepared to reduce position or add collateral

**Example (conservative):**
```python
# Don't do this:
target_yu = collateral_hype  # WRONG - ignores margin requirements

# Do this instead:
# 1. Query current mark APR and maturity
# 2. Compute margin requirement per YU
# 3. Apply buffer
max_yu_theoretical = collateral / margin_per_yu(mark_apr, days_to_maturity)
target_yu = max_yu_theoretical * 0.6  # 60% utilization for safety
```

## Minimum cross cash requirements (MMInsufficientMinCash)

Some Boros actions require you to have a **minimum amount of cross cash** for a given
collateral token.

How it shows up:
- Gas estimation or the tx itself can revert with a custom error.
- In our case, a revert selector `0x428dfdd6` corresponded to `MMInsufficientMinCash()`.

How to check it safely:
- Query on-chain: `MarketHub.getCashFeeData(tokenId)` and read `minCashCross` (cash units, 1e18).
- In this repo: use `BorosAdapter.get_cash_fee_data(token_id=...)`.

Practical implications:
- “Minimum deposit” is **not a constant**; it can vary by token and may change over time.
- For HYPE collateral we observed `minCashCross ≈ 0.4` HYPE on Arbitrum (2026-02-01).

## Deposits can land in isolated cash (even when you asked for cross)

Even when using “cross margin” deposit calldata, Boros can end up crediting your cash as
**isolated** for the target `market_id`.

If you try to trade immediately, you may fail min-cash checks because your **cross** cash is still low.

Mitigation:
- Sweep isolated → cross via `cash_transfer(market_id=..., amount_wei=..., is_deposit=False)`.
- In this repo: `BorosAdapter.deposit_to_cross_margin(...)` now sweeps isolated → cross for that `market_id`.
- For cleanup / ad-hoc scripts: use `BorosAdapter.sweep_isolated_to_cross(token_id=..., market_id=...)`.

## Getting HYPE for Boros (don’t overcomplicate it)

To deposit HYPE collateral into Boros, you ultimately need **Arbitrum OFT HYPE** (the LayerZero OFT token) in the wallet that will deposit.

There are two common funding paths:

1) **Simple / manual path (preferred): BRAP cross-chain swap → HyperEVM native HYPE → OFT bridge → Boros**
   - Use BRAP to cross-chain swap into **native HYPE on HyperEVM (chain 999)** (route provider depends on quoting).
   - Then bridge **HyperEVM native HYPE → Arbitrum OFT HYPE** and deposit to Boros.
   - This avoids needing to touch Hyperliquid at all when you’re just funding Boros.

2) **Strategy / cost-min path (BorosHypeStrategy): Hyperliquid spot → HyperEVM → OFT → Boros**
   - If you’re already running the delta-neutral strategy, it may be cheaper/cleaner to buy HYPE on Hyperliquid spot, withdraw to HyperEVM, then OFT-bridge to Arbitrum for Boros collateral.

## OFT HYPE bridge (HyperEVM → Arbitrum)

If you fund Boros with HYPE by bridging native HYPE from HyperEVM to Arbitrum OFT HYPE:

- The OFT contract requires `msg.value = amount + nativeFee` (not just the fee).
- Amounts must be rounded down to `decimalConversionRate()` (otherwise the bridge call can revert).
- The transfer is asynchronous (LayerZero); wait for OFT HYPE to arrive on Arbitrum before depositing it to Boros.

In this repo, use `BorosAdapter.bridge_hype_oft_hyperevm_to_arbitrum(amount_wei=..., ...)`.

## Funding sign convention (don't get this backwards)

**Negative funding = shorts pay longs (longs receive)**
**Positive funding = longs pay shorts (shorts receive)**

Example interpretation:
- `floating_apr = -5.59%` → If you're **long**, you **receive** ~5.59% annualized
- `floating_apr = +3.00%` → If you're **long**, you **pay** ~3.00% annualized

This is the standard perp convention (Hyperliquid, dYdX, etc.). Don't confuse this with "negative = bad for longs".

## Settlement cadence matters (especially for rate-locking)

Funding is settled on a schedule determined by the underlying perp venue.
For example, Hyperliquid funding is settled hourly; Boros settles on the same cadence for Hyperliquid-based markets.

References:
- Boros settlement: https://pendle.gitbook.io/boros/boros-academy/understanding-funding-rates/settlement
- Hyperliquid funding: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding

Practical implications:
- sample funding and Boros rates on the same cadence (or at least label timestamps precisely)
- avoid reading "current" funding/rates mid-interval and treating them as an exact settled value

## Markets endpoint response shape + field names

Two common sources of confusion (and wasted time):

1) **`marketId` queries return lists, not a single market**
   - `GET /core/v1/markets?marketId=51` returns `{ "results": [ ... ] }` (a list).
   - Don’t assume `get_market(51)` returns a single object unless you’re using the repo’s client helper.
   - In this repo:
     - Use `BorosAdapter.get_market(market_id)` (returns a single market dict)
     - Or `BorosAdapter.quote_market_by_id(market_id)` (directly returns a `BorosMarketQuote`)

2) **Underlying symbol is not a stable top-level field**
   - The asset is typically `metadata.assetSymbol` (and sometimes encoded in `imData.symbol`).
   - Don’t hardcode ad-hoc fields like `underlying_asset`; use the adapter helpers:
     - `BorosAdapter.list_tenor_quotes(underlying_symbol="HYPE", ...)`
     - `BorosAdapter.quote_markets_for_underlying("HYPE", ...)`

Also: Boros enforces `limit <= 100` on `/markets`. Prefer `list_markets_all(page_size=100)` for discovery.

## Tick math

- APRs are derived from ticks; use the adapter helpers:
  - `tick_from_rate(rate, tick_step, round_down=...)`
  - `rate_from_tick(tick, tick_step)`
  - `normalize_apr(value)` for mixed encodings

## Calldata sequencing

Boros API may return multi-tx payloads:
- `{"calldatas": ["0x...", "0x..."]}` → must be executed sequentially to the Boros Router.

The adapter’s `_broadcast_calldata(...)` implements this sequencing; don’t “simplify” it away.
