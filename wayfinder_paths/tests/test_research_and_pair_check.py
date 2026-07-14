"""The pre-trade research toolkit: signal event-studies and the pair admission
gate. These are the tools that stop the strategy agent from building six
parameter variants of a signal with no predictive power, or pair-trading two
correlated-but-not-cointegrated majors (the live ETH/SOL failure)."""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from wayfinder_paths.jobs.research import (
    cost_hurdle,
    engle_granger_both,
    event_study,
    hurst_exponent,
    mean_crossings,
    ou_half_life,
    pair_admission_gate,
    pair_check_job,
)

RNG = np.random.default_rng(7)
HOUR = 3600


def _ou_series(n: int, phi: float = 0.94, sigma: float = 1.0) -> np.ndarray:
    x = np.zeros(n)
    noise = RNG.normal(0, sigma, n)
    for i in range(1, n):
        x[i] = phi * x[i - 1] + noise[i]
    return x


def _cointegrated_pair(
    n: int = 3000, hedge: float = 0.8
) -> tuple[np.ndarray, np.ndarray]:
    """x = random walk; log(y) = hedge * log(x) + OU noise -> textbook pair.
    phi=0.97 -> half-life ~23 bars (~23h on hourly bars): inside the gate's
    tradeable band."""
    log_x = np.cumsum(RNG.normal(0, 0.01, n)) + 5.0
    spread = _ou_series(n, phi=0.97, sigma=0.004)
    log_y = hedge * log_x + spread + 1.0
    return np.exp(log_y), np.exp(log_x)


def _correlated_not_cointegrated(n: int = 3000) -> tuple[np.ndarray, np.ndarray]:
    """The ETH/SOL case: a shared market factor (high return correlation) plus
    INDEPENDENT random-walk idiosyncratic components (levels drift apart)."""
    market = RNG.normal(0, 0.01, n)
    a = np.exp(np.cumsum(market + RNG.normal(0.0002, 0.006, n)) + 5.0)
    b = np.exp(np.cumsum(market + RNG.normal(-0.0002, 0.008, n)) + 3.0)
    return a, b


def test_ou_half_life_recovers_known_ar1() -> None:
    phi = 0.94
    series = _ou_series(20_000, phi=phi)
    expected = math.log(2) / -math.log(phi)
    got = ou_half_life(series)
    assert abs(got - expected) / expected < 0.25


def test_engle_granger_passes_cointegrated_pair() -> None:
    y, x = _cointegrated_pair()
    result = engle_granger_both(y, x)
    assert result["pass"], result
    assert abs(result["ab"]["hedge_ratio"] - 0.8) < 0.1


def test_engle_granger_rejects_independent_walks() -> None:
    a = np.exp(np.cumsum(RNG.normal(0, 0.01, 3000)) + 4.0)
    b = np.exp(np.cumsum(RNG.normal(0, 0.01, 3000)) + 4.0)
    result = engle_granger_both(a, b)
    assert not result["pass"], result


def test_hurst_low_for_ou_high_for_trend() -> None:
    ou = _ou_series(5000, phi=0.9)
    trend = np.cumsum(RNG.normal(0.05, 1.0, 5000))
    assert hurst_exponent(ou) < 0.5
    assert hurst_exponent(trend) > 0.5


def test_mean_crossings_exact_on_sine() -> None:
    x = np.sin(np.linspace(0, 10 * np.pi, 1000))  # 5 full periods
    assert 9 <= mean_crossings(x) <= 11  # endpoint fencepost tolerance


def test_cost_hurdle_arithmetic() -> None:
    # capture (2.0 - 0.5) * 0.01 = 1.5%; cost 2 legs * 2 sides * 8.5bp = 34bp
    result = cost_hurdle(0.01, z_entry=2.0, z_exit=0.5, fee_bps=5.0, slippage_bps=3.5)
    assert abs(result["expected_capture"] - 0.015) < 1e-9
    assert abs(result["round_trip_cost"] - 0.0034) < 1e-9
    assert result["pass"] is True  # 4.4x >= 3x
    tight = cost_hurdle(0.001, z_entry=2.0, z_exit=0.5, fee_bps=5.0, slippage_bps=3.5)
    assert tight["pass"] is False


def _prices_frame(a: np.ndarray, b: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame({"AAA": a, "BBB": b})


def test_gate_rejects_correlated_but_not_cointegrated() -> None:
    """THE load-bearing test — the live ETH/SOL failure, codified: high return
    correlation, drifting levels. The gate must REJECT before anyone tunes a
    single parameter."""
    a, b = _correlated_not_cointegrated()
    corr = np.corrcoef(np.diff(np.log(a)), np.diff(np.log(b)))[0, 1]
    assert corr > 0.5  # genuinely correlated...
    report = pair_admission_gate(_prices_frame(a, b), bar_seconds=HOUR)
    assert report["verdict"] == "REJECT"  # ...but not tradeable
    failed = {c["name"] for c in report["checks"] if c["hard"] and not c["pass"]}
    assert "engle_granger_both_directions" in failed or "rolling_stability" in failed
    assert "REJECT" in report["recommendation"]


def test_gate_passes_synthetic_cointegrated_pair() -> None:
    y, x = _cointegrated_pair(n=9000)  # ~ 375 days of hourly bars
    report = pair_admission_gate(
        _prices_frame(y, x),
        bar_seconds=HOUR,
        fee_bps=0.5,
        slippage_bps=0.25,  # cheap costs so the hurdle reflects the synthetic sigma
    )
    assert report["verdict"] in ("PASS", "MARGINAL"), report["checks"]
    hard_fails = [c for c in report["checks"] if c["hard"] and not c["pass"]]
    assert not hard_fails, hard_fails
    assert report["suggested"]["hedge_ratio"] == pytest.approx(0.8, abs=0.1)
    assert report["suggested"]["lookback_bars"] is not None
    assert report["suggested"]["time_stop_bars"] is not None


def test_gate_half_life_band_unit_conversion() -> None:
    """Half-life band is specified in HOURS; a 4h-bar series must convert via
    bar_seconds (the classic off-by-4x)."""
    y, x = _cointegrated_pair(n=4000)
    hourly = pair_admission_gate(_prices_frame(y, x), bar_seconds=HOUR)
    four_hourly = pair_admission_gate(_prices_frame(y, x), bar_seconds=4 * HOUR)
    hl_1h = next(c for c in hourly["checks"] if c["name"] == "half_life")["value"]
    hl_4h = next(c for c in four_hourly["checks"] if c["name"] == "half_life")["value"]
    assert hl_1h is not None and hl_4h is not None
    assert hl_4h == pytest.approx(hl_1h * 4, rel=0.01)


def _planted_world(sign: float, seed: int = 11) -> pd.DataFrame:
    """Deterministically spaced events (gap 48 > h=24, so decimation is a
    no-op) followed by a strong 24-bar drift of the given sign."""
    n = 6000
    signal = np.zeros(n, dtype=bool)
    rng = np.random.default_rng(seed)
    drift = rng.normal(0, 0.002, n)
    fire_at = np.arange(120, 5800, 48)
    signal[fire_at] = True
    boost = np.zeros(n)
    for i in fire_at:
        boost[i + 1 : i + 25] += sign * 0.004
    close = np.exp(np.cumsum(drift + boost) + np.log(100))
    return pd.DataFrame({"close": close, "sig": signal, "rand": rng.random(n) < 0.02})


def test_event_study_detects_planted_edge_and_rejects_random() -> None:
    frame = _planted_world(+1)

    planted = event_study(frame, "sig", horizons=[24])
    assert planted["has_edge"], planted
    h24 = planted["horizons"][0]
    assert h24["n"] >= 100 and h24["t_stat_vs_drift"] >= 2.0
    assert h24["n"] == h24["n_raw"]  # spaced events: decimation is a no-op

    random_sig = event_study(frame, "rand", horizons=[24])
    assert not random_sig["has_edge"], random_sig


def test_event_study_detects_planted_short_edge() -> None:
    frame = _planted_world(-1)

    short = event_study(frame, "sig", horizons=[24], direction="short")
    assert short["has_edge"], short
    h24 = short["horizons"][0]
    assert h24["t_stat_vs_drift"] <= -2.0
    assert h24["hit_rate"] > 0.5  # direction-adjusted: mean(-fwd > 0)

    # The pre-fix behavior: a long-only read rejects the same genuine edge.
    long_only = event_study(frame, "sig", horizons=[24], direction="long")
    assert not long_only["has_edge"]

    auto = event_study(frame, "sig", horizons=[24], direction="auto")
    assert auto["has_edge"]
    assert auto["horizons"][0]["direction"] == "short"
    assert auto["trials_multiplier"] == 2


def test_event_study_decimates_clustered_events() -> None:
    rng = np.random.default_rng(5)
    n = 400
    close = np.exp(np.cumsum(rng.normal(0, 0.005, n)) + 4)
    sig = np.zeros(n, dtype=bool)
    sig[100:140] = True  # one 40-bar burst
    frame = pd.DataFrame({"close": close, "sig": sig})
    result = event_study(frame, "sig", horizons=[10])
    h10 = result["horizons"][0]
    assert h10["n_raw"] == 40
    assert h10["n"] == 4  # 40 consecutive bars at h=10 -> 4 non-overlapping


def test_event_study_flags_insufficient_sample() -> None:
    rng = np.random.default_rng(3)
    close = np.exp(np.cumsum(rng.normal(0, 0.01, 500)) + 4)
    sig = np.zeros(500, dtype=bool)
    sig[[50, 100, 150]] = True
    frame = pd.DataFrame({"close": close, "sig": sig})
    result = event_study(frame, "sig", horizons=[10])
    assert not result["has_edge"]
    assert result["horizons"][0]["n"] == 3
    assert "insufficient" in (result["horizons"][0]["note"] or "")


def test_event_study_missing_column_lists_available() -> None:
    frame = pd.DataFrame({"close": [1.0, 2.0], "z": [0, 1]})
    with pytest.raises(KeyError, match="z"):
        event_study(frame, "nope", horizons=[1])


class _FakePairFeed:
    """Injectable ccxt fake for pair_check_job: serves deterministic
    cointegrated candles for any symbol pair."""

    def __init__(self):
        y, x = _cointegrated_pair(n=9000)
        self._series = {"AAA": y, "BBB": x}
        # Anchor so the LAST bar is ~now: the real feed requests now-anchored
        # lookback windows.
        now_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
        self._base_ms = (now_ms // 3_600_000) * 3_600_000 - 9000 * 3_600_000

    async def load_markets(self):
        return {
            "AAA/USDT:USDT": {"active": True},
            "BBB/USDT:USDT": {"active": True},
        }

    async def fetch_ohlcv(self, pair, timeframe, since=None, limit=1000):
        coin = pair.split("/")[0]
        series = self._series[coin]
        base_ms = self._base_ms
        start_idx = max(0, int(((since or base_ms) - base_ms) // 3_600_000))
        out = []
        for i in range(start_idx, min(start_idx + limit, len(series))):
            px = float(series[i])
            out.append([base_ms + i * 3_600_000, px, px * 1.001, px * 0.999, px, 10.0])
        return out

    async def close(self):
        return None


def test_pair_check_job_end_to_end_writes_artifact(tmp_path) -> None:
    from wayfinder_paths.jobs.store import JobStore

    store = JobStore(repo_root=tmp_path)
    root = store.job_dir("pair-job")
    root.mkdir(parents=True, exist_ok=True)
    (root / "job.yaml").write_text("id: pair-job\n")
    (root / "execution_spec.json").write_text(
        json.dumps(
            {
                "market_kind": "perp",
                "data_contract": {"bar_interval": "1h", "symbols": ["AAA", "BBB"]},
            }
        )
    )
    report = pair_check_job(
        "pair-job",
        days=370,
        store=store,
        feed=_FakePairFeed(),
        fee_bps=0.5,
        slippage_bps=0.25,
    )
    assert report["verdict"] in ("PASS", "MARGINAL")
    assert report["pair"] == ["AAA", "BBB"]
    assert {c["name"] for c in report["checks"]} >= {
        "engle_granger_both_directions",
        "rolling_stability",
        "half_life",
        "cost_hurdle",
        "hurst",
        "mean_crossings",
        "data_sufficiency",
    }
    artifact = root / "results" / "research" / "pair_check" / "AAA_BBB.json"
    assert artifact.exists()
    persisted = json.loads(artifact.read_text())
    assert persisted["verdict"] == report["verdict"]


def test_pair_check_job_requires_two_symbols(tmp_path) -> None:
    from wayfinder_paths.jobs.store import JobStore

    store = JobStore(repo_root=tmp_path)
    root = store.job_dir("solo-job")
    root.mkdir(parents=True, exist_ok=True)
    (root / "job.yaml").write_text("id: solo-job\n")
    (root / "execution_spec.json").write_text(
        json.dumps(
            {
                "market_kind": "perp",
                "data_contract": {"bar_interval": "1h", "symbols": ["ETH"]},
            }
        )
    )
    with pytest.raises(ValueError, match="exactly 2"):
        pair_check_job("solo-job", store=store, feed=_FakePairFeed())


def test_op_runner_routes_research_ops(monkeypatch) -> None:
    import wayfinder_paths.jobs.research as research_mod
    from wayfinder_paths.jobs.execution import op_runner

    monkeypatch.setattr(
        research_mod,
        "pair_check_job",
        lambda job_id, **kw: {"stub": "pair", "job": job_id},
    )
    monkeypatch.setattr(
        research_mod,
        "signal_check_job",
        lambda job_id, **kw: {"stub": "signal", "column": kw.get("column")},
    )
    assert op_runner._run("pair_check", {"job_id": "j"}) == {"stub": "pair", "job": "j"}
    assert op_runner._run("signal_check", {"job_id": "j", "column": "z"}) == {
        "stub": "signal",
        "column": "z",
    }


def _cross_section(n_bars: int, n_symbols: int, *, informative: bool, seed: int = 5):
    """Synthetic multi-symbol frames. When informative, the ranking column
    equals each symbol's next-period relative performance rank plus noise —
    a real cross-sectional edge. Otherwise the column is pure noise."""
    from wayfinder_paths.jobs.research import rank_ic  # noqa: F401 (import check)

    rng = np.random.default_rng(seed)
    ts = pd.date_range("2024-01-01", periods=n_bars, freq="D", tz="UTC")
    # per-symbol daily relative strengths, drawn fresh each bar
    rel = rng.normal(0, 0.01, size=(n_bars, n_symbols))
    frames: dict[str, pd.DataFrame] = {}
    for j in range(n_symbols):
        # forward return of symbol j at bar i is rel[i+1, j]
        log_close = np.cumsum(np.concatenate([[0.0], rel[1:, j]])) + np.log(100)
        close = np.exp(log_close)
        if informative:
            score = np.empty(n_bars)
            score[:-1] = rel[1:, j] + rng.normal(0, 0.004, n_bars - 1)
            score[-1] = 0.0
        else:
            score = rng.normal(0, 1, n_bars)
        frames[f"S{j}"] = pd.DataFrame(
            {"timestamp": ts, "close": close, "score": score}
        )
    return frames


def test_rank_ic_detects_informative_ranking_and_rejects_noise() -> None:
    from wayfinder_paths.jobs.research import rank_ic

    informative = rank_ic(
        _cross_section(400, 10, informative=True), "score", horizons=[1]
    )
    assert informative["has_edge"], informative
    h1 = informative["horizons"][0]
    assert h1["n"] >= 30 and h1["mean_ic"] > 0.2 and h1["sign_stable"]

    noise = rank_ic(_cross_section(400, 10, informative=False), "score", horizons=[1])
    assert not noise["has_edge"], noise


def test_rank_ic_flags_insufficient_sample() -> None:
    from wayfinder_paths.jobs.research import rank_ic

    result = rank_ic(_cross_section(25, 6, informative=True), "score", horizons=[1])
    h1 = result["horizons"][0]
    assert h1["n"] < 30 and h1["note"] == "insufficient sample (n<30)"


def test_rank_ic_missing_column_lists_available() -> None:
    from wayfinder_paths.jobs.research import rank_ic

    frames = _cross_section(50, 4, informative=False)
    with pytest.raises(KeyError, match="score"):
        rank_ic(frames, "nope", horizons=[1])


def test_rank_check_op_routes(monkeypatch) -> None:
    import wayfinder_paths.jobs.research as research_mod
    from wayfinder_paths.jobs.execution import op_runner

    monkeypatch.setattr(
        research_mod,
        "rank_check_job",
        lambda job_id, **kw: {"stub": "rank", "column": kw.get("column")},
    )
    assert op_runner._run("rank_check", {"job_id": "j", "column": "mom"}) == {
        "stub": "rank",
        "column": "mom",
    }
