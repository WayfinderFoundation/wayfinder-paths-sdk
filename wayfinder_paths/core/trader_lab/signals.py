"""Turn Trader Lab data into backtest inputs (core.backtesting.run_backtest).

These are building blocks for YOUR rules, not strategies: e.g. "enter like
the dip-buyer cohort's best entries, but only when funding is negative", then
backtest on point-in-time cohorts so you never pick wallets with hindsight.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd


def pit_cohort(
    con: Any, as_of: date | str, *, selector: str = "top50_sharpe"
) -> list[str]:
    """Wallets a vintage picked using only data up to ``as_of`` (a month end).
    selector: all | top50_sharpe | top50_net."""
    if selector not in ("all", "top50_sharpe", "top50_net"):
        raise ValueError("selector must be all, top50_sharpe or top50_net")
    rank = {
        "all": "TRUE",
        "top50_sharpe": "rank_sharpe <= 50",
        "top50_net": "rank_net <= 50",
    }[selector]
    rows = con.execute(
        f"SELECT address FROM cohort_membership WHERE as_of = ?::DATE AND {rank} ORDER BY address",
        [str(as_of)],
    ).fetchall()
    return [r[0] for r in rows]


def vintages(con: Any) -> list[date]:
    return [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT as_of FROM cohort_membership ORDER BY 1"
        ).fetchall()
    ]


def trips_to_weights(
    trips: pd.DataFrame,
    index: pd.DatetimeIndex,
    *,
    delay: pd.Timedelta = pd.Timedelta(minutes=5),
    max_gross: float = 1.0,
) -> pd.DataFrame:
    """Target weights per coin from round trips (columns coin, t0, t1, side).

    Each trip holds ``side`` from t0+delay to t1+delay (copying late, as a
    follower would). Concurrent trips on a coin net out; the result is scaled
    so total absolute exposure never exceeds ``max_gross``. ``index`` is the
    backtest's price index (timestamps). Rows are decision targets for the
    bar ending at that timestamp, as run_backtest expects.
    """
    required = {"coin", "t0", "t1", "side"}
    if missing := required - set(trips.columns):
        raise ValueError(f"trips is missing columns: {sorted(missing)}")
    if index.tz is None:
        raise ValueError("index must be timezone-aware (UTC)")
    coins = sorted(trips["coin"].unique())
    weights = pd.DataFrame(0.0, index=index, columns=coins)
    for trip in trips.itertuples(index=False):
        start = pd.Timestamp(trip.t0).tz_convert("UTC") + delay
        end = pd.Timestamp(trip.t1).tz_convert("UTC") + delay
        live = (index >= start) & (index < end)
        weights.loc[live, trip.coin] += float(trip.side)
    gross = weights.abs().sum(axis=1)
    scale = (max_gross / gross).where(gross > max_gross, 1.0)
    return weights.mul(scale, axis=0)
