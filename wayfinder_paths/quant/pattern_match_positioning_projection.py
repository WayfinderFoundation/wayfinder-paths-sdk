"""Underlying-price projections using the positioning model's actual weights."""

from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.quant.pattern_match_positioning import POSITIONING_HORIZON_BARS
from wayfinder_paths.quant.pattern_match_universe import INTERVAL


def positioning_projection_series(
    estimation: pd.DataFrame,
    weights: np.ndarray,
    histories: Mapping[str, pd.DataFrame],
    *,
    as_of: pd.Timestamp,
) -> dict[str, Any]:
    """Weighted empirical asset paths, not a hedged-return curve in USD disguise.

    Histories are close-labeled candles. Use every positive-weight reference,
    not an independently selected top-N set. Missing paths fail the projection
    rather than silently changing the model's analogue distribution.
    """
    if (
        weights.shape != (len(estimation),)
        or not np.isfinite(weights).all()
        or (weights < 0).any()
        or weights.sum() <= 0
    ):
        raise ValueError("Invalid positioning projection weights")
    selected = estimation.iloc[np.flatnonzero(weights > 0)]
    mass = weights[weights > 0] / weights.sum()
    paths = []
    indexed = {
        coin: frame.set_index("timestamp").close for coin, frame in histories.items()
    }
    for row in selected.itertuples():
        end = row.query_time + POSITIONING_HORIZON_BARS * INTERVAL
        if (
            end > as_of
            or row.coin not in indexed
            or indexed[row.coin].index.has_duplicates
        ):
            raise ValueError("Missing or future positioning projection history")
        times = pd.date_range(row.query_time, end, freq=INTERVAL)
        prices = indexed[row.coin].reindex(times).to_numpy(dtype=float)
        if not np.isfinite(prices).all() or not (prices > 0).all():
            raise ValueError("Incomplete positioning projection history")
        paths.append(prices / prices[0] - 1)
    forward = np.asarray(paths)
    order = np.argsort(forward, axis=0, kind="stable")
    values = np.take_along_axis(forward, order, axis=0)
    cumulative = np.cumsum(mass[order], axis=0)
    quantiles = {
        name: (
            values[(cumulative < q).sum(axis=0), np.arange(forward.shape[1])] * 10_000
        ).tolist()
        for name, q in (("q25_bps", 0.25), ("median_bps", 0.5), ("q75_bps", 0.75))
    }
    return {
        "id": "positioning_analogues",
        "label": "Weighted asset paths · BTC hedge excluded",
        "source": "hyperliquid",
        "sample_count": len(selected),
        **quantiles,
        "hit_rate_up": np.average(forward > 0, weights=mass, axis=0).tolist(),
        # The standard individual-line schema labels its rank as similarity.
        # Learned probability mass is not that metric; show the weighted band.
        "analogues": [],
    }
