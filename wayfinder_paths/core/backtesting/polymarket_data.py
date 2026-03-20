"""Data utilities for Polymarket backtesting.

Fetch functions are stubs — production implementations require Delta Lab
integration which is not yet available.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd


def regularize_to_grid(
    raw: dict[str, list[dict]],
    start: str,
    end: str,
    interval: str = "1h",
    max_gap_hours: int | None = None,
) -> pd.DataFrame:
    """Convert sparse {token_id: [{t: unix_ts, p: price}]} history to a regular grid.

    Steps:
    1. Build a regular UTC DatetimeIndex from start to end at interval.
    2. For each token, place observations at their exact timestamps and ffill.
    3. If max_gap_hours is set, any period more than max_gap_hours after the
       last real observation is set to NaN.
    """
    grid = pd.date_range(start, end, freq=interval, tz="UTC")

    frames: dict[str, pd.Series] = {}
    for token_id, ticks in raw.items():
        if not ticks:
            frames[token_id] = pd.Series(np.nan, index=grid, dtype=float)
            continue

        raw_ts = pd.to_datetime([t["t"] for t in ticks], unit="s", utc=True)
        raw_prices = pd.Series([t["p"] for t in ticks], index=raw_ts, dtype=float)

        # Reindex onto grid: places NaN at grid points with no observation
        combined = raw_prices.reindex(grid.union(raw_ts)).sort_index()
        # Forward-fill across the full combined index, then slice to grid
        filled = combined.ffill().reindex(grid)

        if max_gap_hours is not None:
            # Build a "last observation time" series on the same combined index
            obs_index = raw_ts.sort_values()
            for i, ts in enumerate(grid):
                prev_obs = obs_index[obs_index <= ts]
                if prev_obs.empty:
                    filled.iloc[i] = np.nan
                else:
                    gap_h = (ts - prev_obs[-1]).total_seconds() / 3600.0
                    if gap_h > max_gap_hours:
                        filled.iloc[i] = np.nan

        frames[token_id] = filled

    return pd.DataFrame(frames)


def warn_if_large(n_markets: int, n_trades: int) -> None:
    """Emit UserWarnings if inputs are large enough to slow the backtest."""
    if n_markets > 50:
        warnings.warn(
            f"Backtesting {n_markets} markets — this may be slow.",
            stacklevel=2,
        )
    if n_trades > 5000:
        warnings.warn(
            f"Processing {n_trades} trades — this may be slow.",
            stacklevel=2,
        )


def detect_resolutions(
    prices_df: pd.DataFrame,
    threshold: float = 0.99,
) -> dict[str, float]:
    """Return {token_id: resolution_price} for tokens whose price hit 0 or 1.

    A token resolves YES if its max price >= threshold.
    A token resolves NO  if its min price <= (1 - threshold).
    Ambiguous tokens are excluded.
    """
    result: dict[str, float] = {}
    for col in prices_df.columns:
        series = prices_df[col].dropna()
        if series.empty:
            continue
        if series.max() >= threshold:
            result[col] = 1.0
        elif series.min() <= (1.0 - threshold):
            result[col] = 0.0
    return result


async def fetch_wallet_trades(
    wallet_address: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """Fetch historical trades for a wallet from Delta Lab.

    Not yet implemented — requires Delta Lab Polymarket data integration.
    """
    raise NotImplementedError(
        "fetch_wallet_trades requires Delta Lab Polymarket integration — "
        "inject trades_df directly for backtesting."
    )


async def fetch_market_prices(
    token_ids: list[str],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """Fetch hourly price history for Polymarket token IDs from Delta Lab.

    Not yet implemented — requires Delta Lab Polymarket data integration.
    """
    raise NotImplementedError(
        "fetch_market_prices requires Delta Lab Polymarket integration — "
        "inject prices_df directly for backtesting."
    )
