"""Utility functions for backtesting framework."""

from __future__ import annotations

import numpy as np
import pandas as pd

from wayfinder_paths.core.backtesting.constants import DEFAULT_MAINTENANCE_MARGINS
from wayfinder_paths.core.backtesting.types import BacktestConfig


def get_maintenance_margin_rate(symbol: str, config: BacktestConfig) -> float:
    """Get maintenance margin rate for a symbol."""
    if config.maintenance_margin_by_symbol is None:
        return DEFAULT_MAINTENANCE_MARGINS.get(symbol, config.maintenance_margin_rate)
    return config.maintenance_margin_by_symbol.get(
        symbol, config.maintenance_margin_rate
    )


def validate_target_positions(
    target_positions: pd.DataFrame, prices: pd.DataFrame
) -> list[str]:
    """
    Validate target_positions DataFrame and return warning messages.

    Returns:
        List of warning strings (empty if no issues)
    """
    warnings: list[str] = []

    # Check for all-NaN rows
    all_nan_rows = target_positions.isna().all(axis=1)
    if all_nan_rows.any():
        nan_count = all_nan_rows.sum()
        total = len(target_positions)
        warnings.append(
            f"⚠️ Target positions has {nan_count}/{total} rows that are all NaN. "
            "Signal generation may be broken."
        )

    # Check for all-zero positions
    non_nan_positions = target_positions.fillna(0)
    all_zero_rows = (non_nan_positions == 0).all(axis=1)
    if all_zero_rows.all():
        warnings.append(
            "⚠️ All target positions are zero. Strategy will do nothing. "
            "Check signal generation logic."
        )

    # Check for inf values
    has_inf = np.isinf(target_positions.values).any()
    if has_inf:
        warnings.append(
            "⚠️ Target positions contains inf values. This will cause errors. "
            "Check for division by zero in signal generation."
        )

    # Check if positions are wildly outside [-1, 1] before clipping
    max_abs = target_positions.abs().max().max()
    if max_abs > 10:
        warnings.append(
            f"⚠️ Target positions has values up to ±{max_abs:.1f}. "
            f"Expected range is [-1, 1]. Values will be clipped. "
            "Check if you forgot to normalize weights."
        )

    return warnings
