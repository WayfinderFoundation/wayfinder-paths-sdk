"""Run backtests on the configured remote runner (``backtest_runner``).

Remote is an override, never the default: a remote ``provider`` with
``offload_operations: true`` in config.json. This node fetches the data, since it holds
the credentials; only the computation travels, as a registered compute phase over the
fetched frames. Frames and results travel as JSON
that keeps every dtype and float exactly, never pickle, so nothing a remote machine
returns can run code here.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
from loguru import logger

from wayfinder_paths.core.backtesting.types import (
    BacktestConfig,
    BacktestResult,
    BacktestStats,
)
from wayfinder_paths.jobs.backtest_runner import (
    RunnerConfig,
    load_runner_config,
    offload_switched_off,
    run_phase,
)

RESULT_FILE = "backtest-result.json"
# Set in heavy-lane operation children (wayfinder_paths.jobs.heavy_lane.STATUS_PATH_ENV).
_OPERATION_ENV = "WAYFINDER_OP_STATUS_PATH"
_FRAMES = ("metrics_by_period", "positions_over_time")
_SERIES = ("equity_curve", "returns")


def remote_runner(root: Path) -> RunnerConfig | None:
    """The remote runner when the override is on and this call may book it.

    Only a top-level call offloads. An agent operation is offloaded, or not, whole at its
    own boundary; worker threads and processes of a computation stay with it, and
    run_phase's signal handlers need the main thread anyway.
    """
    if (
        _OPERATION_ENV in os.environ
        or threading.current_thread() is not threading.main_thread()
        or multiprocessing.parent_process() is not None
    ):
        return None
    config = load_runner_config(repo_root=root)
    if (
        config.configured
        and config.provider != "local"
        and config.offload_operations
        and offload_switched_off(config) is None
    ):
        return config
    return None


def run_backtest_phase(
    phase: Callable[..., Any],
    root: Path,
    runner: RunnerConfig,
    frames: Mapping[str, pd.DataFrame | None],
    args: Mapping[str, Any],
    config: BacktestConfig,
) -> BacktestResult:
    """Ship ``frames`` and ``config`` to ``phase`` on ``runner`` and rebuild its result.

    Blocks until the remote run finishes: run_phase installs signal handlers, which only
    the main thread may do, so it cannot move to a worker thread.
    """
    logger.info(
        "Running {} on the {} runner (preset {})",
        phase.__name__,
        runner.provider,
        runner.preset,
    )
    staging = root / ".wayfinder" / "backtest_inputs"
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=staging) as directory:
        paths = {}
        for name, frame in frames.items():
            if frame is not None:
                path = Path(directory) / f"{name}.json"
                path.write_text(json.dumps(encode_frame(frame)))
                paths[name] = str(path.relative_to(root))
        outcome = run_phase(
            phase,
            root,
            list(paths.values()),
            {**args, "frames": paths, "config": config_fields(config)},
            config=runner,
            purpose=f"backtest:{phase.__name__}",
        )
    destination = outcome["run"]["destination"]
    logger.info(
        "{} finished on {}",
        phase.__name__,
        " ".join(f"{key}={value}" for key, value in destination.items()),
    )
    return read_result(Path(outcome["outputs_path"]))


def read_frames(inputs: Path, args: Mapping[str, Any]) -> dict[str, pd.DataFrame]:
    return {
        name: decode_frame(json.loads((inputs / path).read_text()))
        for name, path in args["frames"].items()
    }


def config_fields(config: BacktestConfig) -> dict[str, Any]:
    # Funding rates are data the phase rebuilds from its frames, not configuration.
    return {
        field.name: getattr(config, field.name)
        for field in fields(config)
        if field.name != "funding_rates"
    }


def write_result(result: BacktestResult, outputs: Path) -> dict[str, Any]:
    timestamp = result.liquidation_timestamp
    document = {
        "stats": {key: _scalar(value) for key, value in result.stats.items()},
        "trades": [
            {key: _scalar(value) for key, value in trade.items()}
            for trade in result.trades
        ],
        "series": {
            name: encode_frame(getattr(result, name).to_frame("value"))
            | {"name": getattr(result, name).name}
            for name in _SERIES
        },
        "frames": {name: encode_frame(getattr(result, name)) for name in _FRAMES},
        "liquidated": result.liquidated,
        "liquidation_timestamp": _scalar(timestamp) if timestamp is not None else None,
    }
    (outputs / RESULT_FILE).write_text(json.dumps(document))
    return {"stats": document["stats"], "liquidated": result.liquidated}


def read_result(outputs: Path) -> BacktestResult:
    document = json.loads((outputs / RESULT_FILE).read_text())
    timestamp = document["liquidation_timestamp"]
    return BacktestResult(
        **{
            name: decode_frame(encoded)["value"].rename(encoded["name"])
            for name, encoded in document["series"].items()
        },
        **{name: decode_frame(encoded) for name, encoded in document["frames"].items()},
        stats=cast(
            BacktestStats,
            {key: _unscalar(value) for key, value in document["stats"].items()},
        ),
        trades=[
            {key: _unscalar(value) for key, value in trade.items()}
            for trade in document["trades"]
        ],
        liquidated=document["liquidated"],
        liquidation_timestamp=_unscalar(timestamp) if timestamp is not None else None,
    )


def encode_frame(frame: pd.DataFrame) -> dict[str, Any]:
    return {
        "index": _encode_column(frame.index.to_series()) | {"name": frame.index.name},
        "columns": [[name, _encode_column(frame[name])] for name in frame.columns],
    }


def decode_frame(encoded: Mapping[str, Any]) -> pd.DataFrame:
    index = pd.Index(_decode_column(encoded["index"]), name=encoded["index"]["name"])
    return pd.DataFrame(
        {name: _decode_column(column).array for name, column in encoded["columns"]},
        index=index,
    )


def _encode_column(values: pd.Series) -> dict[str, Any]:
    dtype = values.dtype
    if dtype.kind in "mM":
        # Integer nanoseconds (UTC when tz-aware, NaT as numpy's sentinel) round-trip exactly.
        tz = getattr(dtype, "tz", None)
        return {
            "dtype": str(dtype),
            "tz": str(tz) if tz is not None else None,
            "ns": pd.Index(values).as_unit("ns").asi8.tolist(),
        }
    return {
        "dtype": str(dtype),
        "values": [_scalar(value) for value in values.tolist()],
    }


def _decode_column(encoded: Mapping[str, Any]) -> pd.Series:
    if "ns" not in encoded:
        return pd.Series(
            [_unscalar(value) for value in encoded["values"]], dtype=encoded["dtype"]
        )
    kind = "timedelta64" if encoded["dtype"].startswith("timedelta") else "datetime64"
    column = pd.Series(np.array(encoded["ns"], dtype="int64").view(f"{kind}[ns]"))
    if encoded["tz"] is not None:
        column = column.dt.tz_localize("UTC").dt.tz_convert(encoded["tz"])
    return column.astype(encoded["dtype"])


def _scalar(value: Any) -> Any:
    """A JSON value for one cell; floats (NaN included) keep every bit through json."""
    if value is pd.NaT:
        return {"nat": True}
    if isinstance(value, pd.Timestamp):
        return {"timestamp": value.isoformat()}
    if isinstance(value, pd.Timedelta):
        return {"timedelta_ns": value.value}
    if isinstance(value, np.generic):
        return value.item()
    return value


def _unscalar(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if "timestamp" in value:
        return pd.Timestamp(value["timestamp"])
    if "timedelta_ns" in value:
        return pd.Timedelta(value["timedelta_ns"], unit="ns")
    return pd.NaT
