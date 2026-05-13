"""APEX/GMX Pair Velocity Strategy.

Market-neutral mean-reversion on the APEX/GMX log-price spread on
Hyperliquid perps. Single-pair, capital-efficient — runs at lev=2.5
with a loose entry threshold (|z|>0.75) for higher trade frequency.

Backtest (180 days, 30 bps slippage + 4.5 bps fee, with funding):
    30d  Sharpe 2.45 / +27.7%  / 45 trades
    60d  Sharpe 2.54 / +50.4%  / 116 trades
    90d  Sharpe 3.08 / +130.5% / 195 trades
    120d Sharpe 2.19 / +97.5%  / 221 trades
    180d Sharpe 2.51 / +180.4% / 328 trades

Max drawdown over the 180d window: −40.0%. DD is largely structural
at this leverage — invariant from ez=0.5 to ez=1.5.

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

    # 60d window: edge dominates variance (audit shows ~+50% trailing 60d).
    SMOKE_TEST_WINDOW_DAYS = 60
    SMOKE_MIN_TOTAL_RETURN = 0.0

    DEFAULT_PARAMS = {
        "lookback_bars": 72,
        "entry_z": 0.75,
        "velocity_bars": 6,
        "target_leverage": 2.5,
        "rebalance_threshold": 0.02,
        "min_order_usd": 10.0,
        "symbols": ["APEX", "GMX"],
    }
