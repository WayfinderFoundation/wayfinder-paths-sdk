"""Workspace signal sweep: agent-composed defs join the canonical scan under
one pooled BH family, feature columns survive timeframe resampling, the
causality gate rejects lookahead compositions, and holdout confirms workspace
candidates with sha provenance."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from wayfinder_paths.jobs.research import (
    holdout_check_job,
    resample_ohlcv,
    scan_signals,
    signal_scan_job,
)
from wayfinder_paths.jobs.signal_library import (
    SIGNAL_LIBRARY,
    SignalDef,
    build_signal_frame,
)
from wayfinder_paths.jobs.workspace_signals import (
    WORKSPACE_SIGNAL_CAP,
    load_workspace_signals,
    validate_workspace_signals,
)


def _bars(closes: list[float], extra: dict[str, list] | None = None) -> pd.DataFrame:
    n = len(closes)
    opens = [closes[0], *closes[:-1]]
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC"),
            "symbol": "IMX",
            "open": opens,
            "high": [max(o, c) * 1.002 for o, c in zip(opens, closes, strict=False)],
            "low": [min(o, c) * 0.998 for o, c in zip(opens, closes, strict=False)],
            "close": closes,
            "volume": [100.0] * n,
        }
    )
    for name, values in (extra or {}).items():
        frame[name] = values
    return frame


def _wavy_closes(n: int) -> list[float]:
    return [
        100.0 + 8.0 * np.sin(i / 7.0) + 3.0 * np.sin(i / 23.0) - 0.01 * i
        for i in range(n)
    ]


class TestResampleFeatureCarry:
    def test_last_value_aggregation_and_dtype(self):
        # Feature columns arrive from merge_features as object dtype with
        # Nones; resampling must coerce to float and take the value as-of
        # the bucket close.
        funding: list = [None, None, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
        frame = _bars([100.0 + i for i in range(8)], {"funding": funding})
        frame["funding"] = frame["funding"].astype(object)
        out = resample_ohlcv(frame, 4 * 3600, bar_seconds=3600)
        assert out["funding"].dtype == float
        # Bucket labeled 04:00 holds bars 1-4 -> last funding is 0.3.
        assert out["funding"].iloc[1] == pytest.approx(0.3)

    def test_identity_path_coerces_dtype(self):
        frame = _bars([100.0, 101.0, 102.0], {"funding": [None, 0.1, 0.2]})
        frame["funding"] = frame["funding"].astype(object)
        out = resample_ohlcv(frame, 3600, bar_seconds=3600)
        assert out["funding"].dtype == float
        assert np.isnan(out["funding"].iloc[0])

    def test_prefix_property_with_extras(self):
        closes = _wavy_closes(400)
        funding = [0.01 * np.sin(i / 5.0) for i in range(400)]
        full = resample_ohlcv(
            _bars(closes, {"funding": funding}), 4 * 3600, bar_seconds=3600
        )
        prefix = resample_ohlcv(
            _bars(closes[:300], {"funding": funding[:300]}),
            4 * 3600,
            bar_seconds=3600,
        )
        pd.testing.assert_frame_equal(full.iloc[: len(prefix)], prefix)


def _plain_def() -> SignalDef:
    return SignalDef(
        "ws_new_high_3",
        "workspace",
        "close above the prior 3 closes' maximum",
        5,
        lambda f: f["close"] > f["close"].shift(1).rolling(3).max(),
    )


class TestValidateWorkspaceSignals:
    def _probe(self) -> pd.DataFrame:
        return _bars(_wavy_closes(300))

    def test_accepts_causal_boolean_defs(self):
        validate_workspace_signals([_plain_def()], self._probe())

    def test_rejects_over_cap(self):
        defs = [
            SignalDef(f"ws_sig_{i}", "workspace", "d", 3, lambda f: f["close"] > 0)
            for i in range(WORKSPACE_SIGNAL_CAP + 1)
        ]
        with pytest.raises(ValueError, match="cap"):
            validate_workspace_signals(defs, self._probe())

    def test_rejects_canonical_collision(self):
        clash = SignalDef("new_low_5", "workspace", "d", 3, lambda f: f["close"] > 0)
        with pytest.raises(ValueError, match="collides"):
            validate_workspace_signals([clash], self._probe())

    def test_rejects_non_causal(self):
        peek = SignalDef(
            "ws_peek",
            "workspace",
            "tomorrow's close is higher (lookahead)",
            3,
            lambda f: f["close"].shift(-1) > f["close"],
        )
        with pytest.raises(ValueError, match="non-causal"):
            validate_workspace_signals([peek], self._probe())

    def test_rejects_float_output(self):
        floaty = SignalDef(
            "ws_floaty",
            "workspace",
            "returns a float column",
            3,
            lambda f: f["close"] - f["close"].shift(1),
        )
        with pytest.raises(ValueError, match="boolean"):
            validate_workspace_signals([floaty], self._probe())


class TestBuildSignalFrameExtras:
    def test_extra_columns_materialize_after_library(self):
        frame = _bars(_wavy_closes(300))
        signals = build_signal_frame(frame, [_plain_def()])
        assert "ws_new_high_3" in signals.columns
        assert len(signals.columns) == len(SIGNAL_LIBRARY) + 1
        assert signals["ws_new_high_3"].dtype == bool

    def test_scan_counts_and_tags_workspace_rows(self):
        result = scan_signals(
            _bars(_wavy_closes(700)),
            horizons=[4],
            holdout_fraction=0.0,
            extra_signals=[_plain_def()],
        )
        assert result["signals_tested"] == len(SIGNAL_LIBRARY) + 1
        assert result["workspace_signals"] == ["ws_new_high_3"]
        libraries = {row["signal"]: row["library"] for row in result["_all_rows"]}
        assert libraries["ws_new_high_3"] == "workspace"
        assert libraries["new_low_5"] == "canonical"


_WORKSPACE_MODULE = """
from wayfinder_paths.jobs.signal_library import SignalDef

WORKSPACE_SIGNALS = (
    SignalDef(
        "ws_funding_neg_high_3",
        "workspace",
        "fresh 3-bar high while funding is negative",
        5,
        lambda f: (f["close"] > f["close"].shift(1).rolling(3).max())
        & (f["funding"] < 0),
    ),
    SignalDef(
        "ws_new_high_3",
        "workspace",
        "close above the prior 3 closes' maximum",
        5,
        lambda f: f["close"] > f["close"].shift(1).rolling(3).max(),
    ),
)
"""


def _scan_job_store(tmp_path, closes, *, with_workspace=True, with_funding=True):
    from wayfinder_paths.jobs.store import JobStore

    store = JobStore(repo_root=tmp_path)
    root = store.job_dir("scan-job")
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    (root / "job.yaml").write_text("id: scan-job\n")
    features = [{"name": "funding"}] if with_funding else []
    (root / "execution_spec.json").write_text(
        json.dumps(
            {
                "market_kind": "perp",
                "data_contract": {
                    "bar_interval": "1h",
                    "symbols": ["IMX"],
                    "features": features,
                },
            }
        )
    )
    bars = _bars(closes)
    rows = bars.assign(timestamp=bars["timestamp"].astype(str)).to_dict("records")
    (root / "results" / "backtest" / "input_bars.json").write_text(
        json.dumps({"bars": rows})
    )
    if with_funding:
        state = root / "state"
        state.mkdir(parents=True, exist_ok=True)
        feature_rows = [
            {
                "timestamp": str(ts),
                "name": "funding",
                "value": -0.01 if i % 2 == 0 else 0.01,
                "symbol": "IMX",
            }
            for i, ts in enumerate(bars["timestamp"])
        ]
        (state / "features.jsonl").write_text(
            "\n".join(json.dumps(row) for row in feature_rows) + "\n"
        )
    if with_workspace:
        src = root / "workspace" / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "signals.py").write_text(_WORKSPACE_MODULE)
    return store


class TestWorkspaceScanEndToEnd:
    def _closes(self, n=1500):
        rng = np.random.default_rng(7)
        return list(100.0 * np.exp(np.cumsum(rng.normal(0, 0.004, n))))

    def test_sweep_tags_and_ledger_provenance(self, tmp_path):
        store = _scan_job_store(tmp_path, self._closes())
        result = signal_scan_job("scan-job", store=store)
        assert result["workspace_signals"] == [
            "ws_funding_neg_high_3",
            "ws_new_high_3",
        ]
        scan = result["per_symbol"]["IMX"]
        assert scan["signals_tested"] == len(SIGNAL_LIBRARY) + 2
        ws_rows = [
            row
            for row in scan["candidates"] + scan["promoted"]
            if row["signal"].startswith("ws_")
        ]
        for row in ws_rows:
            assert row["library"] == "workspace"
            assert "q_value" in row
        ledger_path = (
            store.job_dir("scan-job")
            / "results"
            / "research"
            / "signal_scan"
            / "ledger.jsonl"
        )
        rows = [json.loads(line) for line in ledger_path.read_text().splitlines()]
        meta = [row for row in rows if row.get("kind") == "scan_meta"]
        assert meta[-1]["workspace_signals"] == [
            "ws_funding_neg_high_3",
            "ws_new_high_3",
        ]
        assert meta[-1]["workspace_signals_sha"]
        tests = [row for row in rows if row.get("kind") == "scan_test"]
        assert any(row.get("library") == "workspace" for row in tests)
        assert any(row.get("library") == "canonical" for row in tests)

    def test_no_workspace_flag_excludes(self, tmp_path):
        store = _scan_job_store(tmp_path, self._closes())
        result = signal_scan_job("scan-job", store=store, include_workspace=False)
        assert result["workspace_signals"] == []
        scan = result["per_symbol"]["IMX"]
        assert scan["signals_tested"] == len(SIGNAL_LIBRARY)

    def test_missing_workspace_file_scans_canonical_only(self, tmp_path):
        store = _scan_job_store(tmp_path, self._closes(), with_workspace=False)
        result = signal_scan_job("scan-job", store=store)
        assert result["workspace_signals"] == []

    def test_holdout_confirms_workspace_signal_and_spends_once(self, tmp_path):
        store = _scan_job_store(tmp_path, self._closes())
        signal_scan_job("scan-job", store=store)
        spent = holdout_check_job(
            "scan-job",
            signal="ws_new_high_3",
            horizon=4,
            direction="long",
            store=store,
        )
        assert spent["already_spent"] is False
        assert "workspace_signals_changed_since_scan" not in spent
        respent = holdout_check_job(
            "scan-job",
            signal="ws_new_high_3",
            horizon=4,
            direction="long",
            store=store,
        )
        assert respent["already_spent"] is True

    def test_holdout_flags_edited_workspace_code(self, tmp_path):
        store = _scan_job_store(tmp_path, self._closes())
        signal_scan_job("scan-job", store=store)
        signals_path = store.job_dir("scan-job") / "workspace" / "src" / "signals.py"
        signals_path.write_text(_WORKSPACE_MODULE + "\n# edited after scan\n")
        report = holdout_check_job(
            "scan-job",
            signal="ws_new_high_3",
            horizon=4,
            direction="long",
            store=store,
        )
        assert report["workspace_signals_changed_since_scan"] is True
        assert "never tested" in report["workspace_warning"]


class TestLoader:
    def test_missing_file_returns_none(self, tmp_path):
        assert load_workspace_signals(tmp_path) is None

    def test_present_without_attr_raises(self, tmp_path):
        src = tmp_path / "workspace" / "src"
        src.mkdir(parents=True)
        (src / "signals.py").write_text("x = 1\n")
        with pytest.raises(ValueError, match="WORKSPACE_SIGNALS"):
            load_workspace_signals(tmp_path)
