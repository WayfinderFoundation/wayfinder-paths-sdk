"""symbol_frame()'s positional fast path must be indistinguishable from the
boolean-mask implementation it replaced — across root views, windowed children,
prefix slices (through), nested slices, and merged feature columns — and the
manual to_dict()s must match asdict() field-for-field."""

from __future__ import annotations

from dataclasses import asdict

import pandas as pd
import pytest

from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    FillEvent,
    OrderIntent,
    PositionRecord,
)


def _rows(n_ts: int, symbols: list[str], *, drop_every: int = 0) -> list[dict]:
    """Multi-symbol rows; drop_every>0 removes some symbols on some timestamps
    so per-symbol row counts differ (the realistic ragged case)."""
    rows = []
    ts = pd.date_range("2024-01-01", periods=n_ts, freq="h", tz="UTC")
    for i in range(n_ts):
        for j, sym in enumerate(symbols):
            if drop_every and (i + j) % drop_every == 0:
                continue
            price = 100.0 + i * 0.1 + j
            rows.append(
                {
                    "timestamp": ts[i].isoformat(),
                    "symbol": sym,
                    "open": price,
                    "high": price + 1,
                    "low": price - 1,
                    "close": price + 0.5,
                    "volume": 10.0,
                }
            )
    return rows


def _mask_frame(view: CompletedBarsView, symbol: str) -> pd.DataFrame:
    return view._bars[view._bars["symbol"] == symbol]


SYMBOLS = ["BTC", "ETH", "SOL", "XRP"]


def test_symbol_frame_matches_mask_on_root_and_children() -> None:
    view = CompletedBarsView.from_rows(_rows(50, SYMBOLS, drop_every=7))
    candidates = [view, view.through(30), view.window(49, 12), view.window(3, 9)]
    # Nested slice: a child of a child (offset composition).
    candidates.append(view.through(40).window(35, 10))
    for v in candidates:
        for sym in [*SYMBOLS, "MISSING"]:
            fast = v.symbol_frame(sym)
            slow = _mask_frame(v, sym)
            pd.testing.assert_frame_equal(fast, slow)


def test_symbol_frame_shares_root_index_across_ticks() -> None:
    view = CompletedBarsView.from_rows(_rows(30, SYMBOLS))
    view.window(10, 5)  # builds the root map
    root_map = view._symbol_positions
    assert root_map is not None
    child = view.window(20, 5)
    assert child._symbol_positions is root_map  # shared, not rebuilt


def test_latest_and_feature_ride_the_fast_path() -> None:
    view = CompletedBarsView.from_rows(_rows(20, SYMBOLS))
    child = view.window(19, 6)
    last = child.latest("ETH")
    assert last["symbol"] == "ETH"
    expected = _mask_frame(child, "ETH").iloc[-1].to_dict()
    assert last == expected


def test_bar_index_property() -> None:
    from wayfinder_paths.jobs.execution.primitives import (
        ExecutionContext,
        ExecutionSpec,
        PositionLedger,
        StateSnapshot,
    )

    view = CompletedBarsView.from_rows(_rows(25, SYMBOLS)).window(24, 10)
    ctx = ExecutionContext(
        view=view,
        ledger=PositionLedger(),
        state_snapshot=StateSnapshot(status="valid"),
        capacity=None,
        params={},
        timestamp="2024-01-02T00:00:00+00:00",
        execution_spec=ExecutionSpec(),
    )
    assert ctx.bar_index == 10
    assert ctx.bar_index == len(view.timestamps)


@pytest.mark.parametrize(
    "obj",
    [
        OrderIntent(
            action="open",
            venue="hyperliquid",
            symbol="ETH",
            side="long",
            notional=100.0,
            bracket={"stop_loss": 1.0, "policy": "conservative"},
            metadata={"entry": 2.0},
        ),
        FillEvent(
            status="filled",
            venue="hyperliquid",
            symbol="ETH",
            side="buy",
            filled_size=1.5,
            avg_price=100.0,
            fee=0.1,
            reduce_only=True,
            raw={"metadata": {"exit_reason": "tp"}},
            timestamp="2024-01-01T00:00:00+00:00",
        ),
        PositionRecord(
            symbol="ETH",
            side="long",
            size=2.0,
            avg_price=99.0,
            bars_held=3,
            metadata={"k": "v"},
        ),
    ],
)
def test_manual_to_dict_matches_asdict(obj) -> None:
    manual = obj.to_dict()
    reference = asdict(obj)
    assert manual == reference
    # Top-level containers are copies, not aliases of the dataclass fields.
    for key, value in manual.items():
        if isinstance(value, dict):
            assert value == reference[key]
            attr = getattr(obj, key, None)
            if isinstance(attr, dict):
                assert value is not attr
