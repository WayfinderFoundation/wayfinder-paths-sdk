"""Observed 24-hour two-leg positioning markouts, not trade execution."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from wayfinder_paths.quant.pattern_match_outcomes import (
    PatternOutcome,
    funding_return_between,
)
from wayfinder_paths.quant.pattern_match_positioning import POSITIONING_HORIZON_BARS
from wayfinder_paths.quant.pattern_match_universe import INTERVAL


@dataclass(frozen=True)
class PositioningOutcome(PatternOutcome):
    hedge_entry_price: float | None = None


def resolve_positioning_outcome(
    bars: pd.DataFrame,
    funding: pd.DataFrame,
    benchmark: pd.DataFrame,
    benchmark_funding: pd.DataFrame,
    *,
    entry_time: pd.Timestamp,
    as_of: pd.Timestamp,
    direction: int,
    hedge_beta: float,
    cost_bps: float,
) -> PositioningOutcome:
    """Open-labeled bars; freeze hedge beta and measure per gross entry dollar.

    The caller sets entry_time to signal time + 15 minutes. Missing prices or
    funding keep the result unresolved; neither leg is silently dropped.
    """
    if (
        direction not in {-1, 1}
        or not np.isfinite([hedge_beta, cost_bps]).all()
        or not -3 <= hedge_beta <= 3
        or cost_bps < 0
    ):
        raise ValueError("Invalid positioning outcome terms")
    exit_time = entry_time + POSITIONING_HORIZON_BARS * INTERVAL
    through = min(as_of, exit_time)
    count = max(0, int((through - entry_time) // INTERVAL))
    if count == 0:
        return PositioningOutcome("pending")
    expected = pd.date_range(entry_time, periods=count, freq=INTERVAL)
    legs = []
    for frame in (bars, benchmark):
        leg = frame[
            (frame.timestamp >= entry_time) & (frame.timestamp + INTERVAL <= through)
        ]
        if (
            not pd.DatetimeIndex(leg.timestamp).equals(expected)
            or not np.isfinite(leg[["open", "close"]]).all().all()
            or not leg[["open", "close"]].gt(0).all().all()
        ):
            return PositioningOutcome("open", data_status="missing_prices")
        legs.append(leg)
    entry, hedge_entry = (float(leg.open.iloc[0]) for leg in legs)
    if as_of < exit_time:
        return PositioningOutcome("open", entry, hedge_entry_price=hedge_entry)
    incomes = [
        funding_return_between(
            rates,
            entry_time=entry_time,
            exit_time=exit_time,
            as_of=as_of,
            direction=1,
        )
        for rates in (funding, benchmark_funding)
    ]
    if any(income is None for income in incomes):
        return PositioningOutcome(
            "open",
            entry,
            data_status="missing_funding",
            hedge_entry_price=hedge_entry,
        )
    gross = (
        direction
        * (
            float(legs[0].close.iloc[-1]) / entry
            - 1
            - hedge_beta * (float(legs[1].close.iloc[-1]) / hedge_entry - 1)
        )
        / (1 + abs(hedge_beta))
    )
    asset_income, hedge_income = incomes
    assert asset_income is not None and hedge_income is not None
    income = (
        direction * (asset_income - hedge_beta * hedge_income) / (1 + abs(hedge_beta))
    )
    return PositioningOutcome(
        status="closed",
        entry_price=entry,
        hedge_entry_price=hedge_entry,
        exit_time=exit_time,
        gross_return=gross,
        funding_return=income,
        net_return=gross + income - cost_bps / 10_000,
    )
