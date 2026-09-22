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
    "bullish-regime-rotation-5m": (
        _result(1, 1.4347, 2.6261, -0.1758, 156, 508.61, OTHER_STARTER_HALT),
        _result(2, 3.8763, 2.6081, -0.3271, 155, 1662.44, OTHER_STARTER_HALT),
        _result(3, 7.3691, 2.6326, -0.4562, 155, 3745.28, OTHER_STARTER_HALT),
        _result(4, 11.7071, 2.6322, -0.5653, 155, 6984.70, OTHER_STARTER_HALT),
        _result(5, 16.6030, 2.5928, -0.6565, 156, 11614.16, OTHER_STARTER_HALT),
    ),
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
        _result(1, 0.1995, 3.666, -0.0149, 78, 77.31, MAKER_MEAN_REVERSION_HALT),
        _result(2, 0.4326, 3.673, -0.0297, 78, 171.22, MAKER_MEAN_REVERSION_HALT),
        _result(3, 0.7039, 3.680, -0.0445, 78, 284.54, MAKER_MEAN_REVERSION_HALT),
        _result(4, 1.0187, 3.686, -0.0593, 78, 420.41, MAKER_MEAN_REVERSION_HALT),
        _result(5, 1.3828, 3.692, -0.0740, 78, 582.43, MAKER_MEAN_REVERSION_HALT),
    ),
    "mixed-momentum-rank-1h": (
        _result(1, 0.2310, 1.5585, -0.1096, 261, 325.04, OTHER_STARTER_HALT),
        _result(2, 0.4986, 1.6095, -0.2208, 262, 724.13, OTHER_STARTER_HALT),
        _result(3, 0.7319, 1.5593, -0.3103, 265, 1211.60, OTHER_STARTER_HALT),
        _result(4, 1.1116, 1.6906, -0.3954, 267, 1806.87, OTHER_STARTER_HALT),
        _result(5, 1.4595, 1.7349, -0.4691, 267, 2481.69, OTHER_STARTER_HALT),
    ),
    "crypto-momentum-persistence-4h": (
        _result(1, 1.0390, 1.5407, -0.1590, 436, 900.88, OTHER_STARTER_HALT),
        _result(2, 2.5656, 1.5236, -0.2826, 439, 2404.53, OTHER_STARTER_HALT),
        _result(3, 4.6651, 1.5437, -0.4385, 441, 4426.94, OTHER_STARTER_HALT),
        _result(4, 6.9181, 1.5495, -0.5146, 448, 7417.53, OTHER_STARTER_HALT),
        _result(5, 10.8986, 1.6338, -0.6563, 456, 11320.56, OTHER_STARTER_HALT),
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
    "diversified-trend-sleeves-15m": (
        _result(1, 0.8443, 2.6780, -0.1107, 889, 674.93, OTHER_STARTER_HALT),
        _result(2, 2.4468, 2.8853, -0.2010, 889, 1950.41, OTHER_STARTER_HALT),
        _result(3, 4.6532, 2.8889, -0.2803, 889, 3901.41, OTHER_STARTER_HALT),
        _result(4, 6.5567, 2.6873, -0.4352, 888, 5886.12, OTHER_STARTER_HALT),
        _result(5, 9.8576, 2.7212, -0.4940, 888, 9152.52, OTHER_STARTER_HALT),
    ),
    "diversified-momentum-taker-15m": (
        _result(1, 0.4537, 1.4236, -0.1821, 1634, 1437.92, OTHER_STARTER_HALT),
        _result(2, 1.0001, 1.5156, -0.3365, 1634, 3335.48, OTHER_STARTER_HALT),
        _result(3, 1.3086, 1.4482, -0.4665, 1633, 5120.84, OTHER_STARTER_HALT),
        _result(4, 1.4081, 1.4072, -0.5808, 1628, 6478.78, OTHER_STARTER_HALT),
        _result(5, 1.6692, 1.4850, -0.6666, 1629, 8240.02, OTHER_STARTER_HALT),
    ),
    "crypto-gold-regime-relay-15m": (
        _result(1, 0.7023, 1.6410, -0.1716, 276, 634.06, OTHER_STARTER_HALT),
        _result(2, 1.4739, 1.6620, -0.3178, 275, 1477.88, OTHER_STARTER_HALT),
        _result(3, 2.2541, 1.7124, -0.4417, 275, 2463.38, OTHER_STARTER_HALT),
        _result(4, 2.8333, 1.7457, -0.5461, 275, 3412.28, OTHER_STARTER_HALT),
        _result(5, 2.9776, 1.7550, -0.6336, 277, 4054.20, OTHER_STARTER_HALT),
    ),
    "hype-passive-rsi-full-5m": (
        _result(1, 0.2566, 1.6639, -0.0627, 244, 613.80, MAKER_MEAN_REVERSION_HALT),
        _result(2, 0.5666, 1.7103, -0.1179, 244, 1413.20, MAKER_MEAN_REVERSION_HALT),
        _result(3, 0.9378, 1.7520, -0.1670, 244, 2441.78, MAKER_MEAN_REVERSION_HALT),
        _result(4, 1.3782, 1.7897, -0.2111, 244, 3750.16, MAKER_MEAN_REVERSION_HALT),
        _result(5, 1.8962, 1.8240, -0.2508, 244, 5396.30, MAKER_MEAN_REVERSION_HALT),
    ),
    "hype-passive-rsi-staged-5m": (
        _result(1, 0.2299, 1.5785, -0.0653, 334, 561.83, MAKER_MEAN_REVERSION_HALT),
        _result(2, 0.5035, 1.6300, -0.1228, 334, 1269.53, MAKER_MEAN_REVERSION_HALT),
        _result(3, 0.8267, 1.6764, -0.1739, 334, 2152.94, MAKER_MEAN_REVERSION_HALT),
        _result(4, 1.2060, 1.7184, -0.2196, 334, 3246.21, MAKER_MEAN_REVERSION_HALT),
        _result(5, 1.6477, 1.7568, -0.2608, 334, 4587.94, MAKER_MEAN_REVERSION_HALT),
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
    "diversified-liquidation-flush-maker-15m": (
        _result(1, 0.1534, 1.9476, -0.0432, 602, 96.92, OTHER_STARTER_HALT),
        _result(2, 0.3260, 1.9578, -0.0853, 602, 207.93, OTHER_STARTER_HALT),
        _result(3, 0.5196, 1.9678, -0.1265, 602, 334.82, OTHER_STARTER_HALT),
        _result(4, 0.7359, 1.9776, -0.1667, 602, 479.56, OTHER_STARTER_HALT),
        _result(5, 0.9768, 1.9872, -0.2059, 602, 644.26, OTHER_STARTER_HALT),
    ),
    "diversified-funding-oi-divergence-taker-15m": (
        _result(1, 0.0632, 0.8755, -0.0475, 792, 184.94, OTHER_STARTER_HALT),
        _result(2, 0.1252, 0.8791, -0.0931, 792, 381.69, OTHER_STARTER_HALT),
        _result(3, 0.1857, 0.8827, -0.1369, 792, 589.50, OTHER_STARTER_HALT),
        _result(4, 0.2440, 0.8863, -0.1788, 792, 807.48, OTHER_STARTER_HALT),
        _result(5, 0.2996, 0.8899, -0.2191, 792, 1034.64, OTHER_STARTER_HALT),
    ),
}
