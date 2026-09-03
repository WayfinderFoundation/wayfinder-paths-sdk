"""Frozen leader closes: fetched once beside a source job, read by worlds."""

from __future__ import annotations

import json
import time
from pathlib import Path

from wayfinder_paths.jobs.bench.leaders import (
    LEADER_CLOSES_RELATIVE,
    freeze_leader_closes,
    load_leader_closes,
)
from wayfinder_paths.tests.test_execution_ccxt_feed import (
    HOUR_MS,
    FakeCcxtExchange,
    _candles,
)


def test_freeze_leader_closes_writes_the_file_from_a_fake_exchange(
    tmp_path: Path,
) -> None:
    # fetch_ccxt_dataset_rows looks back from wall-clock now; the fake's
    # candles must sit inside that window.
    recent_start = (int(time.time() * 1000) // HOUR_MS - 50) * HOUR_MS
    exchange = FakeCcxtExchange(
        markets={"BTC/USDT:USDT": {"active": True}, "ETH/USDT:USDT": {"active": True}},
        candles={
            "BTC/USDT:USDT": _candles(48, start_ms=recent_start),
            "ETH/USDT:USDT": _candles(48, start_ms=recent_start),
        },
    )

    result = freeze_leader_closes(tmp_path, days=2, exchange=exchange)

    assert result["path"] == str(tmp_path / LEADER_CLOSES_RELATIVE)
    assert result["rows"] > 0 and result["first"] < result["last"]
    payload = json.loads((tmp_path / LEADER_CLOSES_RELATIVE).read_text())
    assert payload["metadata"]["interval"] == "1h"
    assert payload["metadata"]["symbols"] == ["BTC", "ETH"]
    assert {row["symbol"] for row in payload["closes"]} == {"BTC", "ETH"}

    loaded = load_leader_closes(tmp_path)
    assert loaded is not None
    frame, metadata = loaded
    assert list(frame.columns) == ["BTC", "ETH"]
    assert frame.index.tz is not None and frame.index.is_monotonic_increasing
    assert metadata["days"] == 2


def test_load_leader_closes_returns_none_when_absent(tmp_path: Path) -> None:
    assert load_leader_closes(tmp_path) is None
