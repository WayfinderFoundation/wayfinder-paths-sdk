"""Cross-sectional factor model: a ridge over causal OHLCV factors that ranks
symbols by their predicted next-day return relative to the panel.

Evidence (research study 2026-10-03, walk-forward, 8 bps per side): on 4h bars
with a one-day horizon the score ranked next-day relative returns at an
information coefficient of about +0.03 to +0.07, and a market-neutral book in
its 10% tails earned net Sharpe +0.3 to +1.9 across Binance (31 symbols, two
years out of sample) and Hyperliquid (20 symbols, six months). A model fitted
on Binance alone scored Hyperliquid at net Sharpe +1.65, three quarters of
three positive. Intraday horizons carried more information but lost to costs.

Training is offline (``fit_walk_forward``); inference reads shipped artifacts.
Each artifact records ``train_end``: a bar is scored only by the newest model
whose training targets were all realized before it, so a backtest never sees a
model fitted on its own future.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

FACTOR_PRED_FEATURE = "xs_factor_pred"
FACTOR_RANK_FEATURE = "xs_factor_rank"
FACTOR_TIMEFRAME = "4h"
FACTOR_HORIZON_BARS = 6
FACTOR_WARMUP_BARS = 130
# A cross-section needs breadth: on the 4-symbol majors world the score had
# no information (IC -0.02), on a 10-symbol world +0.07.
MIN_FACTOR_SYMBOLS = 8
DEFAULT_ARTIFACT = Path(__file__).parent / "data" / "factor_models" / "ridge_4h_h6.json"

_RET_LAGS = (1, 2, 4, 8, 16, 32, 64, 128)
_RANK_LAGS = (4, 16, 64)
_MARKET_LAGS = (1, 4, 16, 64)
_LEADER = "BTC"
_STANDARD_CLIP = 5.0


def resample_bars(bars: pd.DataFrame, rule: str = FACTOR_TIMEFRAME) -> pd.DataFrame:
    """Close-labelled OHLCV bars per symbol: the bar stamped t holds trades up
    to t, matching the execution engine's completed-bar convention. A trailing
    bin still missing source bars is dropped: its label is in the future, and
    the append-only store would keep the partial value forever."""
    out = []
    for symbol, frame in bars.groupby("symbol"):
        stamps = pd.to_datetime(frame["timestamp"], utc=True)
        agg = (
            frame.set_index(stamps)
            .resample(rule, label="right", closed="right")
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )
            .dropna()
        )
        step = (
            stamps.sort_values().diff().median() if len(stamps) > 1 else pd.Timedelta(0)
        )
        agg = agg[agg.index - stamps.max() <= step]
        agg["symbol"] = symbol
        out.append(agg.rename_axis("timestamp").reset_index())
    return pd.concat(out).sort_values(["symbol", "timestamp"]).reset_index(drop=True)


def _symbol_factors(frame: pd.DataFrame) -> pd.DataFrame:
    close = frame["close"]
    logc = np.log(close)
    r1 = logc.diff()
    f = pd.DataFrame(index=frame.index)
    for k in _RET_LAGS:
        f[f"r_{k}"] = logc - logc.shift(k)
    for k in (16, 64):
        f[f"vol_{k}"] = r1.rolling(k).std()
    f["vol_ratio"] = f["vol_16"] / f["vol_64"]
    hl = np.log(frame["high"] / frame["low"])
    f["range_16"] = hl.rolling(16).mean()
    f["park_vol_16"] = np.sqrt((hl**2).rolling(16).mean() / (4 * np.log(2)))
    for k in (20, 50, 100):
        f[f"z_ma_{k}"] = (close - close.rolling(k).mean()) / close.rolling(k).std()
    f["dist_hi_64"] = close / frame["high"].rolling(64).max() - 1
    f["dist_lo_64"] = close / frame["low"].rolling(64).min() - 1
    dollar = np.log1p(frame["volume"] * close)
    f["vol_z_64"] = (dollar - dollar.rolling(64).mean()) / dollar.rolling(64).std()
    f["dollar_vol_64"] = dollar.rolling(64).mean()
    # A bounded window: an open-ended EWM would differ between a live view and
    # the full backtest history.
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    f["rsi_14"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    span = (frame["high"] - frame["low"]).replace(0, np.nan)
    f["body"] = (frame["close"] - frame["open"]) / span
    f["upper_wick"] = (frame["high"] - frame[["open", "close"]].max(axis=1)) / span
    f["lower_wick"] = (frame[["open", "close"]].min(axis=1) - frame["low"]) / span
    stamps = pd.to_datetime(frame["timestamp"], utc=True)
    hour = stamps.dt.hour + stamps.dt.minute / 60
    f["hour_sin"] = np.sin(2 * np.pi * hour / 24)
    f["hour_cos"] = np.cos(2 * np.pi * hour / 24)
    f["dow_sin"] = np.sin(2 * np.pi * stamps.dt.dayofweek / 7)
    f["dow_cos"] = np.cos(2 * np.pi * stamps.dt.dayofweek / 7)
    return f


def factor_panel(
    bars: pd.DataFrame, horizon_bars: int = FACTOR_HORIZON_BARS
) -> pd.DataFrame:
    """Factors at each bar close plus ``y_xs``: the log return to the close
    ``horizon_bars`` later minus that bar's cross-sectional mean (NaN at the
    tail, where the future is not yet known)."""
    parts = []
    for symbol, frame in bars.groupby("symbol", sort=False):
        frame = frame.reset_index(drop=True)
        f = _symbol_factors(frame)
        logc = np.log(frame["close"])
        f["y"] = logc.shift(-horizon_bars) - logc
        f["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        f["symbol"] = symbol
        parts.append(f)
    panel = pd.concat(parts, ignore_index=True)
    grouped = panel.groupby("timestamp")
    for k in _RANK_LAGS:
        panel[f"xs_rank_r_{k}"] = grouped[f"r_{k}"].rank(pct=True)
    panel["xs_rank_vol_64"] = grouped["vol_64"].rank(pct=True)
    panel["xs_rank_vol_z"] = grouped["vol_z_64"].rank(pct=True)
    for k in _MARKET_LAGS:
        market = grouped[f"r_{k}"].transform("mean")
        panel[f"mkt_r_{k}"] = market
        panel[f"idio_r_{k}"] = panel[f"r_{k}"] - market
    panel["dispersion_4"] = grouped["r_4"].transform("std")
    leader_columns = [f"r_{k}" for k in _MARKET_LAGS] + ["vol_64"]
    leader = panel.loc[
        panel["symbol"] == _LEADER, ["timestamp", *leader_columns]
    ].rename(
        columns={
            **{f"r_{k}": f"lead_r_{k}" for k in _MARKET_LAGS},
            "vol_64": "lead_vol_64",
        }
    )
    panel = panel.merge(leader, on="timestamp", how="left")
    panel["y_xs"] = panel["y"] - panel.groupby("timestamp")["y"].transform("mean")
    return panel.drop(columns=["y"])


@dataclass(frozen=True)
class RidgeFactorModel:
    features: list[str]
    lower: np.ndarray
    upper: np.ndarray
    mean: np.ndarray
    scale: np.ndarray
    coef: np.ndarray
    intercept: float
    train_end: pd.Timestamp
    horizon_bars: int
    timeframe: str

    def predict(self, panel: pd.DataFrame) -> np.ndarray:
        # A factor absent from this panel (no BTC in the universe for the
        # leader block) sits at its training mean, i.e. contributes nothing.
        matrix = panel.reindex(columns=self.features).to_numpy(dtype=float)
        standard = (np.clip(matrix, self.lower, self.upper) - self.mean) / self.scale
        standard = np.nan_to_num(np.clip(standard, -_STANDARD_CLIP, _STANDARD_CLIP))
        return standard @ self.coef + self.intercept

    def to_dict(self) -> dict[str, Any]:
        return {
            "features": list(self.features),
            "lower": self.lower.tolist(),
            "upper": self.upper.tolist(),
            "mean": self.mean.tolist(),
            "scale": self.scale.tolist(),
            "coef": self.coef.tolist(),
            "intercept": float(self.intercept),
            "train_end": self.train_end.isoformat(),
            "horizon_bars": self.horizon_bars,
            "timeframe": self.timeframe,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RidgeFactorModel:
        return cls(
            features=list(data["features"]),
            lower=np.asarray(data["lower"], dtype=float),
            upper=np.asarray(data["upper"], dtype=float),
            mean=np.asarray(data["mean"], dtype=float),
            scale=np.asarray(data["scale"], dtype=float),
            coef=np.asarray(data["coef"], dtype=float),
            intercept=float(data["intercept"]),
            train_end=pd.Timestamp(data["train_end"]),
            horizon_bars=int(data["horizon_bars"]),
            timeframe=str(data["timeframe"]),
        )


def feature_columns(panel: pd.DataFrame) -> list[str]:
    return [c for c in panel.columns if c not in ("timestamp", "symbol", "y_xs")]


def fit_ridge(
    panel: pd.DataFrame,
    *,
    train_end: pd.Timestamp,
    bar: pd.Timedelta,
    horizon_bars: int = FACTOR_HORIZON_BARS,
    timeframe: str = FACTOR_TIMEFRAME,
    alpha: float = 100.0,
) -> RidgeFactorModel:
    """Closed-form ridge on standardized factors, fitted only on rows whose
    target was realized by ``train_end``. Rows are thinned to every
    ``horizon_bars // 2`` stamps: overlapping targets add no information."""
    features = feature_columns(panel)
    last_label = train_end - bar * horizon_bars
    stamps = pd.DatetimeIndex(panel["timestamp"].unique()).sort_values()
    keep = stamps[(stamps <= last_label)][:: max(1, horizon_bars // 2)]
    rows = panel[panel["timestamp"].isin(keep) & panel["y_xs"].notna()]
    matrix = rows[features].to_numpy(dtype=float)
    lower = np.nanquantile(matrix, 0.01, axis=0)
    upper = np.nanquantile(matrix, 0.99, axis=0)
    clipped = np.clip(matrix, lower, upper)
    mean = np.nanmean(clipped, axis=0)
    scale = np.nanstd(clipped, axis=0, ddof=1)
    scale = np.where(scale > 0, scale, 1.0)
    x = np.nan_to_num(
        np.clip((clipped - mean) / scale, -_STANDARD_CLIP, _STANDARD_CLIP)
    )
    target = rows["y_xs"].to_numpy(dtype=float)
    target = np.clip(target, *np.quantile(target, [0.005, 0.995]))
    x_mean, y_mean = x.mean(axis=0), target.mean()
    centred = x - x_mean
    coef = np.linalg.solve(
        centred.T @ centred + alpha * np.eye(len(features)),
        centred.T @ (target - y_mean),
    )
    return RidgeFactorModel(
        features=features,
        lower=lower,
        upper=upper,
        mean=mean,
        scale=scale,
        coef=coef,
        intercept=float(y_mean - x_mean @ coef),
        train_end=pd.Timestamp(train_end),
        horizon_bars=horizon_bars,
        timeframe=timeframe,
    )


def fit_walk_forward(
    panel: pd.DataFrame,
    *,
    first_train_end: pd.Timestamp,
    refit: pd.Timedelta = pd.Timedelta(days=30),
    bar: pd.Timedelta = pd.Timedelta(FACTOR_TIMEFRAME),
    min_rows: int = 5_000,
    **kwargs: Any,
) -> list[RidgeFactorModel]:
    models = []
    train_end = pd.Timestamp(first_train_end)
    last = panel["timestamp"].max()
    while train_end <= last:
        model = fit_ridge(panel, train_end=train_end, bar=bar, **kwargs)
        rows = panel["timestamp"] <= train_end - bar * model.horizon_bars
        if int(rows.sum()) >= min_rows:
            models.append(model)
        train_end += refit
    return models


def save_models(
    models: Sequence[RidgeFactorModel], path: Path, *, provenance: dict[str, Any]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"provenance": provenance, "models": [m.to_dict() for m in models]},
            indent=1,
        )
    )


def load_models(path: Path = DEFAULT_ARTIFACT) -> list[RidgeFactorModel]:
    payload = json.loads(Path(path).read_text())
    models = [RidgeFactorModel.from_dict(m) for m in payload["models"]]
    return sorted(models, key=lambda m: m.train_end)


def score_panel(
    panel: pd.DataFrame, models: Sequence[RidgeFactorModel]
) -> pd.DataFrame:
    """``xs_factor_pred`` (predicted relative log return over the horizon) and
    ``xs_factor_rank`` (its cross-sectional rank, centred: -0.5 worst to +0.5
    best) for every row that has a model fitted strictly before it."""
    ends = pd.DatetimeIndex([m.train_end for m in models])
    choice = ends.searchsorted(panel["timestamp"], side="left") - 1
    out = panel[["timestamp", "symbol"]].copy()
    out[FACTOR_PRED_FEATURE] = np.nan
    for index in np.unique(choice[choice >= 0]):
        rows = choice == index
        out.loc[rows, FACTOR_PRED_FEATURE] = models[index].predict(panel.loc[rows])
    scored = out.dropna(subset=[FACTOR_PRED_FEATURE]).copy()
    scored[FACTOR_RANK_FEATURE] = (
        scored.groupby("timestamp")[FACTOR_PRED_FEATURE].rank(pct=True) - 0.5
    )
    return scored


def factor_feature_frames(
    bars: pd.DataFrame,
    index: pd.DatetimeIndex,
    models: Sequence[RidgeFactorModel] | None = None,
) -> dict[str, pd.DataFrame]:
    """The two factor features as wide frames (``index`` x symbol), stepped
    forward from each 4h score to the job's own bars. Empty below
    MIN_FACTOR_SYMBOLS."""
    if bars["symbol"].nunique() < MIN_FACTOR_SYMBOLS:
        return {}
    panel = factor_panel(resample_bars(bars))
    scored = score_panel(panel, models if models is not None else load_models())
    return {
        name: scored.pivot(index="timestamp", columns="symbol", values=name)
        .sort_index()
        .reindex(index, method="ffill")
        for name in (FACTOR_PRED_FEATURE, FACTOR_RANK_FEATURE)
    }


def factor_store_rows(
    frames: dict[str, pd.DataFrame], *, stamps: pd.DatetimeIndex, written_at: str
) -> list[dict[str, Any]]:
    """Per-symbol feature-store rows (the shape ``merge_features`` reads)."""
    rows = []
    for name, wide in frames.items():
        sampled = wide.loc[wide.index.intersection(stamps)]
        for symbol in sampled.columns:
            for stamp, value in sampled[symbol].dropna().items():
                rows.append(
                    {
                        "timestamp": pd.Timestamp(stamp).isoformat(),
                        "name": name,
                        "value": round(float(value), 8),
                        "symbol": str(symbol),
                        "written_at": written_at,
                    }
                )
    rows.sort(key=lambda row: (row["timestamp"], row["name"], row["symbol"]))
    return rows
