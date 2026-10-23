"""Observed-price diagnostics, not forecasts or an execution backtest."""

import hashlib
import json
from itertools import combinations
from math import isfinite
from typing import Any

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


def quantify_variants(
    variants: list[Variant], markets: dict[str, dict], *, align_portfolios: bool = False
) -> dict:
    """Align actual UTC observations; unavailable legs never silently become cash."""
    returns = {
        key: daily_returns(market.get("prices", {})) for key, market in markets.items()
    }
    correlations: list[dict[str, Any]] = []
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
    # Correlation is independent of budget/weight. Share signed pairs across variants.
    by_pair = {(row["left"], row["right"]): row for row in correlations}
    position_pairs = {
        tuple(
            sorted(
                (
                    (left.instrument_id, left.direction),
                    (right.instrument_id, right.direction),
                )
            )
        )
        for variant in variants
        for left, right in combinations(variant.positions, 2)
    }
    position_correlations = []
    for (left, left_direction), (right, right_direction) in sorted(position_pairs):
        row = by_pair[left, right]
        sign = -1 if (left_direction == "short") != (right_direction == "short") else 1
        position_correlations.append(
            {
                **row,
                "left_direction": left_direction,
                "right_direction": right_direction,
                "correlation": row["correlation"] * sign
                if row["correlation"] is not None
                else None,
            }
        )
    comparison_prices = [
        markets[p.instrument_id].get("prices", {})
        for v in variants
        for p in v.positions
    ]
    common_times = (
        sorted(set.intersection(*(set(p) for p in comparison_prices)))
        if comparison_prices
        else []
    )
    if common_times and any(p[common_times[0]] <= 0 for p in comparison_prices):
        common_times = []
    portfolios = []
    for variant in variants:
        legs = variant.positions
        prices = [markets[p.instrument_id].get("prices", {}) for p in legs]
        times = sorted(set.intersection(*(set(p) for p in prices))) if prices else []
        if align_portfolios:
            times = common_times
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
        funding: list[dict[str, str | float | None]] = []
        funding_windows: set[tuple[int | None, int | None]] = set()
        net_funding_cost = 0.0
        complete_funding = True
        for p, w in zip(legs, weights, strict=True):
            if p.kind not in {"perp", "hip3"}:
                continue
            observation = markets[p.instrument_id].get("funding", {})
            rate: float | None = observation.get("sum_rates")
            cost = w * rate if rate is not None else None
            funding.append(
                {
                    "instrument_id": p.instrument_id,
                    "observed_cost_nav_fraction": cost,
                    "observed_cost_nav_pct": cost * 100 if cost is not None else None,
                }
            )
            if cost is not None:
                net_funding_cost += cost
            funding_windows.add(
                (observation.get("start_ms"), observation.get("end_ms"))
            )
            if (
                rate is None
                or not observation.get("expected_hours")
                or observation.get("observed_hours") != observation["expected_hours"]
            ):
                complete_funding = False
        start, end = (
            next(iter(funding_windows)) if len(funding_windows) == 1 else (None, None)
        )
        complete_funding = complete_funding and start is not None and end is not None
        reported_cost = net_funding_cost if complete_funding or not funding else None
        net_flow = None
        if complete_funding:
            if net_funding_cost > 0:
                net_flow = "paid"
            elif net_funding_cost < 0:
                net_flow = "received"
            else:
                net_flow = "zero"
        portfolios.append(
            {
                "allocation_key": allocation_key(variant),
                "budget_usd": variant.budget_usd,
                "cash_bps": variant.cash_bps,
                "largest_capital_bps": max((p.capital_bps for p in legs), default=0),
                "capital_concentration_hhi": sum(
                    (p.capital_bps / 10000) ** 2 for p in legs
                ),
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
                "funding": funding,
                "funding_summary": {
                    "status": (
                        "not_applicable"
                        if not funding
                        else "measured"
                        if complete_funding
                        else "unavailable"
                    ),
                    # Do not turn missing hours or mismatched windows into net carry.
                    "observed_cost_nav_fraction": reported_cost,
                    "observed_cost_nav_pct": (
                        reported_cost * 100 if reported_cost is not None else None
                    ),
                    "observed_net_flow": net_flow,
                    "start_ms": start if complete_funding else None,
                    "end_ms": end if complete_funding else None,
                },
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
        "correlation_basis": "instrument_price_returns_before_position_direction",
        "correlations": correlations,
        "position_correlations": position_correlations,
        "portfolios": portfolios,
        "method": (
            "Fixed initial signed notionals, idle cash held at $1; NO shares are long their own outcome price. "
            "Correlations use instrument price returns, not signed position PnL: negate a pair's correlation "
            "when exactly one leg is short; do not invert a NO share's own price series. "
            "position_correlations share the supplied portfolios' direction-adjusted instrument daily returns; "
            "these are not portfolio beta or net PnL correlation. "
            "Portfolio uses only common observed UTC daily points, no filling or proxy substitution. "
            "Volatility/correlation require 14 overlapping consecutive-day returns; annualization assumes 365 days. "
            "Observed drawdowns can miss intraday/gap losses. Gross price history excludes fees, slippage, "
            "funding, distributions, rebalancing, stops and liquidations. Funding is a separate observed "
            "7-day constant-notional cost, with coverage reported. Funding costs are already NAV-weighted "
            "including leverage/direction: positive is paid, negative is received; do not apply weights again. "
            "observed_cost_nav_pct is the display percentage (fraction times 100), not another rate; "
            "observed_net_flow labels the measured net payment/receipt, not a forecast. "
            "funding_summary nets complete, matching windows only; not total holding costs or a forecast. "
            "These are diagnostics, not forecasts, "
            "expected returns, a probability edge, or the execution-aware dashboard backtest."
        ),
    }
