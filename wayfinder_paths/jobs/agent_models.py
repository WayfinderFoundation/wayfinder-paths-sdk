"""Agent-trained cross-sectional models: bounded, walk-forward, inference in precompute.

A campaign worker may fit a small model on the campaign's own development data
(``evolution_train_model``): ridge, or ridge plus a capped, early-stopped
gradient-boosted tree on ridge's residuals (``tree``), over a
menu of causal factors resampled to 4h — the base OHLCV factor set, realized
moments from the strategy's own bars, and (when the strategy declares them)
funding and the shipped ``xs_factor_rank``. Training is walk-forward: one model
per 30 days, each fitted only on targets realized before its ``train_end``, and
a bar is scored only by a model fitted strictly before it. The artifacts live in
the candidate bundle under ``workspace/models/<name>/`` (hashed into the
revision); the strategy scores the universe in ``precompute`` with
``model_scores``, so backtest, forward replay, probation and live all run the
same code.

Bounds, so a model is one design option and not the whole campaign: a training
budget per campaign, at most MAX_FEATURES inputs, trees no deeper than 3 with at
most 300 iterations, and the diagnostics a worker sees cover only the discovery
window (before the campaign's validation split).

Evidence for the shape (study 2026-10-03): one-day relative targets on 4h bars
are the only horizon whose ranking survives 8 bps per side; ridge transferred
across venues better than trees.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from wayfinder_paths.jobs.factor_model import (
    FACTOR_RANK_FEATURE,
    RidgeFactorModel,
    factor_panel,
    fit_ridge,
    resample_bars,
)

MODELS_DIR = "workspace/models"
TIMEFRAME = "4h"
HORIZONS = {"4h": 1, "1d": 6, "3d": 18}
KINDS = ("ridge", "tree")
MAX_FEATURES = 40
MIN_SYMBOLS = 8
MIN_TRAIN_DAYS = 90
REFIT_DAYS = 30
# The longest lookback in the menu: 128 4h bars of returns (~21 days) and the
# 30-day realized windows. A strategy using a model declares warmup to cover it.
WARMUP_DAYS = 32
TREE_CAPS = {
    "max_depth": 3,
    "max_iter": 300,
    "min_samples_leaf": 100,
    "learning_rate": 0.02,
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_BASE_GROUPS = {
    "returns": ["r_1", "r_2", "r_4", "r_8", "r_16", "r_32", "r_64", "r_128"],
    "volatility": ["vol_16", "vol_64", "vol_ratio", "range_16", "park_vol_16"],
    "trend": ["z_ma_20", "z_ma_50", "z_ma_100", "dist_hi_64", "dist_lo_64", "rsi_14"],
    "volume": ["vol_z_64", "dollar_vol_64"],
    "bar_shape": ["body", "upper_wick", "lower_wick"],
    "calendar": ["hour_sin", "hour_cos", "dow_sin", "dow_cos"],
    "cross_section": [
        "xs_rank_r_4",
        "xs_rank_r_16",
        "xs_rank_r_64",
        "xs_rank_vol_64",
        "xs_rank_vol_z",
        "dispersion_4",
    ],
    "market": [
        "mkt_r_1",
        "mkt_r_4",
        "mkt_r_16",
        "mkt_r_64",
        "idio_r_1",
        "idio_r_4",
        "idio_r_16",
        "idio_r_64",
    ],
    "leader": ["lead_r_1", "lead_r_4", "lead_r_16", "lead_r_64", "lead_vol_64"],
}
FEATURE_GROUPS: dict[str, list[str]] = {
    **_BASE_GROUPS,
    "realized": [
        "rv_1d",
        "rv_3d",
        "rskew_1d",
        "rskew_3d",
        "rkurt_1d",
        "jump_1d",
        "jump_3d",
        "max_1d_30d",
        "min_1d_30d",
        "xsr_rskew_1d",
        "xsr_max_1d_30d",
    ],
    "funding": ["fund_1d", "fund_7d", "fund_chg", "fund_rel", "xsr_fund_7d"],
    "factor_feed": ["xs_factor_rank_in"],
}


def bars_from_frames(frames: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    parts = []
    for symbol, frame in frames.items():
        part = frame.copy()
        part["symbol"] = symbol
        parts.append(part)
    bars = pd.concat(parts, ignore_index=True)
    bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True)
    return bars.sort_values(["symbol", "timestamp"]).reset_index(drop=True)


def _realized(bars: pd.DataFrame, stamps: pd.DatetimeIndex) -> pd.DataFrame:
    step = pd.Series(sorted(bars["timestamp"].unique())).diff().median()
    per_day = max(1, int(pd.Timedelta(days=1) / step))
    out = []
    for symbol, frame in bars.groupby("symbol"):
        frame = frame.set_index("timestamp")
        logc = np.log(frame["close"].astype(float))
        r = logc.diff()
        f: dict[str, pd.Series] = {}
        for days in (1, 3):
            n = per_day * days
            r2 = (r**2).rolling(n).sum()
            f[f"rv_{days}d"] = np.sqrt(r2)
            f[f"rskew_{days}d"] = np.sqrt(n) * (r**3).rolling(n).sum() / r2**1.5
            f[f"rkurt_{days}d"] = n * (r**4).rolling(n).sum() / r2**2
            f[f"jump_{days}d"] = ((r**2) * np.sign(r)).rolling(n).sum() / r2
        daily = logc - logc.shift(per_day)
        f["max_1d_30d"] = daily.rolling(per_day * 30).max()
        f["min_1d_30d"] = daily.rolling(per_day * 30).min()
        if "funding" in frame:
            fund = frame["funding"].astype(float).ffill()
            f["fund_1d"] = fund.rolling(per_day).mean()
            f["fund_7d"] = fund.rolling(per_day * 7).mean()
        if FACTOR_RANK_FEATURE in frame:
            f["xs_factor_rank_in"] = frame[FACTOR_RANK_FEATURE].astype(float)
        columns = pd.DataFrame(f, index=frame.index)
        columns = columns[columns.index.isin(stamps)].copy()
        columns["symbol"] = symbol
        out.append(columns.rename_axis("timestamp").reset_index())
    return pd.concat(out, ignore_index=True)


def model_panel(bars: pd.DataFrame, horizon_bars: int) -> pd.DataFrame:
    """Every menu column at each 4h close (``y_xs`` is the training target:
    relative log return over the horizon, NaN where the future is unknown)."""
    four_hour = resample_bars(
        bars[["timestamp", "symbol", "open", "high", "low", "close", "volume"]],
        TIMEFRAME,
    )
    if four_hour.empty:
        # No complete 4h bin in the window: nothing to score, and a backtest
        # over the same bars has no row for them either.
        return pd.DataFrame(columns=["timestamp", "symbol", "close_4h"])
    panel = factor_panel(four_hour, horizon_bars)
    panel = panel.merge(
        four_hour[["timestamp", "symbol", "close"]].rename(
            columns={"close": "close_4h"}
        ),
        on=["timestamp", "symbol"],
    )
    extra = _realized(bars, pd.DatetimeIndex(panel["timestamp"].unique()))
    panel = panel.merge(extra, on=["timestamp", "symbol"], how="left")
    grouped = panel.groupby("timestamp")
    panel["xsr_rskew_1d"] = grouped["rskew_1d"].rank(pct=True)
    panel["xsr_max_1d_30d"] = grouped["max_1d_30d"].rank(pct=True)
    if "fund_7d" in panel:
        panel["fund_chg"] = panel["fund_1d"] - panel["fund_7d"]
        panel["fund_rel"] = panel["fund_1d"] - grouped["fund_1d"].transform("mean")
        panel["xsr_fund_7d"] = grouped["fund_7d"].rank(pct=True)
    return panel


def resolve_features(requested: Sequence[str], panel: pd.DataFrame) -> list[str]:
    """Group names expand to their columns; plain names must be menu columns
    present in this panel. Fails loudly: a model's inputs are its contract."""
    menu = {c for cols in FEATURE_GROUPS.values() for c in cols}
    columns: list[str] = []
    for item in requested:
        expanded = FEATURE_GROUPS.get(item, [item])
        for column in expanded:
            if column not in menu:
                raise ValueError(
                    f"unknown model input {column!r}; choose from groups {sorted(FEATURE_GROUPS)}"
                )
            if column not in panel or panel[column].notna().sum() == 0:
                raise ValueError(
                    f"model input {column!r} is unavailable here (funding and factor_feed inputs need "
                    "the strategy to declare that feature)"
                )
            if column not in columns:
                columns.append(column)
    if not columns or len(columns) > MAX_FEATURES:
        raise ValueError(f"a model takes 1..{MAX_FEATURES} inputs, got {len(columns)}")
    return columns


_NODE_FIELDS = (
    "feature_idx",
    "num_threshold",
    "missing_go_to_left",
    "left",
    "right",
    "is_leaf",
    "value",
)


@dataclass
class TreeModel:
    """Boosted regression trees as plain node arrays: sklearn fits them, numpy
    scores them, and the artifact is data (JSON), never executable."""

    features: list[str]
    baseline: float
    trees: list[dict[str, list[float]]]
    train_end: pd.Timestamp
    # The tree learns ridge's residuals: on these data a tree alone ranked at
    # IC +0.014 against ridge's +0.054, ridge plus an early-stopped residual
    # tree at +0.049 (Hyperliquid 20, nine months out of sample).
    ridge: RidgeFactorModel | None = None

    def predict(self, panel: pd.DataFrame) -> np.ndarray:
        x = panel.reindex(columns=self.features).to_numpy(dtype=float)
        out = np.full(len(x), self.baseline)
        if self.ridge is not None:
            out = out + self.ridge.predict(panel)
        rows = np.arange(len(x))
        for tree in self.trees:
            feature = np.asarray(tree["feature_idx"], dtype=int)
            threshold = np.asarray(tree["num_threshold"], dtype=float)
            missing_left = np.asarray(tree["missing_go_to_left"], dtype=bool)
            left = np.asarray(tree["left"], dtype=int)
            right = np.asarray(tree["right"], dtype=int)
            leaf = np.asarray(tree["is_leaf"], dtype=bool)
            value = np.asarray(tree["value"], dtype=float)
            node = np.zeros(len(x), dtype=int)
            while not leaf[node].all():
                active = ~leaf[node]
                n = node[active]
                v = x[rows[active], feature[n]]
                go_left = np.where(np.isnan(v), missing_left[n], v <= threshold[n])
                node[active] = np.where(go_left, left[n], right[n])
            out += value[node]
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "features": self.features,
            "baseline": self.baseline,
            "trees": self.trees,
            "train_end": self.train_end.isoformat(),
            "ridge": self.ridge.to_dict() if self.ridge is not None else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TreeModel:
        return cls(
            features=list(data["features"]),
            baseline=float(data["baseline"]),
            trees=list(data["trees"]),
            train_end=pd.Timestamp(data["train_end"]),
            ridge=RidgeFactorModel.from_dict(data["ridge"])
            if data.get("ridge")
            else None,
        )


@dataclass
class TrainedModel:
    name: str
    kind: str
    features: list[str]
    horizon: str
    models: list[Any] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def score(self, panel: pd.DataFrame) -> pd.DataFrame:
        ends = pd.DatetimeIndex([m.train_end for m in self.models])
        choice = ends.searchsorted(panel["timestamp"], side="left") - 1
        out = panel[["timestamp", "symbol"]].copy()
        out["model_score"] = np.nan
        for index in np.unique(choice[choice >= 0]):
            rows = choice == index
            out.loc[rows, "model_score"] = self.models[index].predict(panel.loc[rows])
        out["model_rank"] = out.groupby("timestamp")["model_score"].rank(pct=True) - 0.5
        return out


def _fit_tree(
    panel: pd.DataFrame,
    features: list[str],
    train_end: pd.Timestamp,
    horizon_bars: int,
    params: Mapping[str, Any],
) -> TreeModel:
    bar = pd.Timedelta(TIMEFRAME)
    ridge = fit_ridge(
        panel[["timestamp", "symbol", *features, "y_xs"]],
        train_end=train_end,
        bar=bar,
        horizon_bars=horizon_bars,
    )
    stamps = pd.DatetimeIndex(panel["timestamp"].unique()).sort_values()
    keep = stamps[stamps <= train_end - bar * horizon_bars][
        :: max(1, horizon_bars // 2)
    ]
    rows = panel[panel["timestamp"].isin(keep) & panel["y_xs"].notna()]
    target = rows["y_xs"].clip(*rows["y_xs"].quantile([0.005, 0.995]))
    residual = target.to_numpy() - ridge.predict(rows)
    estimator = HistGradientBoostingRegressor(
        max_depth=min(int(params.get("max_depth", 3)), TREE_CAPS["max_depth"]),
        max_iter=min(int(params.get("max_iter", 300)), TREE_CAPS["max_iter"]),
        learning_rate=max(
            float(params.get("learning_rate", 0.02)), TREE_CAPS["learning_rate"]
        ),
        min_samples_leaf=max(
            int(params.get("min_samples_leaf", 200)), TREE_CAPS["min_samples_leaf"]
        ),
        l2_regularization=10.0,
        max_features=0.5,
        early_stopping=True,
        validation_fraction=0.2,
        n_iter_no_change=20,
        random_state=0,
    )
    estimator.fit(rows[features].to_numpy(dtype=float), residual)
    trees = [
        {name: predictor[0].nodes[name].tolist() for name in _NODE_FIELDS}
        for predictor in estimator._predictors
    ]
    return TreeModel(
        features=features,
        baseline=float(np.ravel(estimator._baseline_prediction)[0]),
        trees=trees,
        train_end=pd.Timestamp(train_end),
        ridge=ridge,
    )


def train(
    bars: pd.DataFrame,
    *,
    name: str,
    kind: str,
    features: Sequence[str],
    horizon: str = "1d",
    discovery_end: pd.Timestamp | None = None,
    params: Mapping[str, Any] | None = None,
) -> TrainedModel:
    if not _NAME_RE.match(name):
        raise ValueError(f"model name must match {_NAME_RE.pattern}")
    if kind not in KINDS:
        raise ValueError(f"model kind must be one of {KINDS}")
    if horizon not in HORIZONS:
        raise ValueError(f"horizon must be one of {sorted(HORIZONS)}")
    if bars["symbol"].nunique() < MIN_SYMBOLS:
        raise ValueError(f"a cross-sectional model needs {MIN_SYMBOLS}+ symbols")
    horizon_bars = HORIZONS[horizon]
    panel = model_panel(bars, horizon_bars)
    columns = resolve_features(features, panel)
    first = panel["timestamp"].min() + pd.Timedelta(days=MIN_TRAIN_DAYS)
    # The last model is fitted at the end of the discovery window and then
    # frozen: nothing retrains an agent's model later, live or in the bench.
    last = (
        min(panel["timestamp"].max(), discovery_end)
        if discovery_end is not None
        else panel["timestamp"].max()
    )
    if first >= last:
        raise ValueError(f"training needs more than {MIN_TRAIN_DAYS} days of history")
    trained = TrainedModel(name=name, kind=kind, features=columns, horizon=horizon)
    train_end = first
    while train_end <= last:
        model: RidgeFactorModel | TreeModel
        if kind == "ridge":
            model = fit_ridge(
                panel[["timestamp", "symbol", *columns, "y_xs"]],
                train_end=train_end,
                bar=pd.Timedelta(TIMEFRAME),
                horizon_bars=horizon_bars,
            )
        else:
            model = _fit_tree(panel, columns, train_end, horizon_bars, params or {})
        trained.models.append(model)
        if train_end == last:
            break
        train_end = min(train_end + pd.Timedelta(days=REFIT_DAYS), last)
    trained.diagnostics = diagnostics(trained, panel, horizon_bars, discovery_end)
    return trained


def diagnostics(
    model: TrainedModel,
    panel: pd.DataFrame,
    horizon_bars: int,
    discovery_end: pd.Timestamp | None,
) -> dict[str, Any]:
    """Out-of-sample ranking quality inside the discovery window only, so the
    campaign's validation split stays unseen by whoever chose the model."""
    scored = model.score(panel).merge(
        panel[["timestamp", "symbol", "y_xs"]], on=["timestamp", "symbol"]
    )
    scored = scored.dropna(subset=["model_score", "y_xs"])
    if discovery_end is not None:
        scored = scored[
            scored["timestamp"]
            <= discovery_end - pd.Timedelta(TIMEFRAME) * horizon_bars
        ]
    stamps = pd.DatetimeIndex(scored["timestamp"].unique()).sort_values()[
        ::horizon_bars
    ]
    decisions = scored[scored["timestamp"].isin(stamps)]
    ics = (
        decisions.groupby("timestamp")
        .apply(
            lambda g: g["model_score"].rank().corr(g["y_xs"].rank()),
            include_groups=False,
        )
        .dropna()
    )
    spread = (
        decisions.groupby("timestamp")
        .apply(
            lambda g: g.loc[g["model_rank"] >= 0.3, "y_xs"].mean()
            - g.loc[g["model_rank"] <= -0.3, "y_xs"].mean(),
            include_groups=False,
        )
        .dropna()
    )
    t_stat = (
        float(ics.mean() / ics.std() * np.sqrt(len(ics)))
        if len(ics) > 2 and ics.std() > 0
        else 0.0
    )
    return {
        "window": [
            str(stamps.min()) if len(stamps) else None,
            str(stamps.max()) if len(stamps) else None,
        ],
        "decisions": int(len(ics)),
        "rank_ic": round(float(ics.mean()), 4) if len(ics) else None,
        "rank_ic_t": round(t_stat, 2),
        "top_minus_bottom_fifth_per_period": round(float(spread.mean()), 5)
        if len(spread)
        else None,
        "read": (
            "out-of-sample inside the discovery window only. A rank IC near +0.03..+0.06 "
            "(t >= 2) is what the study found tradable at a one-day horizon; per-period "
            "spread must clear about 2 x 8 bps of turnover to survive costs."
        ),
    }


def save(model: TrainedModel, bundle: Path) -> Path:
    root = Path(bundle) / MODELS_DIR / model.name
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": model.name,
        "kind": model.kind,
        "features": model.features,
        "horizon": model.horizon,
        "timeframe": TIMEFRAME,
        "warmup_days": WARMUP_DAYS,
        "diagnostics": model.diagnostics,
        "models": [],
    }
    manifest["models"] = [fitted.to_dict() for fitted in model.models]
    (root / "model.json").write_text(json.dumps(manifest))
    return root


def load(path: str | Path) -> TrainedModel:
    """``path`` is the model directory, workspace/models/<name>; a strategy
    resolves it next to its own file (Path(__file__).parents[1] / "models" / name)."""
    manifest = json.loads((Path(path) / "model.json").read_text())
    model = TrainedModel(
        name=manifest["name"],
        kind=manifest["kind"],
        features=list(manifest["features"]),
        horizon=manifest["horizon"],
        diagnostics=dict(manifest.get("diagnostics") or {}),
    )
    decode = (
        RidgeFactorModel.from_dict
        if manifest["kind"] == "ridge"
        else TreeModel.from_dict
    )
    model.models = sorted(
        (decode(item) for item in manifest["models"]), key=lambda m: m.train_end
    )
    return model


# Warm scores per (model, symbol set) and symbol, indexed by 4h close. A
# score is warm when its window held WARMUP_DAYS before it; warm scores do not
# depend on where the window started, so they carry across the sliding windows
# live ticks and the forward-parity replay hand precompute every bar. A score
# changes only when a 4h bar closes: between closes a call reads the cache.
_SCORE_CACHE: dict[tuple[Any, ...], dict[str, pd.DataFrame]] = {}
# The last full pass per key, cold rows included, reused for the same window
# start and 4h close: a bounded replay's opening month grows from one bar and
# must see the scores the one-pass backtest computed for those bars.
_RECENT_SCORES: dict[
    tuple[Any, ...], tuple[pd.Timestamp, pd.Timestamp, dict[str, pd.DataFrame]]
] = {}
# Each key's previous window (start, time span). The warm cache serves only a
# live-style slide of it: same span, start a little later (row counts move
# with markets that skip bars, so the span is the window's length). Any other call (a
# backtest over a new slice) takes a full pass, so a backtest never depends on
# what ran before it in the process.
_LAST_WINDOW: dict[tuple[Any, ...], tuple[pd.Timestamp, pd.Timedelta]] = {}
_SLIDE_LIMIT = pd.Timedelta(days=1)
_SCORE_CACHE_LIMIT = 64


def _cache_key(model: TrainedModel, symbols: tuple[str, ...]) -> tuple[Any, ...]:
    return (
        model.name,
        tuple(model.features),
        model.horizon,
        model.models[0].train_end,
        symbols,
    )


def _same_closes(
    cached: Mapping[str, pd.DataFrame],
    frames: Mapping[str, pd.DataFrame],
    stamps_by_symbol: Mapping[str, pd.DatetimeIndex],
    stamp: pd.Timestamp,
) -> bool:
    """The cache holds a series, not a dataset: serve it only when these
    frames carry the same closes at the stamp."""
    matched = 0
    for symbol, frame in frames.items():
        series = cached.get(symbol)
        if series is None or stamp not in series.index:
            continue
        stamps = stamps_by_symbol[symbol]
        at = stamps.searchsorted(stamp, side="right") - 1
        if at < 0 or stamps[at] != stamp:
            continue
        if not np.isclose(
            float(frame["close"].iloc[at]),
            float(series.at[stamp, "close_4h"]),
            rtol=1e-12,
            atol=0,
        ):
            return False
        matched += 1
    return matched > 0


def _utc_stamps(column: pd.Series) -> pd.DatetimeIndex:
    # pd.to_datetime walks every value of an already-parsed column to decide
    # on caching; this runs every live tick.
    if isinstance(column.dtype, pd.DatetimeTZDtype):
        return pd.DatetimeIndex(column).tz_convert("UTC")
    return pd.DatetimeIndex(pd.to_datetime(column, utc=True))


def _nan_scores(frames: Mapping[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    return {
        symbol: pd.DataFrame(
            {"model_score": np.nan, "model_rank": np.nan}, index=range(len(frame))
        )
        for symbol, frame in frames.items()
    }


def _pass_scores(
    frames: Mapping[str, pd.DataFrame], model: TrainedModel
) -> dict[str, pd.DataFrame]:
    panel = model_panel(bars_from_frames(frames), HORIZONS[model.horizon])
    scored = model.score(panel).merge(
        panel[["timestamp", "symbol", "close_4h"]], on=["timestamp", "symbol"]
    )
    return {
        str(symbol): rows.set_index("timestamp")[
            ["model_score", "model_rank", "close_4h"]
        ].sort_index()
        for symbol, rows in scored.groupby("symbol")
    }


def _carry_warm(
    full: Mapping[str, pd.DataFrame],
    warm_from: pd.Timestamp,
    previous: Mapping[str, pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    warm: dict[str, pd.DataFrame] = {}
    for symbol, series in full.items():
        series = series[series.index >= warm_from]
        if symbol in previous:
            series = pd.concat([previous[symbol], series])
            series = series[~series.index.duplicated(keep="last")].sort_index()
        warm[symbol] = series
    return warm


def model_scores(
    frames: Mapping[str, pd.DataFrame], model: TrainedModel
) -> dict[str, pd.DataFrame]:
    """For ``precompute``: per symbol, ``model_score`` and ``model_rank``
    (-0.5 worst .. +0.5 best in the universe) aligned to the strategy's own
    bars, stepped forward from each 4h close. Rows inside the window's first
    ``WARMUP_DAYS`` are scored from the shorter history the window has (a
    35-day screen trades on them), except on a live-style slide, which reads
    warm scores from the cache and leaves those rows NaN: its latest row is
    always warm, and it is the row a live decision reads."""
    stamps_by_symbol = {
        symbol: _utc_stamps(frame["timestamp"]) for symbol, frame in frames.items()
    }
    nonempty = [stamps for stamps in stamps_by_symbol.values() if len(stamps)]
    if not nonempty:
        return _nan_scores(frames)
    first_bar = min(stamps[0] for stamps in nonempty)
    last_bar = max(stamps[-1] for stamps in nonempty)
    latest_stamp = last_bar.floor(TIMEFRAME)
    warm_from = first_bar + pd.Timedelta(days=WARMUP_DAYS)
    key = _cache_key(model, tuple(sorted(frames)))
    cached = _SCORE_CACHE.get(key)
    recent = _RECENT_SCORES.get(key)
    span = last_bar - first_bar
    last = _LAST_WINDOW.get(key)
    _LAST_WINDOW[key] = (first_bar, span)
    sliding = (
        last is not None
        and last[1] == span
        and last[0] <= first_bar <= last[0] + _SLIDE_LIMIT
    )
    if (
        recent is not None
        and recent[:2] == (first_bar, latest_stamp)
        and _same_closes(recent[2], frames, stamps_by_symbol, latest_stamp)
    ):
        source, cold_from = recent[2], None
    elif (
        sliding
        and latest_stamp >= warm_from
        and cached is not None
        and _same_closes(cached, frames, stamps_by_symbol, latest_stamp)
    ):
        source, cold_from = cached, warm_from
    else:
        full = _pass_scores(frames, model)
        # Warm rows from earlier windows carry over while this window agrees
        # with them one 4h bar back; otherwise the cache starts over.
        previous = (
            cached
            if cached is not None
            and _same_closes(
                cached, frames, stamps_by_symbol, latest_stamp - pd.Timedelta(TIMEFRAME)
            )
            else {}
        )
        if len(_SCORE_CACHE) >= _SCORE_CACHE_LIMIT:
            _SCORE_CACHE.clear()
            _RECENT_SCORES.clear()
            _LAST_WINDOW.clear()
            _LAST_WINDOW[key] = (first_bar, span)
        _SCORE_CACHE[key] = _carry_warm(full, warm_from, previous)
        _RECENT_SCORES[key] = (first_bar, latest_stamp, full)
        source, cold_from = full, None
    out = {}
    for symbol, stamps in stamps_by_symbol.items():
        series = source.get(symbol)
        if series is None or series.empty:
            out[symbol] = pd.DataFrame(
                {"model_score": np.nan, "model_rank": np.nan}, index=range(len(stamps))
            )
            continue
        # Step each score forward from its 4h close (a searchsorted ffill: this
        # runs every live tick). Cached rows from before this window's warm
        # boundary belong to earlier windows, not this one.
        first = 0 if cold_from is None else series.index.searchsorted(cold_from)
        at = series.index.searchsorted(stamps, side="right") - 1
        values = series[["model_score", "model_rank"]].to_numpy(float)[
            np.maximum(at, 0)
        ]
        values[at < first] = np.nan
        out[symbol] = pd.DataFrame(values, columns=["model_score", "model_rank"])
    return out
