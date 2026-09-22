"""Deterministic portfolio-policy scan over the campaign's frozen panel.

The signal scans ask "which event predicts a symbol's forward return?"; the
books that survived the diverse-starter research asked different questions:
which assets to compare, whether to hold one leader, several independent
sleeves or a defensive asset, how much gross to run, how often to rebalance,
and when to hold cash. This scan sweeps those policies mechanically on the
train panel, ranks configurations on the first part of it and reports the
rest, charges the taker round trip per unit of turnover, and hands the
designer survivors that map one-to-one onto existing strategy kernels
(``regime_rotation``, ``mixed_momentum_rank``, ``mixed_sleeve_momentum``) plus
the families it falsified. Weight formation mirrors each kernel's ``decide``;
the engine, the screen and the sealed holdout remain the judges.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.jobs.multiple_testing import expected_max_sharpe, t_statistic

DAYS_PER_YEAR = 365.25
DEFAULT_DEFENSIVE_SYMBOLS: tuple[str, ...] = ("PAXG",)
MACRO_LABELS = {1: "bull", 0: "chop", -1: "bear"}
_MIN_SYMBOLS = 3
_MIN_ROWS = 2_000
FALSIFIED_REPORT_LOSS = 0.02
SURVIVOR_MIN_SHARPE = 1.0
VERDICT_ROWS = 3


@dataclass(frozen=True)
class Panel:
    close: pd.DataFrame
    bar_seconds: int
    macro: pd.Series | None
    defensive: str | None
    # First bar with a close per symbol: a late listing joins the panel when
    # its history begins instead of truncating everyone else's.
    starts: Mapping[str, pd.Timestamp] | None = None

    @property
    def symbols(self) -> list[str]:
        return [str(column) for column in self.close.columns]

    @property
    def risk_symbols(self) -> list[str]:
        return [symbol for symbol in self.symbols if symbol != self.defensive]

    def bars(self, hours: float) -> int:
        return max(1, int(round(float(hours) * 3600.0 / float(self.bar_seconds))))


Builder = Callable[[Panel, dict[str, Any]], pd.DataFrame]
Recipe = Callable[[Panel, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class PolicyFamily:
    name: str
    description: str
    build: Builder
    grid: Mapping[str, tuple[Any, ...]]
    kernel: str | None = None
    recipe: Recipe | None = None
    requires_defensive: bool = False


def _hold(weights: pd.DataFrame, every: int, offset: int = 0) -> pd.DataFrame:
    """Targets set on rebalance bars and held in between, as the kernels'
    ``every_n_bars`` cadence does."""
    mask = pd.Series(
        (np.arange(len(weights)) % max(1, every)) == (offset % max(1, every)),
        index=weights.index,
    )
    return weights.where(mask, np.nan, axis=0).ffill().fillna(0.0)


def _sma(frame: pd.DataFrame, bars: int) -> pd.DataFrame:
    return frame.rolling(bars, min_periods=bars).mean()


def _rotation_weights(panel: Panel, params: dict[str, Any]) -> pd.DataFrame:
    """``RegimeRotationStrategy.decide`` on the whole panel at once."""
    close = panel.close
    risk = list(params.get("risk_symbols") or panel.risk_symbols)
    momentum = close.pct_change(int(params["momentum_bars"]), fill_method=None)
    eligible = momentum[risk] > 0
    if bool(params["require_trend_alignment"]):
        fast = _sma(close[risk], int(params["fast_sma_bars"]))
        slow = _sma(close[risk], int(params["slow_sma_bars"]))
        eligible &= (fast > slow) & (close[risk] > slow)
    available = momentum[risk].notna().sum(axis=1).replace(0, np.nan)
    breadth = (eligible.sum(axis=1) / available).fillna(0.0)
    on = breadth >= float(params["minimum_breadth"])
    score = momentum[risk].where(eligible)
    ranks = score.rank(axis=1, ascending=False, method="first")
    picked = (ranks <= int(params["top_n"])) & eligible
    count = picked.sum(axis=1).replace(0, np.nan)
    gross = float(params["gross_exposure"])
    weights = pd.DataFrame(0.0, index=close.index, columns=panel.symbols)
    weights[risk] = (
        picked.astype(float)
        .div(count, axis=0)
        .fillna(0.0)
        .mul(on.astype(float), axis=0)
        * gross
    )
    if panel.defensive and params.get("defensive_symbol"):
        defensive_on = (~on) & (momentum[panel.defensive] > 0)
        weights[panel.defensive] = defensive_on.astype(float) * gross
    return _hold(weights, int(params["rebalance_bars"]))


def _rank_weights(panel: Panel, params: dict[str, Any]) -> pd.DataFrame:
    """``ranked_weights`` per bar: bottom ``legs`` short, top ``legs`` long."""
    close = panel.close
    score = close.pct_change(int(params["momentum_bars"]), fill_method=None)
    legs = int(params["rank_legs"])
    ranks = score.rank(axis=1, method="first")
    available = score.notna().sum(axis=1)
    longs = ranks.gt(available - legs, axis=0)
    shorts = ranks <= legs
    raw = longs.astype(float) - shorts.astype(float)
    raw = raw.where(available >= 2 * legs, 0.0)
    return _hold(raw * float(params["weight_per_leg"]), int(params["rebalance_bars"]))


def _sleeve_weights(panel: Panel, params: dict[str, Any]) -> pd.DataFrame:
    """``sleeve_weights`` per bar: the winner of each pair long, the loser short."""
    close = panel.close
    score = close.pct_change(int(params["momentum_bars"]), fill_method=None)
    weights = pd.DataFrame(0.0, index=close.index, columns=panel.symbols)
    per_leg = float(params["weight_per_leg"])
    for left, right in params["sleeves"]:
        known = score[left].notna() & score[right].notna()
        left_wins = (score[left] > score[right]) | (
            (score[left] == score[right]) & (left > right)
        )
        weights[left] = np.where(known, np.where(left_wins, per_leg, -per_leg), 0.0)
        weights[right] = np.where(known, np.where(left_wins, -per_leg, per_leg), 0.0)
    return _hold(weights, int(params["rebalance_bars"]))


def _time_series_trend(panel: Panel, params: dict[str, Any]) -> pd.DataFrame:
    close = panel.close[panel.risk_symbols]
    fast = close.ewm(span=int(params["fast_bars"]), adjust=False).mean()
    slow = close.ewm(span=int(params["slow_bars"]), adjust=False).mean()
    trend = (fast - slow) / slow
    raw = np.sign(trend).where(trend.abs() >= float(params["threshold"]), 0.0)
    realized = (
        close.pct_change(fill_method=None).rolling(int(params["volatility_bars"])).std()
    )
    scaled = raw.div(realized.replace(0.0, np.nan)).replace([np.inf, -np.inf], np.nan)
    gross = scaled.abs().sum(axis=1).replace(0.0, np.nan)
    weights = scaled.div(gross, axis=0).fillna(0.0) * float(params["gross"])
    return _hold(
        weights.reindex(columns=panel.symbols, fill_value=0.0),
        int(params["rebalance_bars"]),
    )


def _donchian_breakout(panel: Panel, params: dict[str, Any]) -> pd.DataFrame:
    close = panel.close[panel.risk_symbols]
    entry = int(params["entry_bars"])
    upper = close.rolling(entry).max().shift(1).to_numpy()
    lower = close.rolling(entry).min().shift(1).to_numpy()
    exit_ema = close.ewm(span=int(params["exit_bars"]), adjust=False).mean().to_numpy()
    values = close.to_numpy()
    state = np.zeros_like(values)
    current = np.zeros(values.shape[1])
    for row in range(values.shape[0]):
        ready = np.isfinite(upper[row])
        value = values[row]
        current = np.where((current > 0) & (value < exit_ema[row]), 0.0, current)
        current = np.where((current < 0) & (value > exit_ema[row]), 0.0, current)
        current = np.where(ready & (value > upper[row]), 1.0, current)
        current = np.where(ready & (value < lower[row]), -1.0, current)
        state[row] = current
    raw = pd.DataFrame(state, index=close.index, columns=close.columns)
    count = raw.abs().sum(axis=1).replace(0.0, np.nan)
    weights = raw.div(count, axis=0).fillna(0.0) * float(params["gross"])
    return weights.reindex(columns=panel.symbols, fill_value=0.0)


def _trend_pullback(panel: Panel, params: dict[str, Any]) -> pd.DataFrame:
    close = panel.close[panel.risk_symbols]
    trend = close.pct_change(int(params["trend_bars"]), fill_method=None)
    pullback = close.pct_change(int(params["pullback_bars"]), fill_method=None)
    long = (trend > float(params["trend_threshold"])) & (
        pullback < -float(params["pullback_threshold"])
    )
    short = (trend < -float(params["trend_threshold"])) & (
        pullback > float(params["pullback_threshold"])
    )
    raw = long.astype(float) - short.astype(float)
    count = raw.abs().sum(axis=1).replace(0.0, np.nan)
    weights = raw.div(count, axis=0).fillna(0.0) * float(params["gross"])
    return _hold(
        weights.reindex(columns=panel.symbols, fill_value=0.0),
        int(params["rebalance_bars"]),
    )


def _rotation_recipe(panel: Panel, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbols": panel.symbols,
        "risk_symbols": list(params.get("risk_symbols") or panel.risk_symbols),
        "defensive_symbol": params.get("defensive_symbol"),
        "momentum_bars": int(params["momentum_bars"]),
        "fast_sma_bars": int(params["fast_sma_bars"]),
        "slow_sma_bars": int(params["slow_sma_bars"]),
        "require_trend_alignment": bool(params["require_trend_alignment"]),
        "minimum_breadth": float(params["minimum_breadth"]),
        "top_n": int(params["top_n"]),
        "gross_exposure": float(params["gross_exposure"]),
        "rebalance_bars": int(params["rebalance_bars"]),
        "rebalance_offset": 0,
    }


def _rank_recipe(panel: Panel, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbols": panel.symbols,
        "momentum_bars": int(params["momentum_bars"]),
        "rank_legs": int(params["rank_legs"]),
        "weight_per_leg": float(params["weight_per_leg"]),
        "rebalance_bars": int(params["rebalance_bars"]),
        "rebalance_offset": 0,
    }


def _sleeve_recipe(panel: Panel, params: dict[str, Any]) -> dict[str, Any]:
    sleeves = [list(pair) for pair in params["sleeves"]]
    return {
        "symbols": [symbol for pair in sleeves for symbol in pair],
        "sleeves": sleeves,
        "momentum_bars": int(params["momentum_bars"]),
        "weight_per_leg": float(params["weight_per_leg"]),
        "rebalance_bars": int(params["rebalance_bars"]),
        "rebalance_offset": 0,
    }


# Grid axes named ``*_h`` are hours and convert to bars for the panel's
# interval, so the same sweep reads the same on 5m, 15m and 1h worlds.
FAMILIES: tuple[PolicyFamily, ...] = (
    PolicyFamily(
        name="bull_rotation",
        description=(
            "long-only: own the top momentum leader(s) among trend-aligned risk "
            "assets when breadth confirms, else cash"
        ),
        build=_rotation_weights,
        grid={
            "fast_sma_h": (24, 48),
            "slow_sma_h": (120, 240, 480),
            "minimum_breadth": (0.5, 2 / 3),
            "momentum_h": (24, 72, 120),
            "top_n": (1, 2),
            "rebalance_h": (4, 8, 24),
            "gross_exposure": (0.4, 1.0),
            "basket": ("all", "rank_top_half"),
        },
        kernel="wayfinder_paths.jobs.strategies.regime_rotation",
        recipe=_rotation_recipe,
    ),
    PolicyFamily(
        name="risk_haven_relay",
        description=(
            "long-only relay: the top momentum risk asset when breadth confirms, "
            "else the defensive asset while its own momentum is positive, else cash"
        ),
        build=_rotation_weights,
        grid={
            "momentum_h": (24, 72, 120, 240),
            "minimum_breadth": (0.5, 0.75),
            "top_n": (1, 2),
            "rebalance_h": (4, 8, 24),
            "gross_exposure": (0.4, 1.0),
            "basket": ("all", "rank_top_half"),
        },
        kernel="wayfinder_paths.jobs.strategies.regime_rotation",
        recipe=_rotation_recipe,
        requires_defensive=True,
    ),
    PolicyFamily(
        name="cross_sectional_rank",
        description=(
            "market-neutral: long the top and short the bottom momentum ranks of "
            "the panel, middle flat, rebalanced on a fixed cadence"
        ),
        build=_rank_weights,
        grid={
            "momentum_h": (24, 72, 120, 240, 480),
            "rank_legs": (1, 2, 3),
            "rebalance_h": (4, 8, 12, 24),
            "gross": (0.5, 1.0),
        },
        kernel="wayfinder_paths.jobs.strategies.mixed_momentum_rank",
        recipe=_rank_recipe,
    ),
    PolicyFamily(
        name="sleeve_momentum",
        description=(
            "market-neutral duels: within each correlated pair, long the "
            "trailing-return winner and short the loser"
        ),
        build=_sleeve_weights,
        grid={
            "momentum_h": (24, 72, 120, 240, 480),
            "rebalance_h": (8, 24, 48),
            "gross": (0.5, 1.0),
        },
        kernel="wayfinder_paths.jobs.strategies.mixed_sleeve_momentum",
        recipe=_sleeve_recipe,
    ),
    PolicyFamily(
        name="time_series_trend",
        description="per-asset EMA trend sign, inverse-volatility sized (no kernel)",
        build=_time_series_trend,
        grid={
            "fast_h": (8, 24, 48),
            "slow_h": (96, 192, 480),
            "volatility_h": (48, 96, 192),
            "threshold": (0.0, 0.01, 0.02),
            "rebalance_h": (4, 8, 24),
            "gross": (1.0,),
        },
    ),
    PolicyFamily(
        name="donchian_breakout",
        description="per-asset channel breakout with an EMA exit (no kernel)",
        build=_donchian_breakout,
        grid={"entry_h": (12, 24, 48, 96), "exit_h": (6, 12, 24), "gross": (0.5, 1.0)},
    ),
    PolicyFamily(
        name="trend_pullback",
        description="buy pullbacks inside uptrends, sell bounces inside downtrends (no kernel)",
        build=_trend_pullback,
        grid={
            "trend_h": (72, 120, 240),
            "pullback_h": (2, 4, 8),
            "trend_threshold": (0.0, 0.02),
            "pullback_threshold": (0.005, 0.01, 0.02),
            "rebalance_h": (2, 4, 8),
            "gross": (0.5, 1.0),
        },
    ),
)


def _configs(family: PolicyFamily, panel: Panel) -> Iterable[dict[str, Any]]:
    keys = tuple(family.grid)
    for values in itertools.product(*(family.grid[key] for key in keys)):
        raw = dict(zip(keys, values, strict=True))
        params: dict[str, Any] = {}
        for key, value in raw.items():
            if key.endswith("_h"):
                params[key[:-2] + "_bars"] = panel.bars(value)
            else:
                params[key] = value
        yield params


def _family_params(
    family: PolicyFamily,
    panel: Panel,
    params: dict[str, Any],
    *,
    rank_positive: Sequence[str] = (),
    sleeve_index: pd.Index | None = None,
) -> dict[str, Any]:
    """Fill the kernel-facing fields the grid does not carry. Empty when the
    configuration cannot be formed on this panel."""
    out = dict(params)
    if family.name in {"bull_rotation", "risk_haven_relay"}:
        basket = out.pop("basket", "all")
        risk = list(rank_positive) if basket == "rank_top_half" else panel.risk_symbols
        if len(risk) < 2 or int(params["top_n"]) > len(risk):
            return {}
        out["risk_symbols"] = risk
        out["basket"] = basket
    if family.name == "bull_rotation":
        out.update({"require_trend_alignment": True, "defensive_symbol": None})
    elif family.name == "risk_haven_relay":
        out.update(
            {
                "require_trend_alignment": False,
                "defensive_symbol": panel.defensive,
                "fast_sma_bars": 1,
                "slow_sma_bars": 1,
            }
        )
    elif family.name == "cross_sectional_rank":
        out["weight_per_leg"] = float(params["gross"]) / (2 * int(params["rank_legs"]))
    elif family.name == "sleeve_momentum":
        out["sleeves"] = panel_sleeves(panel, index=sleeve_index)
        out["weight_per_leg"] = float(params["gross"]) / (2 * len(out["sleeves"]))
    return out


def panel_sleeves(
    panel: Panel, *, index: pd.Index | None = None
) -> list[tuple[str, str]]:
    """Disjoint pairs, most correlated first: a duel wants two legs that share
    a driver. Pairing is a selection, so it sees only the bars it is given
    (the rank window); the report window never chooses the pairs."""
    closes = panel.close[panel.risk_symbols]
    if index is not None:
        closes = closes.loc[index]
    returns = closes.pct_change(fill_method=None)
    corr = returns.corr()
    remaining = set(panel.risk_symbols)
    pairs: list[tuple[str, str]] = []
    scored = sorted(
        (
            (float(corr.loc[a, b]), a, b)
            for a, b in itertools.combinations(sorted(panel.risk_symbols), 2)
            if math.isfinite(float(corr.loc[a, b]))
        ),
        reverse=True,
    )
    for _, a, b in scored:
        if a in remaining and b in remaining:
            pairs.append((a, b))
            remaining.discard(a)
            remaining.discard(b)
    return pairs


def _max_lookback(params: Mapping[str, Any]) -> int:
    return max(
        (int(value) for key, value in params.items() if key.endswith("_bars")),
        default=1,
    )


def _daily(net: pd.Series) -> pd.Series:
    return (1.0 + net).groupby(net.index.floor("D")).prod() - 1.0


def _metrics(net: pd.Series, weights: pd.DataFrame) -> dict[str, Any]:
    net = net.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    equity = (1.0 + net).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    daily = _daily(net)
    std = float(daily.std(ddof=1)) if len(daily) > 1 else 0.0
    sharpe = float(daily.mean()) / std * math.sqrt(DAYS_PER_YEAR) if std > 0 else 0.0
    turnover = weights.diff().abs().sum(axis=1).fillna(0.0)
    return {
        "return": round(float(equity.iloc[-1] - 1.0), 4),
        "sharpe": round(sharpe, 3),
        "max_drawdown": round(float(drawdown.min()), 4),
        "days": int(len(daily)),
        "rebalances": int((turnover > 1e-12).sum()),
        "turnover": round(float(turnover.sum()), 2),
        "avg_gross": round(float(weights.abs().sum(axis=1).mean()), 3),
    }


def _simulate(panel: Panel, weights: pd.DataFrame, cost: float) -> pd.Series:
    deployed = weights.shift(1).fillna(0.0)
    asset_returns = panel.close.pct_change(fill_method=None).fillna(0.0)
    turnover = deployed.diff().abs().sum(axis=1).fillna(0.0)
    return (deployed * asset_returns).sum(axis=1) - turnover * cost


def _by_regime(net: pd.Series, macro: pd.Series | None) -> dict[str, float]:
    if macro is None:
        return {}
    out: dict[str, float] = {}
    aligned = macro.reindex(net.index).ffill()
    for code, label in MACRO_LABELS.items():
        mask = aligned == code
        if int(mask.sum()) >= 96:
            out[label] = round(float((1.0 + net[mask]).prod() - 1.0), 4)
    return out


def _policy_id(family: str, params: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"family": family, "params": params}, sort_keys=True, default=str
    )
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def build_panel(
    frames: Mapping[str, pd.DataFrame],
    *,
    bar_seconds: int,
    defensive_symbols: Sequence[str] = DEFAULT_DEFENSIVE_SYMBOLS,
) -> Panel:
    """Wide close panel on the union of the symbols' histories (a bar stays
    once at least the minimum number of symbols trade), with the store's
    macro label when the frames carry it."""
    closes: dict[str, pd.Series] = {}
    macro: pd.Series | None = None
    for symbol, frame in frames.items():
        stamped = frame.copy()
        stamped["timestamp"] = pd.to_datetime(stamped["timestamp"], utc=True)
        stamped = (
            stamped.drop_duplicates("timestamp").set_index("timestamp").sort_index()
        )
        closes[str(symbol)] = pd.to_numeric(stamped["close"], errors="coerce")
        if macro is None and "macro_regime" in stamped.columns:
            macro = pd.to_numeric(stamped["macro_regime"], errors="coerce").dropna()
    close = pd.DataFrame(closes).sort_index()
    if close.shape[1] < _MIN_SYMBOLS:
        raise ValueError(f"policy scan needs at least {_MIN_SYMBOLS} symbols")
    close = close[close.notna().sum(axis=1) >= _MIN_SYMBOLS]
    if len(close) < _MIN_ROWS:
        raise ValueError(f"policy scan needs at least {_MIN_ROWS} common bars")
    defensive = next((s for s in defensive_symbols if s in close.columns), None)
    starts = {
        str(symbol): close[symbol].first_valid_index() for symbol in close.columns
    }
    return Panel(
        close=close,
        bar_seconds=int(bar_seconds),
        macro=macro,
        defensive=defensive,
        starts=starts,
    )


def policy_scan(
    frames: Mapping[str, pd.DataFrame],
    *,
    bar_seconds: int,
    cost_bps_per_side: float,
    limit: int = 6,
    rank_fraction: float = 0.7,
    defensive_symbols: Sequence[str] = DEFAULT_DEFENSIVE_SYMBOLS,
    families: Sequence[PolicyFamily] = FAMILIES,
) -> dict[str, Any]:
    """Sweep every family's grid on the panel; rank on the first
    ``rank_fraction`` of it, report the rest. A survivor is consistent on
    both windows; ``cleared_family`` and ``cleared_scan`` are the rank-window
    daily t-statistic against the expected maximum over the family's grid and
    over every configuration the scan tried."""
    panel = build_panel(
        frames, bar_seconds=bar_seconds, defensive_symbols=defensive_symbols
    )
    cost = float(cost_bps_per_side) / 1e4
    split = int(len(panel.close) * rank_fraction)
    rank_index = panel.close.index[:split]
    report_index = panel.close.index[split:]
    rank_closes = panel.close.loc[rank_index]
    # The stronger half of the panel over the rank window: the mechanical
    # stand-in for a hand-picked "bull basket", chosen before the report window.
    rank_returns: dict[str, float] = {}
    for symbol in panel.risk_symbols:
        series = rank_closes[symbol].dropna()
        if len(series) >= 2 and float(series.iloc[0]) > 0:
            rank_returns[symbol] = float(series.iloc[-1]) / float(series.iloc[0])
    rank_positive = sorted(
        rank_returns,
        key=lambda symbol: (rank_returns[symbol], symbol),
        reverse=True,
    )[: max(2, (len(panel.risk_symbols) + 1) // 2)]
    rows: list[dict[str, Any]] = []
    family_rows: dict[str, list[dict[str, Any]]] = {}
    skipped: dict[str, str] = {}
    for family in families:
        if family.requires_defensive and panel.defensive is None:
            skipped[family.name] = "no defensive symbol in the panel"
            continue
        if family.name == "sleeve_momentum" and len(panel.risk_symbols) < 4:
            skipped[family.name] = "fewer than four risk symbols"
            continue
        collected: list[dict[str, Any]] = []
        seen_params: set[str] = set()
        for raw in _configs(family, panel):
            params = _family_params(
                family, panel, raw, rank_positive=rank_positive, sleeve_index=rank_index
            )
            if not params:
                continue
            fingerprint = json.dumps(params, sort_keys=True, default=str)
            if fingerprint in seen_params:
                continue
            seen_params.add(fingerprint)
            if family.name == "cross_sectional_rank" and 2 * int(
                params["rank_legs"]
            ) > len(panel.symbols):
                continue
            if _max_lookback(params) * 3 > split:
                continue
            weights = family.build(panel, params)
            net = _simulate(panel, weights, cost)
            rank_daily = _daily(net.loc[rank_index])
            rank_metrics = _metrics(net.loc[rank_index], weights.loc[rank_index])
            report_metrics = _metrics(net.loc[report_index], weights.loc[report_index])
            collected.append(
                {
                    "policy_id": _policy_id(family.name, params),
                    "family": family.name,
                    "kernel": family.kernel,
                    "params": {
                        k: v
                        for k, v in params.items()
                        if k not in {"sleeves", "risk_symbols"}
                    },
                    **(
                        {"risk_symbols": list(params["risk_symbols"])}
                        if "risk_symbols" in params
                        else {}
                    ),
                    **(
                        {"sleeves": [list(p) for p in params["sleeves"]]}
                        if "sleeves" in params
                        else {}
                    ),
                    "rank": rank_metrics,
                    "report": report_metrics,
                    "full": _metrics(net, weights),
                    "by_regime": _by_regime(net, panel.macro),
                    "_t": t_statistic([float(v) for v in rank_daily])
                    if len(rank_daily) >= 20
                    else None,
                    "_recipe": family.recipe(panel, params) if family.recipe else None,
                }
            )
        collected.sort(
            key=lambda row: (row["rank"]["sharpe"], row["rank"]["return"]), reverse=True
        )
        family_rows[family.name] = collected
        rows.extend(collected)
    trials = len(rows)
    expected = expected_max_sharpe(trials)
    family_expected = {
        name: round(expected_max_sharpe(len(collected)), 3)
        for name, collected in family_rows.items()
    }
    for row in rows:
        t_stat = row.pop("_t")
        bar = family_expected[row["family"]]
        row["haircut"] = {
            "t_stat": round(t_stat, 3) if t_stat is not None else None,
            "cleared_family": (t_stat >= bar) if t_stat is not None else None,
            "cleared_scan": (t_stat >= expected) if t_stat is not None else None,
        }
        recipe = row.pop("_recipe")
        if recipe is not None:
            row["recipe"] = {
                "module": row["kernel"],
                "build": "build_strategy",
                "params": recipe,
            }
    families_out: list[dict[str, Any]] = []
    survivors: list[dict[str, Any]] = []
    falsified: list[str] = []
    for family in families:
        if family.name in skipped:
            families_out.append(
                {
                    "name": family.name,
                    "kernel": family.kernel,
                    "skipped": skipped[family.name],
                }
            )
            continue
        collected = family_rows.get(family.name) or []
        if not collected:
            families_out.append(
                {
                    "name": family.name,
                    "kernel": family.kernel,
                    "configs": 0,
                    "verdict": "underpowered",
                }
            )
            continue
        best = collected[0]
        # Selection sees the rank window only: rows are chosen by rank-window
        # Sharpe and frozen; their report-window numbers are evaluated once
        # and carried as evidence, never used to pick among configurations.
        # The family verdict is the one look the scan takes at the report
        # window: the rank-best row's out-of-sample return.
        robust = [r for r in collected if r["rank"]["sharpe"] >= SURVIVOR_MIN_SHARPE]
        # The look is the median report return of the top three rank rows: a
        # steadier estimate than one row, still not a choice among them.
        look = sorted(r["report"]["return"] for r in collected[:VERDICT_ROWS])
        report_return = look[len(look) // 2]
        if report_return <= -FALSIFIED_REPORT_LOSS:
            # The family's best in-sample configurations lost more than the
            # screen's slice bound out of sample: dead on this panel.
            verdict = "falsified"
            falsified.append(family.name)
            robust = []
        elif report_return <= 0:
            verdict = "not_replicated"
            robust = []
        elif robust:
            verdict = "survivor"
        else:
            verdict = "weak"
        families_out.append(
            {
                "name": family.name,
                "kernel": family.kernel,
                "description": family.description,
                "configs": len(collected),
                "expected_max_t": family_expected[family.name],
                "verdict": verdict,
                "report_return_top3_median": round(report_return, 4),
                "robust": len(robust),
                # The best in-sample row, compact: enough to see why the
                # family lived or died without the pack carrying its grid.
                "best": {
                    "policy_id": best["policy_id"],
                    "params": best["params"],
                    "rank": {k: best["rank"][k] for k in ("return", "sharpe")},
                    "report": {k: best["report"][k] for k in ("return", "sharpe")},
                    "full": {
                        k: best["full"][k]
                        for k in ("return", "sharpe", "max_drawdown", "rebalances")
                    },
                    **best["haircut"],
                },
                **(
                    {"rank_top_half_basket": rank_positive}
                    if family.name in {"bull_rotation", "risk_haven_relay"}
                    else {}
                ),
            }
        )
        survivors.extend(robust)
    survivors.sort(
        key=lambda r: (r["rank"]["sharpe"], r["rank"]["return"]),
        reverse=True,
    )
    # Breadth over depth: the feed interleaves families (each family's best
    # consistent row first) so the designer sees distinct structures, not six
    # parameterizations of the strongest one.
    queues: dict[str, list[dict[str, Any]]] = {}
    for row in survivors:
        queues.setdefault(row["family"], []).append(row)
    chosen: list[dict[str, Any]] = []
    while len(chosen) < limit and any(queues.values()):
        for name in [family.name for family in families if queues.get(family.name)]:
            chosen.append(queues[name].pop(0))
            if len(chosen) >= limit:
                break
    for index, row in enumerate(chosen):
        row["pointer"] = f"/policy_scan/survivors/{index}"
    return {
        "available": True,
        "method": (
            "vectorized policy sweep on the train panel (union of the symbols' "
            "histories): weights held between rebalance bars, applied one bar "
            "later, taker round trip charged per unit of turnover; configurations "
            "and sleeve pairs are selected on the first "
            f"{int(rank_fraction * 100)}% of the panel only (rank window) and "
            "frozen; the rest (report window) is evaluated once per frozen row "
            "and never used to choose among them; survivor = rank-window daily "
            "Sharpe at or above 1 in a family whose rank-best row did not lose "
            "on the report window; cleared_family and cleared_scan are the "
            "rank-window daily t-stat against the expected maximum of the "
            "family's grid and of every configuration tried"
        ),
        "bar_seconds": int(bar_seconds),
        "symbols": panel.symbols,
        "defensive_symbol": panel.defensive,
        "panel_start": panel.close.index[0].isoformat(),
        "panel_end": panel.close.index[-1].isoformat(),
        "symbol_start": {
            symbol: (stamp.isoformat() if stamp is not None else None)
            for symbol, stamp in (panel.starts or {}).items()
        },
        "rank_window_end": rank_index[-1].isoformat() if len(rank_index) else None,
        "cost_bps_per_unit_turnover": round(float(cost_bps_per_side), 2),
        "configs": trials,
        "expected_max_t": round(expected, 3),
        "macro_split": panel.macro is not None,
        "families": families_out,
        "survivors": chosen,
        "falsified": falsified,
        "funnel": {
            "configs": trials,
            "families": len(families_out),
            "robust": len(survivors),
            "survivors": len(chosen),
            "falsified": len(falsified),
        },
        "how_to_use": (
            "A survivor is a screen, not evidence, and its report-window numbers "
            "were not used to select it: cite its pointer and "
            "instantiate its kernel — workspace/src/strategy.py is one line, "
            "`from <recipe.module> import build_strategy`, and recipe.params go "
            "into execution_params beside the job's venue and cost params; no "
            "new code. The screen, full development and the sealed holdout "
            "judge it. Falsified families are dead on this panel; do not spend "
            "attempts repairing them."
        ),
    }
