"""Signal library + scan: causality (prefix property), planted-edge
detection with direction, multiple-testing honesty, and the reference
strategy catalog."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from wayfinder_paths.jobs.research import scan_signals
from wayfinder_paths.jobs.signal_library import (
    SIGNAL_LIBRARY,
    build_signal_frame,
    signal_defs,
)
from wayfinder_paths.jobs.strategies import library_catalog


def _bars(closes: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    # Contiguous bars: each bar's range envelopes the move from the prior
    # close, so a crash bar has a genuinely wide range (ATR triggers can fire).
    n = len(closes)
    volume = volumes if volumes is not None else [100.0] * n
    opens = [closes[0], *closes[:-1]]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC"),
            "symbol": "IMX",
            "open": opens,
            "high": [max(o, c) * 1.002 for o, c in zip(opens, closes, strict=False)],
            "low": [min(o, c) * 0.998 for o, c in zip(opens, closes, strict=False)],
            "close": closes,
            "volume": volume,
        }
    )


def _wavy_closes(n: int) -> list[float]:
    # Deterministic drifting sine — exercises every trigger family without RNG.
    return [
        100.0 + 8.0 * np.sin(i / 7.0) + 3.0 * np.sin(i / 23.0) - 0.01 * i
        for i in range(n)
    ]


class TestSignalLibrary:
    def test_all_columns_boolean_and_aligned(self):
        frame = _bars(_wavy_closes(400))
        signals = build_signal_frame(frame)
        assert list(signals.index) == list(frame.index)
        assert set(signals.columns) == {spec.name for spec in SIGNAL_LIBRARY}
        for name in signals.columns:
            assert signals[name].dtype == bool, name

    def test_causal_prefix_property(self):
        # Appending future bars must never change past signal values — the
        # lookahead check. Compare the full frame vs a truncated prefix.
        closes = _wavy_closes(400)
        full = build_signal_frame(_bars(closes))
        prefix = build_signal_frame(_bars(closes[:300]))
        pd.testing.assert_frame_equal(full.iloc[:300], prefix)

    def test_every_signal_fires_somewhere(self):
        # A trigger that never fires on a rich fixture is a broken builder.
        n = 600
        closes = _wavy_closes(n)
        # Splice in a crash and a melt-up so extreme/exhaustion triggers fire.
        for i in range(200, 260):
            closes[i] -= (i - 200) * 0.8
        for i in range(400, 460):
            closes[i] += (i - 400) * 0.8
        volumes = [100.0 + (500.0 if i % 37 == 0 else 0.0) for i in range(n)]
        signals = build_signal_frame(_bars(closes, volumes))
        silent = [name for name in signals.columns if not signals[name].any()]
        assert silent == [], f"never-firing signals: {silent}"

    def test_short_frame_returns_all_false(self):
        signals = build_signal_frame(_bars(_wavy_closes(5)))
        assert not signals.to_numpy().any()

    def test_directional_pairs_are_distinct_events(self):
        frame = _bars(_wavy_closes(400))
        signals = build_signal_frame(frame)
        assert not (signals["new_low_5"] & signals["new_high_5"]).any()
        defs = signal_defs()
        assert defs["new_low_5"].family == "breakout"


class TestScanSignals:
    def test_planted_short_edge_detected_with_direction(self):
        # After every 5-bar-low break, force a further deterministic drop —
        # the scan must surface new_low_5 as a SHORT candidate.
        rng = np.random.default_rng(7)
        closes = [100.0]
        for _ in range(1200):
            closes.append(closes[-1] * (1 + rng.normal(0, 0.002)))
        series = pd.Series(closes)
        breaks = (series < series.shift(1).rolling(5).min()).fillna(False)
        adjusted = list(series)
        for i in np.flatnonzero(breaks.to_numpy()):
            for j in range(i + 1, min(i + 5, len(adjusted))):
                adjusted[j] *= 0.99
        result = scan_signals(_bars(adjusted), horizons=[4])
        short_hits = {
            row["signal"] for row in result["candidates"] if row["direction"] == "short"
        }
        # The forced follow-through creates overlapping late breaks that
        # dilute the raw trigger; the first-trigger variant captures the
        # planted event cleanly — either surfacing proves detection.
        assert {"new_low_5", "fresh_new_low_5"} & short_hits

    def test_random_walk_reports_no_stable_candidates(self):
        # A driftless seeded random walk carries no information; lucky
        # single-half passes may occur across ~50 tests, but none should
        # survive the half-split stability requirement.
        rng = np.random.default_rng(42)
        closes = list(100.0 * np.exp(np.cumsum(rng.normal(0, 0.003, 900))))
        result = scan_signals(_bars(closes), horizons=[4, 24])
        stable = [row for row in result["candidates"] if row["stable_across_halves"]]
        assert stable == []

    def test_multiple_testing_fields_present(self):
        result = scan_signals(_bars(_wavy_closes(700)), horizons=[4, 24])
        assert result["tests_run"] > 0
        assert result["expected_lucky_passes"] == pytest.approx(
            result["tests_run"] * 0.05, abs=0.06
        )
        assert result["signals_tested"] == len(SIGNAL_LIBRARY)
        assert len(result["top_by_abs_t"]) <= 5


class TestStrategyCatalog:
    def test_catalog_lists_ported_live_strategies(self):
        catalog = {entry["name"]: entry for entry in library_catalog()}
        assert {"imx_momentum", "imx_atr_target", "snx_momentum"} <= set(catalog)
        imx = catalog["imx_momentum"]
        assert imx["module"] == "wayfinder_paths.jobs.strategies.imx_momentum"
        assert "import build_strategy" in imx["workspace_reexport"]
        assert imx["default_params"]["symbol"] == "IMX"

    def test_helper_modules_excluded(self):
        names = {entry["name"] for entry in library_catalog()}
        assert not {"indicators", "models", "portfolio"} & names
