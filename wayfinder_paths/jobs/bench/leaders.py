"""Frozen leader closes for benchmark worlds.

A benchmark world has no network and no leader bars (the panel is the
job's own symbols), so the broad-market leader state is derived from a
close series frozen once beside the source job's bars, at the panel's
freeze, and split at each loop's cutoff like every other derived feature.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.bench.env import atomic_json, sha256_file
from wayfinder_paths.jobs.execution.ccxt_feed import fetch_ccxt_dataset_rows
from wayfinder_paths.jobs.regime import LEADER_SYMBOLS

LEADER_CLOSES_RELATIVE = Path("results") / "backtest" / "leader_closes.json"
LEADER_INTERVAL = "1h"


def freeze_leader_closes(
    source_job: Path,
    *,
    days: int,
    symbols: tuple[str, ...] = LEADER_SYMBOLS,
    exchange_id: str = "binance",
    exchange: Any | None = None,
) -> dict[str, Any]:
    """Fetch ``days`` of hourly leader closes and write them beside the
    source job's bars. Network once, at freeze time; worlds read the file."""
    rows, source_metadata = asyncio.run(
        fetch_ccxt_dataset_rows(
            list(symbols),
            LEADER_INTERVAL,
            days=int(days),
            exchange_id=exchange_id,
            exchange=exchange,
        )
    )
    closes = sorted(
        (
            {
                "timestamp": pd.Timestamp(row["timestamp"]).isoformat(),
                "symbol": str(row["symbol"]),
                "close": float(row["close"]),
            }
            for row in rows
        ),
        key=lambda row: (row["timestamp"], row["symbol"]),
    )
    if not closes:
        raise ValueError("leader fetch returned no closes")
    payload = {
        "metadata": {
            **source_metadata,
            "interval": LEADER_INTERVAL,
            "days": int(days),
            "symbols": list(symbols),
            "frozen_at": datetime.now(UTC).isoformat(),
        },
        "closes": closes,
    }
    target = source_job / LEADER_CLOSES_RELATIVE
    atomic_json(target, payload)
    return {
        "path": str(target),
        "rows": len(closes),
        "first": closes[0]["timestamp"],
        "last": closes[-1]["timestamp"],
        "sha256": sha256_file(target),
    }


def load_leader_closes(
    source_job: Path,
) -> tuple[pd.DataFrame, dict[str, Any]] | None:
    """The frozen leader closes as a wide frame (UTC index, one column per
    leader) with the file's metadata; None when the source has no file."""
    path = source_job / LEADER_CLOSES_RELATIVE
    if not path.exists():
        return None
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    frame = pd.DataFrame(payload.get("closes") or [])
    if frame.empty:
        return None
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    wide = (
        frame.pivot_table(
            index="timestamp", columns="symbol", values="close", aggfunc="last"
        )
        .sort_index()
        .astype(float)
    )
    return wide, dict(payload.get("metadata") or {})
