"""The default harnessed (jobs_v1) execution contract.

One place for the Hyperliquid-perps, completed-bars, next-bar-open shape the
catalog starters and agent-built strategies share, so a custom build gets the
same data contract a starter does from a single `create` call.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from wayfinder_paths.jobs.execution.primitives import bar_interval_seconds

DEFAULT_LOOKBACK_BARS = 400
_INTERVAL_LABELS = ((86_400, "d"), (3_600, "h"), (60, "m"))


def interval_label(seconds: int) -> str:
    """Seconds → the bar label the data contract uses (300 → 5m, 3600 → 1h)."""
    for unit, suffix in _INTERVAL_LABELS:
        if seconds >= unit and seconds % unit == 0:
            return f"{seconds // unit}{suffix}"
    raise ValueError(f"no bar interval label for {seconds} seconds")


def harnessed_execution_spec(
    symbols: Sequence[str],
    bar_interval: str,
    *,
    features: Sequence[Mapping[str, Any]] | None = None,
    robustness_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if bar_interval_seconds(bar_interval) is None:
        raise ValueError(f"unsupported bar interval: {bar_interval!r}")
    if not symbols:
        raise ValueError("a harnessed job needs at least one symbol")
    data_contract: dict[str, Any] = {
        "candles_source": "sdk_only",
        "no_external_ccxt": True,
        "rate_limit_safe": True,
        "bar_interval": bar_interval,
        "symbols": [str(s) for s in symbols],
        "max_bar_age_intervals": 2,
        "stale_policy": "skip",
    }
    if features:
        data_contract["features"] = [copy.deepcopy(dict(f)) for f in features]
    validation: dict[str, Any] = {"mode": "strict", "require_scenarios": False}
    if robustness_plan:
        validation["robustness_plan"] = copy.deepcopy(dict(robustness_plan))
    return {
        "market_kind": "perp",
        "view_type": "completed_bars",
        "bar_model": "completed_only",
        "fill_model": "next_bar_open",
        "ohlc_rules": {
            "use_high_low_for_stops": True,
            "allow_close_only_entries": False,
            "same_bar_fill": False,
            "same_bar_policy": "conservative",
        },
        "data_contract": data_contract,
        "validation": validation,
        "venues": ["hyperliquid"],
    }


def harnessed_execution_params(
    symbols: Sequence[str],
    *,
    lookback_bars: int = DEFAULT_LOOKBACK_BARS,
    initial_capital: float = 10_000.0,
    leverage: int | float | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "symbols": [str(s) for s in symbols],
        "venue": "hyperliquid",
        "initial_capital": float(initial_capital),
        "fee_bps": 4.5,
        "slippage_bps": 3.5,
        "min_trade_notional": 25.0,
        # The live driver hands decide() a sliding window of this many
        # completed bars; smaller than the strategy's warmup means it never
        # trades.
        "lookback_bars": int(lookback_bars),
    }
    if leverage is not None:
        params["leverage"] = leverage
    return params
