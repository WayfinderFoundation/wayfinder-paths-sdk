"""Causal price inputs for the frozen asset/BTC positioning model.

Input timestamps label completed 15-minute bars. Backend open-labeled candles
must be shifted to their close time before calling, with unfinished bars removed.
No future labels, position snapshots, provider calls, or orders are read here.
"""

import numpy as np
import pandas as pd

from wayfinder_paths.quant.pattern_match_positioning import (
    POSITIONING_HORIZON_BARS,
    PRICE_COLUMNS,
)
from wayfinder_paths.quant.pattern_match_universe import INTERVAL

BETA_BARS = 14 * 96
RMS_BARS = 7 * 96
PATTERN_BARS = 96


def relative_state(bars: pd.DataFrame, benchmark: pd.DataFrame) -> pd.DataFrame:
    """Causal matching index; a trade freezes beta rather than rehedging."""
    if not bars.timestamp.equals(benchmark.timestamp):
        raise ValueError("Target and hedge candles must be timestamp-aligned")
    for frame in (bars, benchmark):
        if (
            frame.timestamp.isna().any()
            or not frame.timestamp.diff().dropna().eq(INTERVAL).all()
            or not np.isfinite(frame.close).all()
            or not frame.close.gt(0).all()
        ):
            raise ValueError(
                "Relative price features require complete positive candles"
            )
    target = np.log(bars.close).diff()
    hedge = np.log(benchmark.close).diff()
    beta = (target.rolling(BETA_BARS).cov(hedge) / hedge.rolling(BETA_BARS).var()).clip(
        -3, 3
    )
    residual = target - beta.shift(1) * hedge
    # The zero prefix initializes the index only. Scale stays unknown until
    # every return in the volatility window has a previously observed beta.
    index = np.exp(residual.fillna(0).cumsum())
    return pd.DataFrame(
        {
            "beta": beta,
            "relative_index": index,
            "residual_rms": np.sqrt(residual.pow(2).rolling(RMS_BARS).mean()),
        }
    )


def relative_price_features(
    bars: pd.DataFrame, benchmark: pd.DataFrame
) -> pd.DataFrame:
    """Thirty-minute query features, also usable for daily training rows.

    Returns every input timestamp so the caller can apply the :15/:45 cadence
    and volume filter without changing alignment. Warmup/flat windows remain
    NaN; only finite features with positive scale are eligible for the model.
    """
    state = relative_state(bars, benchmark)
    frame = pd.DataFrame(
        {
            "query_time": bars.timestamp,
            "beta": state.beta,
            "scale": state.residual_rms
            * np.sqrt(POSITIONING_HORIZON_BARS)
            / (1 + state.beta.abs()),
        }
    )
    frame[PRICE_COLUMNS] = np.nan
    if len(bars) < PATTERN_BARS:
        return frame
    # Keep the research's vectorized normalization and epsilon. The ordinary
    # pattern tool intentionally rejects near-flat windows at another threshold.
    windows = np.lib.stride_tricks.sliding_window_view(
        np.log(state.relative_index.to_numpy()), PATTERN_BARS
    )
    shape = (windows - windows.mean(axis=1)[:, None]) / np.maximum(
        windows.std(axis=1)[:, None], 1e-12
    )
    points = np.linspace(0, PATTERN_BARS - 1, 24).astype(int)
    ranges = windows.max(axis=1) - windows.min(axis=1)
    variation = np.sqrt(np.square(np.diff(windows, axis=1)).sum(axis=1))
    with np.errstate(divide="ignore", invalid="ignore"):
        features = np.column_stack(
            (
                shape[:, points] * np.sqrt(0.65 / 24),
                np.log(ranges) * np.sqrt(0.20),
                np.log(variation) * np.sqrt(0.15),
            )
        )
    features[~np.isfinite(features)] = np.nan
    frame.loc[frame.index[PATTERN_BARS - 1 :], PRICE_COLUMNS] = features
    return frame
