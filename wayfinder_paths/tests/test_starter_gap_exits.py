"""An unrelated missing candle must not veto an independent leg's exit."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd
import pytest

from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    ExecutionContext,
    ExecutionSpec,
    PositionLedger,
    PositionRecord,
    StateSnapshot,
)
from wayfinder_paths.jobs.strategies._starter_utils import current_rows
from wayfinder_paths.jobs.strategies.mixed_bollinger_pullback import (
    MixedBollingerPullbackStrategy,
)
from wayfinder_paths.jobs.strategies.mixed_funding_divergence import (
    MixedFundingDivergenceStrategy,
)
from wayfinder_paths.jobs.strategies.mixed_liquidation_flush import (
    MixedLiquidationFlushStrategy,
)
from wayfinder_paths.jobs.strategies.mixed_rsi_snapback import MixedRsiSnapbackStrategy
from wayfinder_paths.jobs.strategies.mixed_volume_capitulation import (
    MixedVolumeCapitulationStrategy,
)

CASES = [
    (MixedRsiSnapbackStrategy, "market"),
    (MixedBollingerPullbackStrategy, "market"),
    (MixedVolumeCapitulationStrategy, "market"),
    (MixedVolumeCapitulationStrategy, "maker"),
    (MixedFundingDivergenceStrategy, "market"),
    (MixedFundingDivergenceStrategy, "maker"),
    (MixedLiquidationFlushStrategy, "market"),
    (MixedLiquidationFlushStrategy, "maker"),
]


def context(*, missing: str = "absent", held: bool = True) -> ExecutionContext:
    now = pd.Timestamp("2026-01-01T12:00Z")
    rows: list[Mapping[str, Any]] = []
    for symbol in ("HYPE", "ETH", "BTC"):
        if symbol == "BTC" and missing == "absent":
            continue
        row = {
            "timestamp": now - pd.Timedelta(minutes=15)
            if symbol == "BTC" and missing == "stale"
            else now,
            "symbol": symbol,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 200.0,
            "starter_rsi": 10.0,
            "starter_capitulation_rsi": 10.0,
            "starter_trend_sma": 90.0,
            "starter_volume_median": 100.0,
            "starter_pullback_zscore": -3.0,
            "starter_stop_atr": 2.0,
            "starter_signal": 1.0,
            "starter_signal_age": 0.0,
            "starter_funding_z": -3.0,
            "starter_confirm_return": 0.0,
            "starter_flush_return": -0.10,
            "starter_oi_change": -0.20,
        }
        if symbol == "HYPE" and held:
            row.update(
                starter_rsi=80.0,
                starter_capitulation_rsi=80.0,
                starter_pullback_zscore=1.0,
                starter_signal=0.0,
                starter_signal_age=1000.0,
            )
        rows.append(row)
    ledger = PositionLedger()
    if held:
        ledger.positions["HYPE"] = PositionRecord("HYPE", "long", 25, 100, 1000)
        ledger.positions["BTC"] = PositionRecord("BTC", "long", 25, 100, 1000)
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "15m"
    return ExecutionContext(
        view=CompletedBarsView.from_rows(rows),
        ledger=ledger,
        state_snapshot=StateSnapshot(status="valid"),
        capacity=None,
        params={"initial_capital": 10000},
        timestamp=now.isoformat(),
        execution_spec=spec,
    )


def strategy(cls: type[Any], entry: str) -> Any:
    result = cls({"symbols": ["HYPE", "ETH", "BTC"], "entry_order_type": entry})
    result.warmup_bars = 1
    return result


@pytest.mark.parametrize("cls,entry", CASES)
@pytest.mark.parametrize("missing", ["absent", "stale"])
def test_healthy_exit_survives_unrelated_missing_bar(
    cls: type[Any], entry: str, missing: str
) -> None:
    intents = strategy(cls, entry).decide(context(missing=missing))
    assert len(intents) == 1
    assert intents[0]["symbol"] == "HYPE"
    assert intents[0]["action"] == "CLOSE"
    assert intents[0]["reduce_only"] is True


@pytest.mark.parametrize("cls,entry", CASES)
def test_missing_peer_still_pauses_new_entries(cls: type[Any], entry: str) -> None:
    assert strategy(cls, entry).decide(context(held=False)) == []


@pytest.mark.parametrize("cls,entry", CASES)
def test_complete_panel_still_enters_and_exits(cls: type[Any], entry: str) -> None:
    intents = strategy(cls, entry).decide(context(missing="none"))
    assert any(i["symbol"] == "HYPE" and i["action"] == "CLOSE" for i in intents)
    assert any(i["symbol"] == "ETH" and i["action"] == "OPEN" for i in intents)


def test_default_row_helper_retains_all_or_nothing_basket_semantics() -> None:
    assert current_rows(context(), ["HYPE", "BTC"]) is None


@pytest.mark.parametrize("missing", ["absent", "stale"])
def test_partial_row_helper_excludes_unavailable_peers(missing: str) -> None:
    rows = current_rows(context(missing=missing), ["HYPE", "BTC"], require_all=False)
    assert rows is not None
    assert list(rows) == ["HYPE"]


def test_partial_row_helper_does_not_fabricate_required_features() -> None:
    ctx = context(missing="none")
    assert current_rows(ctx, ["HYPE", "BTC"], required_columns=("unknown",)) is None
    assert (
        current_rows(
            ctx, ["HYPE", "BTC"], required_columns=("unknown",), require_all=False
        )
        == {}
    )


@pytest.mark.parametrize("cls,entry", CASES)
@pytest.mark.parametrize("size", [1.0, 80.0])
def test_gap_does_not_rebalance_or_close_a_leg_without_its_exit(
    cls: type[Any], entry: str, size: float
) -> None:
    ctx = context()
    rows = ctx.view.to_frame()
    # Keep the healthy held leg's signal active and far from its time limit.
    mask = rows.symbol == "HYPE"
    rows.loc[mask, "starter_rsi"] = 10.0
    rows.loc[mask, "starter_capitulation_rsi"] = 10.0
    rows.loc[mask, "starter_pullback_zscore"] = -3.0
    rows.loc[mask, "starter_signal"] = 1.0
    rows.loc[mask, "starter_signal_age"] = 0.0
    ctx.view = CompletedBarsView(rows)
    ctx.ledger.positions["HYPE"] = PositionRecord("HYPE", "long", size, 100, 1)
    # Both growing a small holding and trimming a large one must remain paused.
    assert strategy(cls, entry).decide(ctx) == []


@pytest.mark.parametrize("cls,entry", CASES)
def test_gap_preserves_time_exit_without_recovery_signal(
    cls: type[Any], entry: str
) -> None:
    ctx = context()
    rows = ctx.view.to_frame()
    mask = rows.symbol == "HYPE"
    rows.loc[mask, "starter_rsi"] = 10.0
    rows.loc[mask, "starter_capitulation_rsi"] = 10.0
    rows.loc[mask, "starter_pullback_zscore"] = -3.0
    ctx.view = CompletedBarsView(rows)
    intents = strategy(cls, entry).decide(ctx)
    assert len(intents) == 1
    assert intents[0]["symbol"] == "HYPE"
    assert intents[0]["reduce_only"] is True


@pytest.mark.parametrize(
    "cls,entry",
    [
        (MixedBollingerPullbackStrategy, "market"),
        (MixedFundingDivergenceStrategy, "market"),
        (MixedFundingDivergenceStrategy, "maker"),
        (MixedLiquidationFlushStrategy, "market"),
        (MixedLiquidationFlushStrategy, "maker"),
    ],
)
def test_short_exit_remains_buy_to_close(cls: type[Any], entry: str) -> None:
    ctx = context()
    ctx.ledger.positions["HYPE"].side = "short"
    intents = strategy(cls, entry).decide(ctx)
    assert len(intents) == 1
    assert intents[0]["side"] == "buy"
    assert intents[0]["size"] == 25
    assert intents[0]["reduce_only"] is True


@pytest.mark.parametrize(
    "cls,entry",
    [
        case
        for case in CASES
        if case[0] in {MixedFundingDivergenceStrategy, MixedLiquidationFlushStrategy}
    ],
)
@pytest.mark.parametrize("side,signal", [("long", -1), ("short", 1)])
def test_gap_keeps_signal_flip_exit_without_reopening(
    cls: type[Any], entry: str, side: str, signal: int
) -> None:
    ctx = context()
    rows = ctx.view.to_frame()
    rows.loc[rows.symbol == "HYPE", "starter_signal"] = signal
    ctx.view = CompletedBarsView(rows)
    ctx.ledger.positions["HYPE"] = PositionRecord("HYPE", side, 25, 100, 1)
    intents = strategy(cls, entry).decide(ctx)
    assert len(intents) == 1
    assert intents[0]["symbol"] == "HYPE"
    assert intents[0]["action"] == "CLOSE"
    assert intents[0]["metadata"]["exit_reason"] == "signal_flipped"
