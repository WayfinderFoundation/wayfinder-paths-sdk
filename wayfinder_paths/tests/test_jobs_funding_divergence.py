"""Funding-divergence starters: the signal, both execution styles, the
open-interest confirmation, and the wake-path feed refresh that keeps their
funding and open-interest features live."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from wayfinder_paths.jobs.derived_features import (
    refresh_declared_feeds,
    refresh_derived_features_if_stale,
)
from wayfinder_paths.jobs.execution.features import apply_precompute
from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    ExecutionContext,
    ExecutionSpec,
    PositionLedger,
    PositionRecord,
    StateSnapshot,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.strategies.mixed_funding_divergence import (
    MixedFundingDivergenceStrategy,
)

WINDOW = 960


def _rows(
    *,
    crowded: str,
    price_confirms: bool,
    oi_growing: bool = True,
    with_funding: bool = True,
    with_oi: bool = True,
    bars: int = WINDOW + 200,
) -> list[dict[str, Any]]:
    """A flat book whose last 24 bars carry an extreme funding print while the
    trailing day's return refuses (or agrees) to confirm the crowd."""
    stamps = pd.date_range("2026-01-01T00:00:00Z", periods=bars, freq="15min")
    rows: list[dict[str, Any]] = []
    for symbol in ("AAA", "BBB"):
        close = np.full(bars, 100.0)
        drift = np.linspace(0.0, 1.0, 96)
        if crowded == "longs":
            # longs pay up; price fails to rise (short divergence) or rises (confirmed)
            close[-96:] = 100.0 + (drift if price_confirms else -drift)
        else:
            close[-96:] = 100.0 - (drift if price_confirms else -drift)
        funding = np.full(bars, 0.0001)
        funding[-24:] = 0.003 if crowded == "longs" else -0.003
        oi = np.linspace(1.0e6, 1.2e6 if oi_growing else 0.8e6, bars)
        for i in range(bars):
            row: dict[str, Any] = {
                "timestamp": stamps[i],
                "symbol": symbol,
                "open": float(close[i]),
                "high": float(close[i]) + 0.3,
                "low": float(close[i]) - 0.3,
                "close": float(close[i]),
                "volume": 1.0,
            }
            if with_funding:
                row["funding"] = float(funding[i])
            if with_oi:
                row["open_interest"] = float(oi[i])
            rows.append(row)
    return rows


def _context(
    strategy: MixedFundingDivergenceStrategy, rows: list[dict[str, Any]]
) -> ExecutionContext:
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "15m"
    view = apply_precompute(strategy, CompletedBarsView.from_rows(rows))
    return ExecutionContext(
        view=view,
        ledger=PositionLedger(),
        state_snapshot=StateSnapshot(status="valid"),
        capacity=None,
        params={"initial_capital": 10_000.0},
        timestamp=pd.Timestamp(rows[-1]["timestamp"]).isoformat(),
        execution_spec=spec,
    )


def _strategy(**overrides: Any) -> MixedFundingDivergenceStrategy:
    return MixedFundingDivergenceStrategy(
        {
            "symbols": ["AAA", "BBB"],
            "funding_z_window_bars": WINDOW,
            "min_trade_notional": 0.0,
            **overrides,
        }
    )


def test_maker_rests_an_offer_beyond_the_close_when_crowded_longs_are_not_paid() -> (
    None
):
    strategy = _strategy(
        entry_order_type="maker", entry_offset_atr=0.5, entry_ttl_bars=8
    )
    ctx = _context(strategy, _rows(crowded="longs", price_confirms=False))
    intents = strategy.decide(ctx)
    assert {intent["symbol"] for intent in intents} == {"AAA", "BBB"}
    entry = intents[0]
    assert entry["action"] == "OPEN"
    assert entry["side"] == "sell"
    assert entry["time_in_force"] == "ALO"
    assert entry["expires_after_bars"] == 8
    close = float(ctx.view.symbol_frame("AAA").iloc[-1]["close"])
    assert entry["limit_price"] > close
    assert entry["notional"] == 500.0  # 5% of a 10,000 book
    assert entry["bracket"]["cooldown_seconds"] == 86_400
    assert entry["metadata"]["signal_funding_z"] > 2.0


def test_crowded_shorts_that_are_not_paid_are_bought_below_the_close() -> None:
    strategy = _strategy(entry_order_type="maker")
    ctx = _context(strategy, _rows(crowded="shorts", price_confirms=False))
    entry = strategy.decide(ctx)[0]
    close = float(ctx.view.symbol_frame(entry["symbol"]).iloc[-1]["close"])
    assert entry["side"] == "buy"
    assert entry["limit_price"] < close


def test_price_confirming_the_crowd_is_not_a_divergence() -> None:
    strategy = _strategy(entry_order_type="market")
    ctx = _context(strategy, _rows(crowded="longs", price_confirms=True))
    assert strategy.decide(ctx) == []


def test_taker_takes_the_next_open_without_a_limit() -> None:
    strategy = _strategy(entry_order_type="market")
    entry = strategy.decide(
        _context(strategy, _rows(crowded="longs", price_confirms=False))
    )[0]
    assert entry["action"] == "OPEN"
    assert entry["side"] == "sell"
    assert "limit_price" not in entry
    assert "time_in_force" not in entry


def test_open_interest_confirmation_requires_a_building_crowd() -> None:
    strategy = _strategy(entry_order_type="market", oi_confirmation="building")
    building = _context(
        strategy, _rows(crowded="longs", price_confirms=False, oi_growing=True)
    )
    unwinding = _context(
        strategy, _rows(crowded="longs", price_confirms=False, oi_growing=False)
    )
    assert len(strategy.decide(building)) == 2
    assert strategy.decide(unwinding) == []
    missing = _context(
        strategy, _rows(crowded="longs", price_confirms=False, with_oi=False)
    )
    assert strategy.decide(missing) == []
    joiner = _strategy(entry_order_type="market", oi_confirmation="unwinding")
    joins_the_unwind = _context(
        joiner, _rows(crowded="longs", price_confirms=False, oi_growing=False)
    )
    stands_aside = _context(
        joiner, _rows(crowded="longs", price_confirms=False, oi_growing=True)
    )
    assert len(joiner.decide(joins_the_unwind)) == 2
    assert joiner.decide(stands_aside) == []


def test_without_the_funding_feature_the_book_stands_down() -> None:
    strategy = _strategy(entry_order_type="maker")
    ctx = _context(
        strategy, _rows(crowded="longs", price_confirms=False, with_funding=False)
    )
    frame = ctx.view.symbol_frame("AAA")
    assert frame["starter_funding_z"].isna().all()
    assert strategy.decide(ctx) == []


def test_positions_close_after_the_hold_or_on_a_flipped_signal() -> None:
    strategy = _strategy(entry_order_type="market", max_hold_bars=96)
    ctx = _context(strategy, _rows(crowded="longs", price_confirms=False))
    # a long against the short signal is flipped out immediately
    ctx.ledger.positions["AAA"] = PositionRecord(
        symbol="AAA", side="long", size=5.0, avg_price=100.0
    )
    intents = strategy.decide(ctx)
    closes = [intent for intent in intents if intent["action"] == "CLOSE"]
    assert closes and closes[0]["symbol"] == "AAA" and closes[0]["side"] == "sell"
    assert closes[0]["metadata"]["exit_reason"] == "signal_flipped"
    # a short in the same direction rides until the hold expires
    ctx.ledger.positions["AAA"] = PositionRecord(
        symbol="AAA", side="short", size=5.0, avg_price=100.0, bars_held=95
    )
    intents = strategy.decide(ctx)
    closes = [intent for intent in intents if intent["action"] == "CLOSE"]
    assert closes[0]["metadata"]["exit_reason"] == "max_hold"


def test_rejects_unknown_execution_or_confirmation_modes() -> None:
    import pytest

    with pytest.raises(ValueError):
        MixedFundingDivergenceStrategy({"entry_order_type": "limit"})
    with pytest.raises(ValueError):
        MixedFundingDivergenceStrategy({"oi_confirmation": "maybe"})


# ------------------------------------------------------------ feeds ----
def _feed_job(tmp_path: Path, features: list[dict[str, Any]]) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "feed-demo",
        script=".wayfinder/jobs/feed-demo/workspace/src/strategy.py",
        interval_seconds=900,
    )
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "15m"
    spec.data_contract["features"] = features
    job.execution_spec = spec.to_dict()
    job.execution_params = {"symbols": ["BTC", "ETH"], "venue": "hyperliquid"}
    store.save(job)
    return store, job.id


def test_declared_feeds_refresh_funding_and_record_open_interest(tmp_path) -> None:
    store, job_id = _feed_job(
        tmp_path,
        [{"name": "funding", "max_age_seconds": 7200}, {"name": "open_interest"}],
    )
    calls: list[dict[str, Any]] = []

    def fake_funding(job_id_: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"job_id": job_id_, **kwargs})
        return {"appended": 6}

    def fake_open_interest(symbols: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "timestamp": "2026-09-05T10:00:00+00:00",
                "name": "open_interest",
                "value": 1.5e9,
                "symbol": s,
            }
            for s in symbols
        ]

    result = refresh_declared_feeds(
        job_id,
        store=store,
        fetch_funding=fake_funding,
        fetch_open_interest=fake_open_interest,
    )
    assert result == {"funding": {"appended": 6}, "open_interest": {"appended": 2}}
    assert (
        calls[0]["exchange"] == "hyperliquid"
        and calls[0]["quote"] == "USDC"
        and calls[0]["days"] == 3
    )
    rows = [
        json.loads(line)
        for line in (store.job_dir(job_id) / "state" / "features.jsonl")
        .read_text()
        .splitlines()
    ]
    assert {row["symbol"] for row in rows} == {"BTC", "ETH"}
    assert all(row["name"] == "open_interest" and row["written_at"] for row in rows)
    # the same hour is not recorded twice
    again = refresh_declared_feeds(
        job_id,
        store=store,
        fetch_funding=fake_funding,
        fetch_open_interest=fake_open_interest,
    )
    assert again["open_interest"] == {"appended": 0}


def test_declared_feeds_skip_undeclared_features_and_journal_failures(tmp_path) -> None:
    store, job_id = _feed_job(tmp_path, [{"name": "sentiment"}])
    assert refresh_declared_feeds(job_id, store=store) == {
        "funding": None,
        "open_interest": None,
    }

    store2, job2 = _feed_job(
        tmp_path / "two", [{"name": "funding"}, {"name": "open_interest"}]
    )

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("venue down")

    result = refresh_declared_feeds(
        job2, store=store2, fetch_funding=broken, fetch_open_interest=broken
    )
    assert result["funding"] == {"error": "venue down"}
    assert result["open_interest"] == {"error": "venue down"}
    journal = (store2.job_dir(job2) / "journal.jsonl").read_text()
    assert journal.count("feed_refresh_failed") == 2


def test_wake_refresh_runs_the_declared_feeds(tmp_path, monkeypatch) -> None:
    store, job_id = _feed_job(tmp_path, [{"name": "open_interest"}])
    seen: list[str] = []
    monkeypatch.setattr(
        "wayfinder_paths.jobs.derived_features.refresh_declared_feeds",
        lambda job_id_, *, store=None: (
            seen.append(job_id_) or {"funding": None, "open_interest": {"appended": 2}}
        ),
    )
    result = refresh_derived_features_if_stale(
        job_id,
        store=store,
        derive=lambda *args, **kwargs: {
            "newest_feature_ts": pd.Timestamp.now(tz="UTC").isoformat()
        },
        refresh_dataset=False,
    )
    assert seen == [job_id]
    assert result["feeds"] == {"funding": None, "open_interest": {"appended": 2}}
