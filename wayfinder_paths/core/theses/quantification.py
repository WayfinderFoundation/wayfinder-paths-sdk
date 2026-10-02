"""Observed-price diagnostics, not forecasts or an execution backtest."""

import hashlib
import json
from itertools import combinations
from math import isfinite

import numpy as np

from wayfinder_paths.core.theses.models import Variant
from wayfinder_paths.quant.market_metrics import max_drawdown

DAY_MS = 86_400_000
MIN_RETURNS = 14


def allocation_key(variant: Variant) -> str:
    """Same exposures can share diagnostics across budgets; prose is immaterial."""
    legs = sorted(
        (p.kind, p.instrument_id, p.direction, p.capital_bps, float(p.leverage))
        for p in variant.positions
    )
    return hashlib.sha256(json.dumps([legs, variant.cash_bps]).encode()).hexdigest()


def daily_returns(prices: dict[int, float]) -> dict[int, float]:
    # A missing day is not a zero return or a one-day multi-day move.
    return {
        t: price / prices[t - DAY_MS] - 1
        for t, price in sorted(prices.items())
        if t - DAY_MS in prices and prices[t - DAY_MS] > 0
    }


def price_metrics(prices: dict[int, float]) -> dict:
    times = sorted(prices)
    result = {"observations": len(times), "daily_returns": 0, "status": "unavailable"}
    if len(times) < 2:
        return result
    values = [prices[t] for t in times]
    if values[0] <= 0 or not all(isfinite(v) and v >= 0 for v in values):
        return {
            **result,
            "reason": "Invalid initial price or negative/non-finite equity",
        }
    returns = list(daily_returns(prices).values())
    return {
        "status": "measured" if len(returns) >= MIN_RETURNS else "limited_history",
        "observations": len(times),
        "daily_returns": len(returns),
        "start_ms": times[0],
        "end_ms": times[-1] + DAY_MS,
        "price_return": values[-1] / values[0] - 1,
        "max_observed_drawdown": max_drawdown(values),
        "annualized_daily_volatility": (
            float(np.std(returns, ddof=1) * np.sqrt(365))
            if len(returns) >= MIN_RETURNS
            else None
        ),
    }


def quantify_variants(variants: list[Variant], markets: dict[str, dict]) -> dict:
    """Align actual UTC observations; unavailable legs never silently become cash."""
    returns = {
        key: daily_returns(market.get("prices", {})) for key, market in markets.items()
    }
    correlations = []
    for left, right in combinations(sorted(markets), 2):
        times = sorted(returns[left].keys() & returns[right].keys())
        x, y = [returns[left][t] for t in times], [returns[right][t] for t in times]
        sufficient = len(times) >= MIN_RETURNS and np.std(x) > 0 and np.std(y) > 0
        correlations.append(
            {
                "left": left,
                "right": right,
                "overlapping_daily_returns": len(times),
                "correlation": float(np.corrcoef(x, y)[0, 1]) if sufficient else None,
            }
        )
    portfolios = []
    for variant in variants:
        legs = variant.positions
        prices = [markets[p.instrument_id].get("prices", {}) for p in legs]
        times = sorted(set.intersection(*(set(p) for p in prices))) if prices else []
        if times and any(p[times[0]] <= 0 for p in prices):
            times = []  # No finite entry quantity at a zero-priced outcome.
        weights = [
            p.capital_bps / 10000 * p.leverage * (-1 if p.direction == "short" else 1)
            for p in legs
        ]
        equity = {
            t: 1
            + sum(
                w * (p[t] / p[times[0]] - 1)
                for w, p in zip(weights, prices, strict=True)
            )
            for t in times
        }
        portfolios.append(
            {
                "allocation_key": allocation_key(variant),
                "budget_usd": variant.budget_usd,
                "cash_bps": variant.cash_bps,
                "gross_notional_bps": sum(p.capital_bps * p.leverage for p in legs),
                "signed_directional_notional_bps": sum(
                    p.capital_bps * p.leverage * (-1 if p.direction == "short" else 1)
                    for p in legs
                    if p.kind != "prediction"
                ),
                "prediction_capital_bps": sum(
                    p.capital_bps for p in legs if p.kind == "prediction"
                ),
                "metrics": price_metrics(equity),
                "missing_history": [
                    p.instrument_id
                    for p, series in zip(legs, prices, strict=True)
                    if not series
                ],
                "funding": [
                    {
                        "instrument_id": p.instrument_id,
                        # Positive cost = paid; negative = received. Not extrapolated.
                        "observed_cost_nav_fraction": (
                            w * markets[p.instrument_id]["funding"]["sum_rates"]
                            if markets[p.instrument_id]
                            .get("funding", {})
                            .get("sum_rates")
                            is not None
                            else None
                        ),
                    }
                    for p, w in zip(legs, weights, strict=True)
                    if p.kind in {"perp", "hip3"}
                ],
            }
        )
    return {
        "assets": {
            key: {
                **{k: v for k, v in market.items() if k != "prices"},
                "metrics": price_metrics(market.get("prices", {})),
            }
            for key, market in markets.items()
        },
        "correlations": correlations,
        "portfolios": portfolios,
        "method": (
            "Fixed initial signed notionals, idle cash held at $1; NO shares are long their own outcome price. "
            "Portfolio uses only common observed UTC daily points, no filling or proxy substitution. "
            "Volatility/correlation require 14 overlapping consecutive-day returns; annualization assumes 365 days. "
            "Observed drawdowns can miss intraday/gap losses. Gross price history excludes fees, slippage, "
            "funding, distributions, rebalancing, stops and liquidations. Funding is a separate observed "
            "7-day constant-notional cost, with coverage reported. These are diagnostics, not forecasts, "
            "expected returns, a probability edge, or the execution-aware dashboard backtest."
        ),
    }
