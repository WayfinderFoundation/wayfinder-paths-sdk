# Building a custom harnessed strategy (`jobs_v1`) without the Strategy Lab handoff

When the owner describes a rule set over bars on Hyperliquid perps and wants evidence ("backtest it", "show me how it would have done"), build a harnessed job yourself. Do not read the engine or simulator source to work it out; this page is the contract.

## The five calls

1. **Create the job with its data contract in one call:**
   `core_jobs(action="create", job_id="<id>", name="…", goal="…", script="workspace/src/strategy.py", interval_seconds=<bar seconds>, execution_contract="jobs_v1", symbols=["BTC"], bar_interval="5m", agent_mode="intervene")`
   `symbols` + `bar_interval` seed `execution_spec.data_contract` (Hyperliquid perps, completed bars, next-bar-open fills) and sensible `execution_params` (10,000 USD paper capital, 4.5 bps fee, 3.5 bps slippage, a lookback window). Override any of those through `execution_params`. The job is created paused.
2. **Write the strategy module** at the returned `script_entrypoint` (always `workspace/src/…`), see the contract below.
3. **Fetch the bars:** `core_jobs(action="fetch_dataset", job_id="<id>", days=<n>)` — pulls the declared symbols at the declared interval into `results/backtest/input_bars.json` (60 days of 5m bars is plenty; the sandbox has the API).
4. **Backtest:** `core_jobs(action="backtest_job", job_id="<id>", quick_bars=2000)` for a fast pass, then without `quick_bars` for the full window. The report lands under `results/backtest/latest.json` (stats, trades, equity curve).
5. **Read it back:** `core_jobs(action="readout", job_id="<id>")` then `launch_checklist`; stop before `launch` unless the owner asked to launch.

If `fetch_dataset` fails (no API in this environment), say exactly that, show the validation and the code, and do not invent numbers.

## The strategy module contract

```python
from __future__ import annotations
from typing import Any
import pandas as pd
from wayfinder_paths.jobs.execution.primitives import ExecutionContext


class MyStrategy:
    default_params: dict[str, Any] = {"symbol": "BTC", "venue": "hyperliquid", "notional_usd": 1000.0}

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = {**self.default_params, **(params or {})}
        self.warmup_bars = 2  # bars needed before the first decision

    def precompute(self, frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        return {}  # optional derived columns per symbol; {} when decide() computes what it needs

    def decide(self, ctx: ExecutionContext) -> list[dict[str, Any]]:
        symbol = self.params["symbol"]
        if symbol in ctx.ledger.positions:   # one position at a time; the bracket owns the exit
            return []
        frame = ctx.view.symbol_frame(symbol)   # columns: timestamp, open, high, low, close (+ precompute columns)
        ...                                     # your rule on the completed bars
        close = float(frame.iloc[-1]["close"])
        return [{
            "action": "OPEN", "venue": self.params["venue"], "symbol": symbol,
            "side": "buy",                       # or "sell" for a short
            "size": round(self.params["notional_usd"] / close, 4),
            "bracket": {"stop_loss": <price>, "take_profit": <price>},   # absolute prices, evaluated on highs/lows
            "metadata": {"entry_reason": "<why>"},
        }]


def build_strategy(params: dict[str, Any] | None = None) -> MyStrategy:
    return MyStrategy(params)
```

- `ctx.view.symbol_frame(symbol)` is the completed-bar history handed to this tick (bounded by `execution_params.lookback_bars`); the last row is the latest completed bar and the fill happens at the next bar's open.
- `ctx.ledger.positions` (symbol → position with `side`, `size`, `avg_price`) is the book; `ctx.strategy_state` is a dict that persists across ticks for your own bookkeeping (a "taken today" flag, a pending setup).
- Session logic (Asia, London, New York) uses the bar timestamps in UTC; keep the windows as params so they can be tuned.
- A `CLOSE` intent (`{"action": "CLOSE", "venue", "symbol", "side": <opposite>, "size"}`) exits early; otherwise the bracket exits at the stop or the target.
- Put every number the owner named in `default_params` and quote them back in the readout.

## Fidelity to the owner's sequence

When the owner describes a sequence ("level → sweep → reclaim → gap → retracement entry"), every step is its own condition in `decide`, in that order, and the entry fires on the last step, never on the one before it: a fair value gap forming is not the entry, the price coming back into that gap is. Name each step in the code (a `_sweep`, a `_reclaim`, a `_fvg`, a `_retrace` check, or a state machine with those states) so a reader can match the code to the words, and quote the owner's numbers (buffer, R multiple, one position) back in the readout. Simplifying the rule to make it trade more is a different strategy, not the owner's.

## What the readout owes the owner

The readout's `validation` block is the truth about validation. Any refetch of the dataset (`fetch_dataset` again, a longer window, another source) or any edit to the strategy stales the validation stamp: run `validate_job` again before `readout`, and never say validation passes unless the readout you are quoting says so. Whether a backtest exists, the window and symbols it covered, the trade count, the net return and max drawdown from `results/backtest/latest.json`, and what is missing before paper or live. Numbers come only from that report.
