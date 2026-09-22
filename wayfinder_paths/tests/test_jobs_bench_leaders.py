"""Frozen leader closes: fetched once beside a source job, read by worlds."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

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


def test_freeze_funding_resolves_the_store_and_forwards_the_fetch(
    tmp_path, monkeypatch
) -> None:
    from wayfinder_paths.jobs.bench.cli import main as bench_main
    from wayfinder_paths.jobs.bench.leaders import freeze_funding_features

    captured: dict = {}

    def fake_fetch(job_id, *, days, exchange, store, exchange_client=None):
        captured.update(
            {
                "job_id": job_id,
                "days": days,
                "exchange": exchange,
                "repo_root": store.repo_root,
                "client": exchange_client,
            }
        )
        return {"rows_fetched": 3, "feature_declared_now": True}

    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.preflight.fetch_funding_features", fake_fetch
    )
    source_job = tmp_path / ".wayfinder" / "jobs" / "bench-source"
    source_job.mkdir(parents=True)
    result = freeze_funding_features(
        source_job, days=7, exchange_id="bybit", exchange="x"
    )
    assert result["rows_fetched"] == 3
    assert captured["job_id"] == "bench-source" and captured["days"] == 7
    assert captured["exchange"] == "bybit" and captured["client"] == "x"
    assert Path(captured["repo_root"]).resolve() == tmp_path.resolve()
    with pytest.raises(ValueError, match="must be a .wayfinder/jobs"):
        freeze_funding_features(tmp_path, days=1)
    # The bench CLI wraps it (network once, at freeze time).
    captured.clear()
    bench_main(["freeze-funding", str(source_job), "--days", "3"])
    assert captured["job_id"] == "bench-source" and captured["days"] == 3
    assert captured["exchange"] == "binance"
