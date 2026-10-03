"""Fit the walk-forward factor-model artifacts the `factor` derived-feature set scores with.

usage: poetry run python scripts/train_factor_models.py <bars.parquet> [--out PATH] [--first 2023-01-01]

<bars.parquet> holds OHLCV rows (timestamp, symbol, open, high, low, close, volume) at 4h
or finer; finer bars are resampled to 4h. One model per 30 days from --first: each is
fitted on targets realized before its train_end, so the feature store never scores a bar
with a model that saw it. Retrain monthly on fresh bars; old artifacts stay valid for the
history they already scored.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
from pathlib import Path

import pandas as pd

from wayfinder_paths.jobs.factor_model import (
    DEFAULT_ARTIFACT,
    FACTOR_HORIZON_BARS,
    FACTOR_TIMEFRAME,
    factor_panel,
    fit_walk_forward,
    resample_bars,
    save_models,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("bars")
    parser.add_argument("--out", default=str(DEFAULT_ARTIFACT))
    parser.add_argument("--first", default="2023-01-01")
    args = parser.parse_args()
    raw = pd.read_parquet(args.bars)
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    bars = resample_bars(raw, FACTOR_TIMEFRAME)
    panel = factor_panel(bars, FACTOR_HORIZON_BARS)
    models = fit_walk_forward(panel, first_train_end=pd.Timestamp(args.first, tz="UTC"))
    save_models(
        models,
        Path(args.out),
        provenance={
            "bars": Path(args.bars).name,
            "bars_sha1": hashlib.sha1(Path(args.bars).read_bytes()).hexdigest()[:12],
            "symbols": sorted(bars["symbol"].unique().tolist()),
            "first_bar": bars["timestamp"].min().isoformat(),
            "last_bar": bars["timestamp"].max().isoformat(),
            "timeframe": FACTOR_TIMEFRAME,
            "horizon_bars": FACTOR_HORIZON_BARS,
            "trained_at": dt.datetime.now(dt.UTC).isoformat(),
        },
    )
    print(
        f"{len(models)} models, train_end {models[0].train_end} .. {models[-1].train_end} -> {args.out}"
    )


if __name__ == "__main__":
    main()
