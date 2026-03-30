# Gotchas

## Quick cheat sheet

| Wrong | Right | Why |
|-------|-------|-----|
| `prices_df.columns = condition_ids` | `prices_df.columns = token_ids` | Prices are indexed by CLOB token ID, not condition_id |
| `resolution_prices = {token_id: 1.0}` | `resolution_prices = {condition_id: 1.0}` | Backtester's `resolution_prices` keys are condition_ids |
| `prices_df.loc[ts, condition_id]` | `prices_df.loc[ts, token_id]` | Prices columns are token_ids |
| `trades_df.index` is naive datetime | `trades_df.index` must be UTC-aware | Backtester checks timezone; mismatch → KeyError |
| Mix `token_id` and numeric string | Keep as str, never cast to int | CLOB token IDs are 77-digit integers stored as strings |
| `from strategies/.../parser import TradeSignal` | `from core/backtesting/polymarket_parser import TradeSignal` | `TradeSignal` lives in core |

---

## 1. Three different IDs — never mix them

Polymarket uses three independent identifiers that look similar:

| ID | Example | Used for |
|----|---------|----------|
| `condition_id` | `"0x1a2b3c..."` (hex, 66 chars) | On-chain redemption; `resolution_prices` keys |
| `token_id` | `"71321045..."` (decimal, 77 chars) | CLOB trading; `prices_df` columns |
| `market_slug` | `"trump-wins-2024"` | Human display; search |

**Backtester uses `condition_id` for `resolution_prices` and `markets_won/lost` stats; `token_id` for everything else (`prices_df` columns, `positions` dict).**

---

## 2. `prices_df` must cover ALL token_ids in `trades_df`

If a token_id in `trades_df` is not a column in `prices_df`, positions in that market will produce NaN equity.

```python
# Always derive token_ids from trades_df
token_ids = trades_df["token_id"].unique().tolist()
prices_df = await fetch_market_prices(token_ids, start, end, adapter=adapter)
```

---

## 3. Date ranges must match between `trades_df` and `prices_df`

Use the same `start_date` and `end_date` for both calls. Trades outside the price grid are dropped by the backtester (ceil snapping).

---

## 4. Empty `trades_df` is valid — don't crash

If a wallet made no trades in the date window, `fetch_wallet_trades` returns an empty DataFrame (all columns present, zero rows). The backtester handles this correctly — returns flat equity at `initial_capital`.

---

## 5. NaN prices during active positions → NaN equity

If `prices_df` has a NaN at timestamp `ts` for a token you hold, `equity_curve.loc[ts]` will be NaN. This signals a data gap, not a bug.

---

## 6. `brier_score` and `market_win_rate` are NaN when no markets have resolved

Always check before displaying:

```python
import math
bs = result.stats["brier_score"]
print(f"Brier: {bs:.3f}" if not math.isnan(bs) else "Brier: N/A")
```

---

## 7. Polymarket fee model (2% of potential winnings, side-dependent)

The backtester uses the real Polymarket fee formula from `adapters/polymarket_adapter/fees.py`:

```python
from wayfinder_paths.adapters.polymarket_adapter.fees import polymarket_fee_rate

# BUY fee = 0.02 × (1 - price) — fee on potential profit
polymarket_fee_rate(0.9, "BUY")   # 0.02 * 0.1 = 0.002 (0.2%)
polymarket_fee_rate(0.1, "BUY")   # 0.02 * 0.9 = 0.018 (1.8%)

# SELL fee = 0.02 × price — fee on proceeds
polymarket_fee_rate(0.9, "SELL")  # 0.02 * 0.9 = 0.018 (1.8%)
polymarket_fee_rate(0.1, "SELL")  # 0.02 * 0.1 = 0.002 (0.2%)
```

The `config.fee_rate` field is **not used** by the backtester — fees come from `polymarket_fee_rate()` directly.

---

## 8. Slippage delay model (default: on)

The backtester models copy-trade execution delay via `config.slippage_delay` (default 0.5):

- When the next grid price moves **against** us (higher for BUY, lower for SELL), the execution price blends toward it: `exec = (1-d) × signal_price + d × next_price`
- When the next price is **better** or the same, falls back to flat `config.slippage_rate`

Set `slippage_delay=None` to disable and use only flat `slippage_rate`.

---

## 9. Price grid convention: "strictly before"

`prices_df` row at time `t` contains the last observation **strictly before** `t`. This means:
- A price observed at 10:35 appears in the 11:00 row (not 10:00)
- A price observed at exactly 10:00 appears in the 11:00 row (not 10:00)
- The first grid row is always NaN (no prior observation)

Trades are snapped to the first grid point **strictly after** their timestamp (ceil + boundary guard).

---

## 10. `TradeSignal` and `parse_activity` live in core

```python
# Right — import from core
from wayfinder_paths.core.backtesting.polymarket_parser import TradeSignal, parse_activity

# Also works — re-exported for strategy-internal use
from wayfinder_paths.strategies.polymarket_copy_strategy.parser import TradeSignal
```
