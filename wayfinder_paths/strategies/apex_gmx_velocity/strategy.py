"""APEX/GMX Pair Velocity Strategy.

Market-neutral mean-reversion on the APEX/GMX log-price spread on
Hyperliquid perps. Single-pair, low turnover, capital-efficient — runs
at ~0.9 trades/day and ~$25 daily volume on a $46 NAV at lev=1.5.

Backtest (200 days, 25 bps slippage + 4.5 bps fee, with funding):
    30d Sharpe 3.25  / +8.77%  / 27 trades
    60d Sharpe 4.07  / +26.54% / 55 trades
    90d Sharpe 3.78  / +50.14% / 83 trades
    120d Sharpe 3.74 / +64.07% / 104 trades

Funding net ≈ 0 because both legs have similar positive funding (APEX
+11.96% / GMX +8.47% annualized) and the strategy spends roughly equal
time in each direction.
"""

from pathlib import Path

from wayfinder_paths.core.strategies.active_perps import ActivePerpsStrategy


class ApexGmxVelocityStrategy(ActivePerpsStrategy):
    # `name` is the wallet label used by get_adapter() and the StateStore
    # directory. Set to a wallet present in config.json before deploying.
    name = "perp_dex_funded_tester"

    REF = Path(__file__).parent / "backtest_ref.json"

    SIGNAL = "wayfinder_paths.strategies.apex_gmx_velocity.signal:compute_signal"
    DECIDE = "wayfinder_paths.strategies.apex_gmx_velocity.decide:decide"

    HIP3_DEXES = []

    DEFAULT_PARAMS = {
        "lookback_bars": 72,
        "entry_z": 2.0,
        "velocity_bars": 6,
        "target_leverage": 1.5,
        "rebalance_threshold": 0.02,
        "min_order_usd": 10.0,
        "symbols": ["APEX", "GMX"],
    }
