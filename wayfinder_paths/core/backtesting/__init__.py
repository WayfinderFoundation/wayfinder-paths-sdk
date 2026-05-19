"""Backtesting helpers — re-exports for the common public surface."""

from wayfinder_paths.core.backtesting.backtester import run_backtest
from wayfinder_paths.core.backtesting.data import (
    fetch_funding_rates,
    fetch_lending_rates,
    fetch_prices,
)
from wayfinder_paths.core.backtesting.helpers import (
    backtest_delta_neutral,
    backtest_with_rates,
    quick_backtest,
)
from wayfinder_paths.core.backtesting.perps import (
    backtest_perps_trigger,
    default_decide,
)
from wayfinder_paths.core.backtesting.ref import (
    BacktestRef,
    ExecutionAssumptions,
    emit_backtest_ref,
    fingerprint_frames,
    hash_module_source,
    load_ref,
    promote_candidate,
)
from wayfinder_paths.core.backtesting.types import (
    BacktestConfig,
    BacktestResult,
    BacktestStats,
)

__all__ = [
    "BacktestConfig",
    "BacktestRef",
    "BacktestResult",
    "BacktestStats",
    "ExecutionAssumptions",
    "backtest_delta_neutral",
    "backtest_perps_trigger",
    "backtest_with_rates",
    "default_decide",
    "emit_backtest_ref",
    "fetch_funding_rates",
    "fetch_lending_rates",
    "fetch_prices",
    "fingerprint_frames",
    "hash_module_source",
    "load_ref",
    "promote_candidate",
    "quick_backtest",
    "run_backtest",
]
