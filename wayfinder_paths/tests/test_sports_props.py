import math

import pytest

from wayfinder_paths.quant import sports_props as sp

# ── odds / de-vig ────────────────────────────────────────────────────────────


def test_american_to_implied_and_payout():
    assert sp.american_to_implied(100) == pytest.approx(0.5)
    assert sp.american_to_implied(-200) == pytest.approx(2 / 3)
    assert sp.american_to_implied(150) == pytest.approx(0.4)
    assert sp.american_payout(100) == pytest.approx(1.0)
    assert sp.american_payout(-200) == pytest.approx(0.5)


def test_devig_sums_to_one_and_removes_vig():
    p_over, p_under = sp.devig_two_way(-110, -110)
    assert p_over == pytest.approx(0.5)
    assert p_over + p_under == pytest.approx(1.0)
    # raw implied probs summed to > 1 (the vig); de-vigged they sum to exactly 1
    raw = sp.american_to_implied(-110) + sp.american_to_implied(-110)
    assert raw > 1.0


# ── distributions ────────────────────────────────────────────────────────────


def test_prob_over_normal_symmetry_and_direction():
    # line at the mean -> 50/50; below the mean -> >50%
    assert sp.prob_over(20.0, 5.0, 20.0) == pytest.approx(0.5)
    assert sp.prob_over(20.0, 5.0, 15.0) > 0.5
    assert sp.prob_over(20.0, 5.0, 25.0) < 0.5


def test_prob_over_poisson_low_count():
    # mean 1.5 blocks, line 0.5 -> P(>=1) = 1 - e^-1.5
    p = sp.prob_over(1.5, 0.0, 0.5, distribution="poisson")
    assert p == pytest.approx(1.0 - math.exp(-1.5), abs=1e-6)


def test_pick_distribution():
    assert sp.pick_distribution("points") == "normal"
    assert sp.pick_distribution("blocks") == "poisson"
    assert sp.pick_distribution("threes") == "poisson"


# ── adjustments ──────────────────────────────────────────────────────────────


def test_pace_and_opponent_factors():
    # both teams above league pace -> >1 multiplier
    assert sp.pace_factor(104, 102, 100) == pytest.approx(1.03)
    # opponent worse defense (higher def_rating) inflates a scoring projection
    assert sp.opponent_factor(120, 113) > 1.0
    assert sp.opponent_factor(108, 113) < 1.0
    # weight dampens the effect
    assert sp.opponent_factor(120, 113, weight=0.5) < sp.opponent_factor(
        120, 113, weight=1.0
    )


# ── projection ───────────────────────────────────────────────────────────────


def test_parse_minutes():
    assert sp.parse_minutes("33:30") == pytest.approx(33.5)
    assert sp.parse_minutes(28) == 28.0
    assert sp.parse_minutes(None) == 0.0


def test_projection_minutes_based_and_recency():
    # 10 games at 20 pts / 30 min; recent 3 games hotter (28 pts) -> projection > 20
    logs = [{"pts": 28, "min": "32:00"}] * 3 + [{"pts": 20, "min": "30:00"}] * 7
    season = {"pts": 18, "min": "30:00"}
    proj = sp.project_stat(logs, season, ("pts",), recent_n=15, half_life=3.0)
    assert proj.n == 10
    assert proj.mean > 20.0  # recency weights the hot stretch up
    assert proj.std > 0


def test_projection_combo_sums_components():
    logs = [{"pts": 20, "reb": 8, "ast": 5, "min": "34:00"}] * 8
    proj = sp.project_stat(logs, None, ("pts", "reb", "ast"), recent_n=15)
    assert proj.mean == pytest.approx(33.0, rel=0.05)  # ~20+8+5


def test_projection_zero_logs_falls_back_to_season():
    proj = sp.project_stat([], {"pts": 15, "min": "25:00"}, ("pts",))
    assert proj.n == 0
    assert proj.mean == pytest.approx(15.0)


# ── value + orchestration ────────────────────────────────────────────────────


def test_prop_value_picks_positive_edge_side():
    # model thinks OVER is 65% but book (de-vigged) is 50% -> bet OVER, positive edge/EV
    v = sp.prop_value(0.65, 0.5, -110, -110)
    assert v.side == "OVER"
    assert v.edge == pytest.approx(0.15)
    assert v.ev > 0
    assert v.kelly > 0


def test_prop_value_flips_to_under_when_model_low():
    v = sp.prop_value(0.30, 0.5, -110, -110)
    assert v.side == "UNDER"
    assert v.edge == pytest.approx(0.20)


def test_score_prop_end_to_end_and_flags():
    logs = [{"pts": 26, "min": "35:00"}] * 12
    season = {"pts": 24, "min": "34:00"}
    prop = {
        "player_id": 1,
        "prop_type": "points",
        "line": 22.5,
        "over_odds": -110,
        "under_odds": -110,
    }
    score = sp.score_prop(prop, logs, season, opponent_factor=1.05, pace_factor=1.02)
    assert score is not None
    assert score.side == "OVER"  # projects ~26 vs a 22.5 line
    assert score.model_p > 0.5
    assert "low_sample(12)" not in score.flags
    # injured flag is recorded but the bet still scores
    inj = sp.score_prop(prop, logs, season, injured=True)
    assert "injured" in inj.flags


def test_score_prop_unknown_type_returns_none():
    assert (
        sp.score_prop(
            {
                "prop_type": "double_double",
                "line": 0.5,
                "over_odds": 100,
                "under_odds": -120,
            },
            [],
            None,
        )
        is None
    )
