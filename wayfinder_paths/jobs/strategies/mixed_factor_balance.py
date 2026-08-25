"""Long-only factor balance across crypto and HIP-3 equity/commodity perps."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import ExecutionContext
from wayfinder_paths.jobs.factors import (
    blend_factor_scores,
    cross_sectional_rank,
    panel_from_frames,
    residual_return,
)
from wayfinder_paths.jobs.indicators import (
    panel_breadth,
    realized_volatility,
    trailing_return,
)
from wayfinder_paths.jobs.strategies._starter_utils import (
    RANKING_STOP_DEFAULTS,
    add_stop_atr,
    buffered_rank_weights,
    current_rows,
    merge_params,
    stop_brackets,
)
from wayfinder_paths.jobs.strategies.portfolio import target_weights_to_intents

_CRYPTO = ["BTC", "ETH", "SOL", "HYPE", "DOGE", "AAVE", "NEAR", "ENA"]
_ONCHAIN = [
    "xyz:MU",
    "xyz:GOOGL",
    "xyz:NVDA",
    "xyz:CRCL",
    "xyz:TSLA",
    "xyz:META",
    "xyz:MSTR",
    "xyz:INTC",
    "xyz:AAPL",
    "xyz:COIN",
    "xyz:AMD",
    "xyz:AMZN",
    "xyz:GOLD",
    "xyz:SILVER",
    "xyz:CL",
    "xyz:BRENTOIL",
    "xyz:COPPER",
    "xyz:NATGAS",
    "xyz:PLATINUM",
    "xyz:PALLADIUM",
]
_EQUITY_BENCHMARK = "xyz:SP500"


def _factor_panels(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    benchmark: pd.Series,
    funding: pd.DataFrame,
    *,
    beta_bars: int,
    minimum_dollar_volume: float,
    min_assets: int,
) -> tuple[dict[str, pd.DataFrame], pd.Series]:
    valid_history = close.notna().rolling(beta_bars).sum().ge(beta_bars)
    dollar_volume = (close * volume).rolling(6, min_periods=5).sum()
    eligible = valid_history & close.notna() & dollar_volume.gt(minimum_dollar_volume)
    residuals = {
        bars: residual_return(
            close,
            benchmark,
            bars,
            beta_period=beta_bars,
        )
        for bars in (6, 18, 42)
    }
    low_volatility = close.apply(lambda series: realized_volatility(series, 6))
    factors = {
        "medium_strength": cross_sectional_rank(residuals[18], eligible),
        "slow_strength": cross_sectional_rank(residuals[42], eligible),
        "relative_value": cross_sectional_rank(residuals[42] - residuals[6], eligible),
        "low_volatility": cross_sectional_rank(-low_volatility, eligible),
    }
    smoothed_funding = funding.ewm(span=18, min_periods=6, adjust=False).mean()
    factors["carry"] = cross_sectional_rank(
        -smoothed_funding, eligible & smoothed_funding.notna()
    )

    above_average = close.div(close.rolling(18, min_periods=18).mean()).sub(1.0)
    breadth = panel_breadth(above_average.where(eligible), 0.0, min_assets=min_assets)
    broad_trend = trailing_return(benchmark, 18).gt(0.0) & breadth.ge(0.55)
    slow_trend = trailing_return(benchmark, 42).gt(0.0) & breadth.ge(0.55)
    return factors, broad_trend & slow_trend


class MixedFactorBalanceStrategy:
    """Allocate to top-ranked factor exposures while retaining a cash reserve."""

    default_params: dict[str, Any] = {
        "symbols": [*_CRYPTO, *_ONCHAIN, _EQUITY_BENCHMARK],
        "sleeves": {"crypto": _CRYPTO, "onchain": _ONCHAIN},
        "venue": "hyperliquid",
        "equity_benchmark": _EQUITY_BENCHMARK,
        "beta_bars": 42,
        "min_assets": 8,
        "minimum_dollar_volume": 1_000_000.0,
        "side_count": 2,
        "sleeve_gross": {"crypto": 0.40, "onchain": 0.50},
        "factor_weights": {
            "crypto": {
                "medium_strength": 0.35,
                "slow_strength": 0.25,
                "low_volatility": 0.25,
                "carry": 0.15,
            },
            "onchain": {
                "relative_value": 0.45,
                "slow_strength": 0.35,
                "low_volatility": 0.20,
            },
        },
        "rebalance_bars": {"crypto": 6, "onchain": 18},
        "rebalance_offset": 4,
        "rebalance_threshold": 0.025,
        "min_trade_notional": 25.0,
        **RANKING_STOP_DEFAULTS,
        "stop_atr_period": 24,
    }

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = merge_params(self.default_params, params)
        self.params["sleeves"] = {
            str(name): [str(symbol) for symbol in symbols]
            for name, symbols in dict(self.params["sleeves"]).items()
        }
        self.params["factor_weights"] = {
            str(sleeve): {
                str(factor): float(weight) for factor, weight in dict(weights).items()
            }
            for sleeve, weights in dict(self.params["factor_weights"]).items()
        }
        self.warmup_bars = max(int(self.params["beta_bars"]), 42) + 4

    def precompute(self, frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        derived = {
            symbol: pd.DataFrame(index=frame.index) for symbol, frame in frames.items()
        }
        for sleeve, symbols in self.params["sleeves"].items():
            close = panel_from_frames(frames, "close", symbols=symbols)
            if close.empty:
                continue
            volume = panel_from_frames(frames, "volume", symbols=symbols).reindex(
                index=close.index, columns=close.columns
            )
            funding = panel_from_frames(frames, "funding", symbols=symbols).reindex(
                index=close.index, columns=close.columns
            )
            benchmark = self._benchmark(frames, close, sleeve)
            if benchmark is None:
                continue
            factors, broad_trend = _factor_panels(
                close,
                volume,
                benchmark,
                funding,
                beta_bars=int(self.params["beta_bars"]),
                minimum_dollar_volume=float(self.params["minimum_dollar_volume"]),
                min_assets=int(self.params["min_assets"]),
            )
            weights = self.params["factor_weights"][sleeve]
            score = blend_factor_scores(
                {name: factors[name] for name in weights},
                weights,
            )
            if sleeve == "crypto":
                scale = pd.Series(1.0, index=score.index)
            else:
                scale = broad_trend.astype(float)
            self._attach_sleeve(derived, frames, symbols, score, scale)
        return add_stop_atr(derived, frames, period=int(self.params["stop_atr_period"]))

    def _benchmark(
        self,
        frames: Mapping[str, pd.DataFrame],
        close: pd.DataFrame,
        sleeve: str,
    ) -> pd.Series | None:
        if sleeve == "crypto":
            return close["BTC"] if "BTC" in close else None
        panel = panel_from_frames(
            frames,
            "close",
            symbols=(str(self.params["equity_benchmark"]),),
        )
        return None if panel.empty else panel.iloc[:, 0].reindex(close.index)

    @staticmethod
    def _attach_sleeve(
        derived: dict[str, pd.DataFrame],
        frames: Mapping[str, pd.DataFrame],
        symbols: Sequence[str],
        score: pd.DataFrame,
        scale: pd.Series,
    ) -> None:
        for symbol in symbols:
            frame = frames.get(symbol)
            if frame is None or symbol not in score:
                continue
            timestamps = pd.to_datetime(frame["timestamp"], utc=True)
            derived[symbol]["starter_factor_score"] = (
                score[symbol].reindex(timestamps).to_numpy()
            )
            derived[symbol]["starter_factor_scale"] = scale.reindex(
                timestamps
            ).to_numpy()

    def decide(self, ctx: ExecutionContext) -> list[dict[str, Any]]:
        if ctx.bar_index < self.warmup_bars:
            return []
        intents: list[dict[str, Any]] = []
        for sleeve, symbols in self.params["sleeves"].items():
            if not ctx.every_n_bars(
                int(self.params["rebalance_bars"][sleeve]),
                offset=int(self.params["rebalance_offset"]),
            ):
                continue
            rows = current_rows(
                ctx,
                symbols,
                required_columns=("starter_factor_score", "starter_factor_scale"),
            )
            if rows is None:
                continue
            scores = {
                symbol: float(rows[symbol]["starter_factor_score"])
                for symbol in symbols
            }
            scale = float(next(iter(rows.values()))["starter_factor_scale"])
            if not math.isfinite(scale):
                continue
            previous = {
                symbol: (
                    0.0
                    if (position := ctx.ledger.positions.get(symbol)) is None
                    else 1.0
                    if position.side == "long"
                    else -1.0
                )
                for symbol in symbols
            }
            weights = buffered_rank_weights(
                scores,
                previous,
                side_count=int(self.params["side_count"]),
                gross=float(self.params["sleeve_gross"][sleeve]) * scale,
                long_only=True,
            )
            if not weights:
                continue
            intents.extend(
                target_weights_to_intents(
                    ctx,
                    weights,
                    venue=str(self.params["venue"]),
                    rebalance_threshold=float(self.params["rebalance_threshold"]),
                    min_trade_notional=float(self.params["min_trade_notional"]),
                    brackets=stop_brackets(ctx, symbols, self.params),
                    scope=symbols,
                )
            )
        return intents


def build_strategy(params: dict[str, Any] | None = None) -> MixedFactorBalanceStrategy:
    return MixedFactorBalanceStrategy(params)
