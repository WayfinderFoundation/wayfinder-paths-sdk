from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from wayfinder_paths.jobs import policy_scan as ps
from wayfinder_paths.jobs.strategies._starter_utils import (
    ranked_weights,
    sleeve_weights,
)

BAR_SECONDS = 900


def _panel_frames(
    n_bars: int = 6_000,
    symbols: tuple[str, ...] = ("AAA", "BBB", "CCC", "DDD", "PAXG", "EEE"),
    *,
    seed: int = 0,
    drift: dict[str, float] | None = None,
    macro: bool = False,
) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    stamps = pd.date_range("2026-01-01", periods=n_bars, freq="15min", tz="UTC")
    frames: dict[str, pd.DataFrame] = {}
    # AAA and BBB share a factor so the correlation pairing puts them in one
    # sleeve; their drifts are what a duel between them can earn.
    factor = rng.normal(0.0, 0.003, size=n_bars)
    for symbol in symbols:
        mu = (drift or {}).get(symbol, 0.0)
        steps = rng.normal(mu, 0.004, size=n_bars)
        if symbol in {"AAA", "BBB"}:
            steps = steps + factor
        close = 100.0 * np.exp(np.cumsum(steps))
        frame = pd.DataFrame({"timestamp": stamps, "close": close})
        if macro:
            frame["macro_regime"] = np.where(np.arange(n_bars) < n_bars // 2, -1.0, 1.0)
        frames[symbol] = frame
    return frames


def test_grid_hours_convert_to_bars_and_ids_are_stable() -> None:
    panel = ps.build_panel(_panel_frames(), bar_seconds=BAR_SECONDS)
    assert panel.bars(24) == 96
    assert panel.bars(0.1) == 1
    family = next(f for f in ps.FAMILIES if f.name == "cross_sectional_rank")
    configs = list(ps._configs(family, panel))
    assert configs[0]["momentum_bars"] == 96
    assert "momentum_h" not in configs[0]
    first = ps._policy_id("cross_sectional_rank", configs[0])
    assert first == ps._policy_id("cross_sectional_rank", dict(configs[0]))
    assert first != ps._policy_id("cross_sectional_rank", configs[1])


def test_panel_needs_breadth_and_common_history() -> None:
    with pytest.raises(ValueError, match="at least 3 symbols"):
        ps.build_panel(_panel_frames(symbols=("AAA", "BBB")), bar_seconds=BAR_SECONDS)
    with pytest.raises(ValueError, match="common bars"):
        ps.build_panel(_panel_frames(n_bars=500), bar_seconds=BAR_SECONDS)
    panel = ps.build_panel(_panel_frames(), bar_seconds=BAR_SECONDS)
    assert panel.defensive == "PAXG"
    assert "PAXG" not in panel.risk_symbols


def test_rank_and_sleeve_weights_match_the_kernel_helpers() -> None:
    panel = ps.build_panel(_panel_frames(), bar_seconds=BAR_SECONDS)
    score = panel.close.pct_change(96)
    bar = 4_000
    scores = {s: float(score[s].iloc[bar]) for s in panel.symbols}
    rank = ps._rank_weights(
        panel,
        {
            "momentum_bars": 96,
            "rank_legs": 2,
            "weight_per_leg": 0.25,
            "rebalance_bars": 1,
        },
    )
    expected = ranked_weights(scores, weight_per_leg=0.25, legs=2)
    assert {s: round(float(rank[s].iloc[bar]), 6) for s in panel.symbols} == {
        s: round(v, 6) for s, v in expected.items()
    }
    sleeves = ps.panel_sleeves(panel)
    assert len(sleeves) == 2 and len({s for pair in sleeves for s in pair}) == 4
    sleeve = ps._sleeve_weights(
        panel,
        {
            "momentum_bars": 96,
            "weight_per_leg": 0.125,
            "rebalance_bars": 1,
            "sleeves": sleeves,
        },
    )
    expected_sleeve = sleeve_weights(scores, sleeves, weight_per_leg=0.125)
    for symbol, value in expected_sleeve.items():
        assert round(float(sleeve[symbol].iloc[bar]), 6) == round(value, 6)


def test_rotation_weights_follow_the_kernel_rules() -> None:
    frames = _panel_frames(drift={"AAA": 0.0006, "BBB": 0.0004, "CCC": 0.0003})
    panel = ps.build_panel(frames, bar_seconds=BAR_SECONDS)
    params = {
        "momentum_bars": 96,
        "fast_sma_bars": 24,
        "slow_sma_bars": 96,
        "require_trend_alignment": True,
        "minimum_breadth": 0.5,
        "top_n": 1,
        "gross_exposure": 0.4,
        "rebalance_bars": 1,
        "defensive_symbol": "PAXG",
        "risk_symbols": panel.risk_symbols,
    }
    weights = ps._rotation_weights(panel, params)
    close = panel.close
    momentum = close.pct_change(96)
    fast = close.rolling(24).mean()
    slow = close.rolling(96).mean()
    risk = panel.risk_symbols
    for bar in (2_000, 3_500, 5_000):
        eligible = {
            s: float(momentum[s].iloc[bar])
            for s in risk
            if momentum[s].iloc[bar] > 0
            and fast[s].iloc[bar] > slow[s].iloc[bar]
            and close[s].iloc[bar] > slow[s].iloc[bar]
        }
        expected = dict.fromkeys(panel.symbols, 0.0)
        if len(eligible) / len(risk) >= 0.5:
            leader = max(eligible, key=lambda s: (eligible[s], s))
            expected[leader] = 0.4
        elif momentum["PAXG"].iloc[bar] > 0:
            expected["PAXG"] = 0.4
        assert {
            s: round(float(weights[s].iloc[bar]), 6) for s in panel.symbols
        } == expected
    # Gross never exceeds the configured exposure.
    assert float(weights.abs().sum(axis=1).max()) <= 0.4 + 1e-9


def test_hold_keeps_targets_between_rebalance_bars() -> None:
    index = pd.date_range("2026-01-01", periods=10, freq="15min", tz="UTC")
    weights = pd.DataFrame({"AAA": np.arange(10, dtype=float)}, index=index)
    held = ps._hold(weights, 4)
    assert held["AAA"].tolist() == [0, 0, 0, 0, 4, 4, 4, 4, 8, 8]


def test_planted_relative_trend_survives_and_random_walks_do_not() -> None:
    # One pair with a persistent relative drift: the sleeve duel earns on both
    # windows; the pure noise families cannot show a Sharpe of one twice.
    frames = _panel_frames(n_bars=12_000, drift={"AAA": 0.0005, "BBB": -0.0005}, seed=3)
    out = ps.policy_scan(
        frames, bar_seconds=BAR_SECONDS, cost_bps_per_side=8.0, limit=12
    )
    assert out["available"] and out["configs"] > 500
    by_name = {f["name"]: f for f in out["families"]}
    assert by_name["sleeve_momentum"]["verdict"] == "survivor"
    assert out["survivors"], out["funnel"]
    assert out["survivors"][0]["pointer"] == "/policy_scan/survivors/0"
    # The rank family sees the same divergence (long AAA, short BBB); the
    # sleeve survivor must pair the two planted legs.
    sleeve = next(r for r in out["survivors"] if r["family"] == "sleeve_momentum")
    assert sleeve["recipe"]["module"].endswith("mixed_sleeve_momentum")
    assert ["AAA", "BBB"] in [sorted(p) for p in sleeve["recipe"]["params"]["sleeves"]]
    for row in out["survivors"]:
        assert row["rank"]["sharpe"] >= 1 and row["report"]["sharpe"] >= 1
        assert row["report"]["return"] > 0
        assert isinstance(row["haircut"]["cleared_family"], bool)
    assert set(out["funnel"]) == {
        "configs",
        "families",
        "robust",
        "survivors",
        "falsified",
    }
    # Everything the pack carries is JSON with rounded floats.
    encoded = json.dumps(out, sort_keys=True)
    assert len(encoded) < 60_000
    for family in out["families"]:
        if "best" in family:
            assert set(family["best"]["rank"]) == {"return", "sharpe"}


def test_regime_split_uses_the_store_label() -> None:
    frames = _panel_frames(n_bars=8_000, macro=True, drift={"AAA": 0.0004})
    out = ps.policy_scan(frames, bar_seconds=BAR_SECONDS, cost_bps_per_side=8.0)
    assert out["macro_split"] is True
    rows = [r for f in out["families"] for r in ([f] if "best" in f else [])]
    assert rows
    survivors = out["survivors"]
    if survivors:
        assert set(survivors[0]["by_regime"]) <= {"bull", "chop", "bear"}


def test_relay_needs_a_defensive_symbol() -> None:
    frames = _panel_frames(symbols=("AAA", "BBB", "CCC", "DDD"))
    out = ps.policy_scan(frames, bar_seconds=BAR_SECONDS, cost_bps_per_side=8.0)
    relay = next(f for f in out["families"] if f["name"] == "risk_haven_relay")
    assert relay["skipped"] == "no defensive symbol in the panel"
    assert out["defensive_symbol"] is None
