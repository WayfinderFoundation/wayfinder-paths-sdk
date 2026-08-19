"""Published jobs_v1 leverage sweeps for selectable starter strategies."""

from __future__ import annotations

from typing import Any


def _result(
    leverage: int,
    net_return: float,
    sharpe: float,
    max_drawdown: float,
    trade_count: int,
    total_fees_usd: float,
    account_halt_threshold: float,
) -> dict[str, Any]:
    return {
        "leverage": leverage,
        "return_after_fees_and_slippage": net_return,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "trade_count": trade_count,
        "total_fees_usd": total_fees_usd,
        "liquidation_count": 0,
        "account_halt_threshold": account_halt_threshold,
        "within_account_halt_threshold": max_drawdown >= account_halt_threshold,
    }


MEAN_REVERSION_HALT = -0.06
MAKER_MEAN_REVERSION_HALT = -0.08
OTHER_STARTER_HALT = -0.20

STARTER_LEVERAGE_RESULTS: dict[str, tuple[dict[str, Any], ...]] = {
    "mixed-rsi-snapback-1h": (
        _result(1, 0.0815, 1.6318, -0.0279, 182, 213.81, MEAN_REVERSION_HALT),
        _result(2, 0.1670, 1.6406, -0.0554, 182, 445.13, MEAN_REVERSION_HALT),
        _result(3, 0.2567, 1.6494, -0.0824, 182, 694.63, MEAN_REVERSION_HALT),
        _result(4, 0.3504, 1.6584, -0.1090, 182, 962.89, MEAN_REVERSION_HALT),
        _result(5, 0.4479, 1.6674, -0.1351, 182, 1250.47, MEAN_REVERSION_HALT),
    ),
    "mixed-bollinger-pullback-1h": (
        _result(1, 0.0584, 1.8481, -0.0162, 164, 190.41, MEAN_REVERSION_HALT),
        _result(2, 0.1189, 1.8488, -0.0322, 164, 393.09, MEAN_REVERSION_HALT),
        _result(3, 0.1812, 1.8495, -0.0480, 164, 608.43, MEAN_REVERSION_HALT),
        _result(4, 0.2455, 1.8502, -0.0636, 164, 836.77, MEAN_REVERSION_HALT),
        _result(5, 0.3117, 1.8510, -0.0789, 164, 1078.47, MEAN_REVERSION_HALT),
    ),
    "mixed-volume-capitulation-1h": (
        _result(1, 0.1230, 3.1431, -0.0275, 104, 124.52, MEAN_REVERSION_HALT),
        _result(2, 0.2586, 3.1423, -0.0546, 104, 263.67, MEAN_REVERSION_HALT),
        _result(3, 0.4079, 3.1414, -0.0814, 104, 418.74, MEAN_REVERSION_HALT),
        _result(4, 0.5720, 3.1405, -0.1078, 104, 591.12, MEAN_REVERSION_HALT),
        _result(5, 0.7519, 3.1396, -0.1338, 104, 782.27, MEAN_REVERSION_HALT),
    ),
    "balanced-passive-capitulation-1h": (
        _result(
            1, 0.1995, 3.666, -0.0149, 78, 77.31, MAKER_MEAN_REVERSION_HALT
        ),
        _result(
            2, 0.4326, 3.673, -0.0297, 78, 171.22, MAKER_MEAN_REVERSION_HALT
        ),
        _result(
            3, 0.7039, 3.680, -0.0445, 78, 284.54, MAKER_MEAN_REVERSION_HALT
        ),
        _result(
            4, 1.0187, 3.686, -0.0593, 78, 420.41, MAKER_MEAN_REVERSION_HALT
        ),
        _result(
            5, 1.3828, 3.692, -0.0740, 78, 582.43, MAKER_MEAN_REVERSION_HALT
        ),
    ),
    "mixed-momentum-rank-1h": (
        _result(1, 0.2310, 1.5585, -0.1096, 261, 325.04, OTHER_STARTER_HALT),
        _result(2, 0.4986, 1.6095, -0.2208, 262, 724.13, OTHER_STARTER_HALT),
        _result(3, 0.7319, 1.5593, -0.3103, 265, 1211.60, OTHER_STARTER_HALT),
        _result(4, 1.1116, 1.6906, -0.3954, 267, 1806.87, OTHER_STARTER_HALT),
        _result(5, 1.4595, 1.7349, -0.4691, 267, 2481.69, OTHER_STARTER_HALT),
    ),
    "mixed-sleeve-momentum-15m": (
        _result(1, 0.2587, 1.4915, -0.1146, 120, 141.12, OTHER_STARTER_HALT),
        _result(2, 0.4675, 1.3741, -0.2176, 120, 297.48, OTHER_STARTER_HALT),
        _result(3, 0.7783, 1.5342, -0.3390, 121, 464.03, OTHER_STARTER_HALT),
        _result(4, 1.0879, 1.6204, -0.3983, 122, 689.13, OTHER_STARTER_HALT),
        _result(5, 0.8241, 1.3245, -0.5840, 125, 763.47, OTHER_STARTER_HALT),
    ),
    "mixed-low-vol-rank-15m": (
        _result(1, 0.2044, 1.3399, -0.1023, 141, 173.02, OTHER_STARTER_HALT),
        _result(2, 0.3410, 1.1861, -0.1881, 143, 375.99, OTHER_STARTER_HALT),
        _result(3, 0.5335, 1.2668, -0.2732, 143, 607.34, OTHER_STARTER_HALT),
        _result(4, 0.6290, 1.1954, -0.3986, 149, 836.47, OTHER_STARTER_HALT),
        _result(5, 0.7307, 1.2168, -0.4753, 151, 1129.24, OTHER_STARTER_HALT),
    ),
    "hype-passive-rsi-full-5m": (
        _result(
            1, 0.2566, 1.6639, -0.0627, 244, 613.80, MAKER_MEAN_REVERSION_HALT
        ),
        _result(
            2, 0.5666, 1.7103, -0.1179, 244, 1413.20, MAKER_MEAN_REVERSION_HALT
        ),
        _result(
            3, 0.9378, 1.7520, -0.1670, 244, 2441.78, MAKER_MEAN_REVERSION_HALT
        ),
        _result(
            4, 1.3782, 1.7897, -0.2111, 244, 3750.16, MAKER_MEAN_REVERSION_HALT
        ),
        _result(
            5, 1.8962, 1.8240, -0.2508, 244, 5396.30, MAKER_MEAN_REVERSION_HALT
        ),
    ),
    "hype-passive-rsi-staged-5m": (
        _result(
            1, 0.2299, 1.5785, -0.0653, 334, 561.83, MAKER_MEAN_REVERSION_HALT
        ),
        _result(
            2, 0.5035, 1.6300, -0.1228, 334, 1269.53, MAKER_MEAN_REVERSION_HALT
        ),
        _result(
            3, 0.8267, 1.6764, -0.1739, 334, 2152.94, MAKER_MEAN_REVERSION_HALT
        ),
        _result(
            4, 1.2060, 1.7184, -0.2196, 334, 3246.21, MAKER_MEAN_REVERSION_HALT
        ),
        _result(
            5, 1.6477, 1.7568, -0.2608, 334, 4587.94, MAKER_MEAN_REVERSION_HALT
        ),
    ),
    "btc-eth-relative-strength-1d": (
        _result(1, 0.2090, 0.9930, -0.1102, 182, 56.43, OTHER_STARTER_HALT),
        _result(2, 0.4275, 0.9836, -0.2137, 185, 126.43, OTHER_STARTER_HALT),
        _result(3, 0.6499, 0.9781, -0.3108, 189, 212.11, OTHER_STARTER_HALT),
        _result(4, 0.8626, 0.9712, -0.4016, 191, 313.03, OTHER_STARTER_HALT),
        _result(5, 1.0518, 0.9636, -0.4865, 191, 427.80, OTHER_STARTER_HALT),
    ),
    "bch-ltc-relative-strength-1d": (
        _result(1, 0.2815, 1.2433, -0.1079, 181, 49.76, OTHER_STARTER_HALT),
        _result(2, 0.6096, 1.2495, -0.2059, 184, 105.91, OTHER_STARTER_HALT),
        _result(3, 0.9832, 1.2564, -0.3000, 192, 168.00, OTHER_STARTER_HALT),
        _result(4, 1.3958, 1.2622, -0.3883, 191, 234.93, OTHER_STARTER_HALT),
        _result(5, 1.8399, 1.2679, -0.4697, 193, 307.15, OTHER_STARTER_HALT),
    ),
}
