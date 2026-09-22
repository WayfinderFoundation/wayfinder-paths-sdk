"""Pure Pattern Match research outcome policy; no scheduler or order execution.

Bars are open-labeled. An outcome is available at its exit bar's close. Missing
price bars or hourly funding hold resolution pending, never imply a zero return.
Bracket fills deliberately retain the frozen research's level-fill convention.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.quant.pattern_match_universe import INTERVAL, OUTCOME_HORIZON_BARS


def pattern_outcome_for_bar(
    shadow: Mapping[str, Any], row: Any
) -> tuple[str, float] | None:
    direction = int(shadow["direction"])
    entry = float(shadow["entry_price"])
    stop = float(shadow["stop_distance"])
    take = float(shadow["take_distance"])
    high, low = float(row.high) / entry - 1, float(row.low) / entry - 1
    stopped = low <= -stop if direction == 1 else high >= stop
    taken = high >= take if direction == 1 else low <= -take
    if stopped:
        return ("both_stop" if taken else "stop", -stop)
    if taken:
        return ("take", take)
    if int(shadow["bars_held"]) >= OUTCOME_HORIZON_BARS:
        return ("null", direction * (float(row.close) / entry - 1))
    return None


@dataclass(frozen=True)
class PatternOutcome:
    status: str
    entry_price: float | None = None
    exit_time: pd.Timestamp | None = None
    gross_return: float | None = None
    funding_return: float | None = None
    net_return: float | None = None
    data_status: str = "complete"


def funding_return_between(
    funding: pd.DataFrame,
    *,
    entry_time: pd.Timestamp,
    exit_time: pd.Timestamp,
    as_of: pd.Timestamp,
    direction: int,
) -> float | None:
    """Entry-notional funding approximation; missing coverage is never zero."""
    expected = pd.date_range(entry_time.floor("h"), exit_time.floor("h"), freq="h")
    # HL timestamps can be milliseconds past the hour. Validate by bucket but
    # charge the actual timestamp (entry < t <= exit), using only known rates.
    covered = funding[
        (funding.timestamp.dt.floor("h") >= entry_time.floor("h"))
        & (funding.timestamp.dt.floor("h") <= exit_time.floor("h"))
        & (funding.timestamp <= as_of)
    ]
    hours = covered.timestamp.dt.floor("h")
    if (
        hours.duplicated().any()
        or set(hours) != set(expected)
        or not np.isfinite(covered.funding_rate.to_numpy(dtype=float)).all()
    ):
        return None
    payments = covered[
        (covered.timestamp > entry_time) & (covered.timestamp <= exit_time)
    ]
    return -direction * float(payments.funding_rate.sum())


def resolve_pattern_outcome(
    bars: pd.DataFrame,
    funding: pd.DataFrame,
    *,
    entry_time: pd.Timestamp,
    as_of: pd.Timestamp,
    direction: int,
    stop_distance: float,
    take_distance: float,
    cost_bps: float,
) -> PatternOutcome:
    if direction not in (-1, 1) or min(stop_distance, take_distance) <= 0:
        raise ValueError("Invalid Pattern Match bracket")
    selected = bars[
        (bars.timestamp >= entry_time) & (bars.timestamp + INTERVAL <= as_of)
    ]
    entry: float | None = None
    for index, bar in enumerate(
        selected.head(OUTCOME_HORIZON_BARS).itertuples(index=False)
    ):
        prices = np.array([bar.open, bar.high, bar.low, bar.close], dtype=float)
        if (
            bar.timestamp != entry_time + index * INTERVAL
            or not np.isfinite(prices).all()
            or (prices <= 0).any()
        ):
            return PatternOutcome(
                "pending" if entry is None else "open",
                entry,
                data_status="missing_prices",
            )
        if entry is None:
            entry = float(bar.open)
        hit = pattern_outcome_for_bar(
            {
                "direction": direction,
                "entry_price": entry,
                "stop_distance": stop_distance,
                "take_distance": take_distance,
                "bars_held": index + 1,
            },
            bar,
        )
        if hit is None:
            continue
        status, gross = hit
        exit_time = bar.timestamp + INTERVAL
        income = funding_return_between(
            funding,
            entry_time=entry_time,
            exit_time=exit_time,
            as_of=as_of,
            direction=direction,
        )
        if income is None:
            return PatternOutcome("open", entry, data_status="missing_funding")
        return PatternOutcome(
            "stop" if status == "both_stop" else status,
            entry,
            exit_time,
            gross,
            income,
            gross + income - cost_bps / 10_000,
        )
    expected_bars = min(
        OUTCOME_HORIZON_BARS, max(0, int((as_of - entry_time) // INTERVAL))
    )
    return PatternOutcome(
        "pending" if entry is None else "open",
        entry,
        data_status="missing_prices" if len(selected) < expected_bars else "complete",
    )
