"""The default harnessed (jobs_v1) execution contract.

One place for the completed-bars, next-bar-open shape the catalog starters
and agent-built strategies share, so a custom build gets the same data
contract a starter does from a single `create` call — on Hyperliquid perps,
on-chain spot tokens, or Hyperliquid spot pairs.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from wayfinder_paths.jobs.execution.hyperliquid_spot import is_spot_pair
from wayfinder_paths.jobs.execution.primitives import bar_interval_seconds
from wayfinder_paths.jobs.execution.token_bars import is_token_symbol
from wayfinder_paths.quant.pattern_match_context import SUPPORTED_INTERVALS

DEFAULT_LOOKBACK_BARS = 400
# What a harnessed job trades: the market kind the engine simulates and the
# taker costs pinned on the job. Spot venues are long-only and take no
# leverage; the engine refuses shorts, brackets and limit orders there.
HARNESSED_VENUES: dict[str, dict[str, Any]] = {
    "hyperliquid": {"market_kind": "perp", "fee_bps": 4.5, "slippage_bps": 3.5},
    "onchain": {"market_kind": "spot", "fee_bps": 30.0, "slippage_bps": 50.0},
    "hyperliquid_spot": {"market_kind": "spot", "fee_bps": 7.0, "slippage_bps": 10.0},
}
_INTERVAL_LABELS = ((86_400, "d"), (3_600, "h"), (60, "m"))


def interval_label(seconds: int) -> str:
    """Seconds → the bar label the data contract uses (300 → 5m, 3600 → 1h)."""
    for unit, suffix in _INTERVAL_LABELS:
        if seconds >= unit and seconds % unit == 0:
            return f"{seconds // unit}{suffix}"
    raise ValueError(f"no bar interval label for {seconds} seconds")


def _venue_profile(venue: str) -> dict[str, Any]:
    profile = HARNESSED_VENUES.get(str(venue))
    if profile is None:
        raise ValueError(
            f"unknown harnessed venue {venue!r}; one of {', '.join(HARNESSED_VENUES)}"
        )
    return profile


def _check_symbols_for_venue(
    symbols: Sequence[str], venue: str, bar_interval: str
) -> None:
    if venue == "onchain":
        if bar_interval not in SUPPORTED_INTERVALS:
            raise ValueError(
                f"the on-chain data source serves {'|'.join(SUPPORTED_INTERVALS)} bars; "
                f"got {bar_interval!r}"
            )
        for symbol in symbols:
            if not is_token_symbol(symbol):
                raise ValueError(
                    f"{symbol!r} is not a token id: an onchain job trades "
                    "<coingecko_id>-<chain_code> (ethereum-robinhood) or "
                    "<chain_code>_<address>"
                )
    if venue == "hyperliquid_spot":
        for symbol in symbols:
            if not is_spot_pair(symbol):
                raise ValueError(
                    f"{symbol!r} is not a spot pair: a hyperliquid_spot job trades "
                    "<BASE>/<QUOTE> pairs (HYPE/USDC)"
                )


def harnessed_execution_spec(
    symbols: Sequence[str],
    bar_interval: str,
    *,
    venue: str = "hyperliquid",
    features: Sequence[Mapping[str, Any]] | None = None,
    robustness_plan: Mapping[str, Any] | None = None,
    token_resolution: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    profile = _venue_profile(venue)
    if bar_interval_seconds(bar_interval) is None:
        raise ValueError(f"unsupported bar interval: {bar_interval!r}")
    if not symbols:
        raise ValueError("a harnessed job needs at least one symbol")
    _check_symbols_for_venue(symbols, venue, bar_interval)
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
    if token_resolution:
        data_contract["token_resolution"] = {
            str(symbol): dict(pinned) for symbol, pinned in token_resolution.items()
        }
    validation: dict[str, Any] = {"mode": "strict", "require_scenarios": False}
    if robustness_plan:
        validation["robustness_plan"] = copy.deepcopy(dict(robustness_plan))
    return {
        "market_kind": profile["market_kind"],
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
        "venues": [venue],
    }


def harnessed_execution_params(
    symbols: Sequence[str],
    *,
    venue: str = "hyperliquid",
    lookback_bars: int = DEFAULT_LOOKBACK_BARS,
    initial_capital: float = 10_000.0,
    leverage: int | float | None = None,
) -> dict[str, Any]:
    profile = _venue_profile(venue)
    if leverage is not None and profile["market_kind"] == "spot":
        raise ValueError(f"{venue} is a spot venue: it takes no leverage")
    params: dict[str, Any] = {
        "symbols": [str(s) for s in symbols],
        "venue": venue,
        "initial_capital": float(initial_capital),
        "fee_bps": float(profile["fee_bps"]),
        "slippage_bps": float(profile["slippage_bps"]),
        "min_trade_notional": 25.0,
        # The live driver hands decide() a sliding window of this many
        # completed bars; smaller than the strategy's warmup means it never
        # trades.
        "lookback_bars": int(lookback_bars),
    }
    if leverage is not None:
        params["leverage"] = leverage
    if profile["market_kind"] == "perp":
        # A live perp entry gets a venue-side stop the engine confirms before
        # the position may stay open; spot brokers have no such order type.
        params["native_stop_required"] = True
    return params
