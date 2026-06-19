from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from wayfinder_paths.quant.event_sim import load_config, run_simulation


def test_bracket_simulation_ranks_stronger_participants() -> None:
    config = load_config(
        {
            "iterations": 4000,
            "seed": 7,
            "participants": [
                {"id": "a", "name": "A", "rating": 2050},
                {"id": "b", "name": "B", "rating": 1900},
                {"id": "c", "name": "C", "rating": 1800},
                {"id": "d", "name": "D", "rating": 1700},
            ],
            "bracket": {
                "matches": [
                    {
                        "id": "s1",
                        "a": {"participant": "a"},
                        "b": {"participant": "b"},
                    },
                    {
                        "id": "s2",
                        "a": {"participant": "c"},
                        "b": {"participant": "d"},
                    },
                    {
                        "id": "final",
                        "a": {"winner": "s1"},
                        "b": {"winner": "s2"},
                    },
                ],
                "champion_match": "final",
            },
        }
    )

    rows = run_simulation(config)
    probs = {row.participant_id: row.probability for row in rows}

    assert abs(sum(probs.values()) - 1.0) < 1e-9
    assert probs["a"] > probs["b"] > probs["c"] > probs["d"]


def test_evidence_cards_adjust_effective_rating() -> None:
    base = {
        "iterations": 3000,
        "seed": 11,
        "participants": [
            {
                "id": "a",
                "name": "A",
                "rating": 1800,
                "evidence": [
                    {
                        "claim": "important current-state edge",
                        "direction": "for_yes",
                        "strength": "strong",
                        "sourceQuality": "primary",
                        "freshness": "fresh",
                        "independence": "independent",
                        "alreadyPriced": "unlikely",
                        "resolutionRelevance": "direct",
                    }
                ],
            },
            {"id": "b", "name": "B", "rating": 1800},
        ],
        "bracket": {
            "matches": [
                {"id": "final", "a": {"participant": "a"}, "b": {"participant": "b"}}
            ]
        },
    }

    rows = run_simulation(load_config(base))
    probs = {row.participant_id: row.probability for row in rows}

    assert probs["a"] > 0.6
    assert probs["b"] < 0.4


def test_completed_group_state_conditions_slots() -> None:
    config = load_config(
        {
            "iterations": 2000,
            "seed": 13,
            "participants": [
                {"id": "strong", "name": "Strong", "rating": 2000},
                {"id": "mid", "name": "Mid", "rating": 1800},
                {"id": "weak", "name": "Weak", "rating": 1500},
            ],
            "groups": [
                {
                    "id": "G",
                    "participants": ["strong", "mid", "weak"],
                    "qualifiers": [{"rank": 1, "slot": "G1"}],
                    "matches": [
                        {
                            "a": "strong",
                            "b": "weak",
                            "status": "completed",
                            "score": [0, 2],
                        },
                        {"a": "strong", "b": "mid"},
                        {"a": "mid", "b": "weak"},
                    ],
                }
            ],
            "bracket": {
                "matches": [
                    {
                        "id": "final",
                        "a": {"slot": "G1"},
                        "b": {"participant": "mid"},
                    }
                ]
            },
            "markets": [{"participant_id": "strong", "bid": 0.4, "ask": 0.5}],
        }
    )

    rows = run_simulation(config)
    strong = next(row for row in rows if row.participant_id == "strong")

    assert strong.classification == "live_conditioned"
    assert strong.market_price == 0.45


def test_partial_group_pack_simulates_remaining_round_robin() -> None:
    config = load_config(
        {
            "iterations": 1200,
            "seed": 17,
            "participants": [
                {"id": "a", "name": "A", "rating": 1900},
                {"id": "b", "name": "B", "rating": 1850},
                {"id": "c", "name": "C", "rating": 1600},
            ],
            "groups": [
                {
                    "id": "G",
                    "participants": ["a", "b", "c"],
                    "qualifiers": [{"rank": 1, "slot": "G1"}],
                    "matches": [
                        {"a": "a", "b": "b", "status": "completed", "score": [0, 1]}
                    ],
                }
            ],
            "bracket": {
                "matches": [
                    {
                        "id": "final",
                        "a": {"slot": "G1"},
                        "b": {"participant": "c"},
                    }
                ]
            },
        }
    )

    rows = run_simulation(config)
    probs = {row.participant_id: row.probability for row in rows}

    assert probs["a"] > 0.0
    assert probs["b"] > probs["a"]


def test_champion_bracket_requires_first_place_slots_to_reach_target() -> None:
    """Fail fast when a generated event pack leaves a group winner outside the
    champion path instead of running a long malformed Monte Carlo."""
    config = load_config(
        {
            "iterations": 1000,
            "seed": 101,
            "participants": [
                {"id": "a", "name": "A", "rating": 1900},
                {"id": "b", "name": "B", "rating": 1800},
                {"id": "c", "name": "C", "rating": 1700},
                {"id": "d", "name": "D", "rating": 1600},
            ],
            "groups": [
                {
                    "id": "G1",
                    "participants": ["a", "b"],
                    "qualifiers": [{"rank": 1, "slot": "G1_1"}],
                },
                {
                    "id": "G2",
                    "participants": ["c", "d"],
                    "qualifiers": [{"rank": 1, "slot": "G2_1"}],
                },
            ],
            "bracket": {
                "matches": [
                    {
                        "id": "final",
                        "a": {"slot": "G1_1"},
                        "b": {"participant": "b"},
                    }
                ],
                "champion_match": "final",
            },
            "target": {"type": "champion"},
        }
    )

    with pytest.raises(ValueError, match="first-place slot 'G2_1'"):
        run_simulation(config)


def test_wildcard_slots_are_validated_before_long_run() -> None:
    config = load_config(
        {
            "iterations": 1000,
            "seed": 103,
            "participants": [
                {"id": "a", "name": "A", "rating": 1900},
                {"id": "b", "name": "B", "rating": 1800},
                {"id": "c", "name": "C", "rating": 1700},
                {"id": "d", "name": "D", "rating": 1600},
                {"id": "e", "name": "E", "rating": 1500},
                {"id": "f", "name": "F", "rating": 1400},
            ],
            "groups": [
                {
                    "id": "G1",
                    "participants": ["a", "b", "c"],
                    "qualifiers": [{"rank": 1, "slot": "G1_1"}],
                },
                {
                    "id": "G2",
                    "participants": ["d", "e", "f"],
                    "qualifiers": [{"rank": 1, "slot": "G2_1"}],
                },
            ],
            "wildcards": [{"source_rank": 2, "count": 1, "slot_prefix": "WC"}],
            "bracket": {
                "matches": [
                    {"id": "s1", "a": {"slot": "G1_1"}, "b": {"slot": "WC2"}},
                    {"id": "final", "a": {"winner": "s1"}, "b": {"slot": "G2_1"}},
                ],
                "champion_match": "final",
            },
            "target": {"type": "champion"},
        }
    )

    with pytest.raises(ValueError, match="slot 'WC2' that cannot be assigned"):
        run_simulation(config)


def test_slot_target_supports_non_winner_take_all_markets() -> None:
    """Anti-overfit guard: path markets are not always trophy/champion markets."""
    config = load_config(
        {
            "iterations": 2500,
            "seed": 23,
            "participants": [
                {"id": "a", "name": "A", "rating": 1950},
                {"id": "b", "name": "B", "rating": 1850},
                {"id": "c", "name": "C", "rating": 1750},
                {"id": "d", "name": "D", "rating": 1650},
            ],
            "groups": [
                {
                    "id": "promotion_table",
                    "participants": ["a", "b", "c", "d"],
                    "qualifiers": [
                        {"rank": 1, "slot": "PROMO1"},
                        {"rank": 2, "slot": "PROMO2"},
                    ],
                }
            ],
            "target": {"type": "slot", "slots": ["PROMO1", "PROMO2"]},
            "markets": [{"participant_id": "b", "bid": 0.52, "ask": 0.54}],
        }
    )

    rows = run_simulation(config)
    probs = {row.participant_id: row.probability for row in rows}
    row_b = next(row for row in rows if row.participant_id == "b")

    assert 1.95 < sum(probs.values()) < 2.05
    assert probs["a"] > probs["b"] > probs["c"] > probs["d"]
    assert row_b.market_price == 0.53
    assert row_b.classification == "clean_unplayed"


def test_bid_only_market_is_not_buy_candidate() -> None:
    config = load_config(
        {
            "iterations": 800,
            "seed": 19,
            "min_edge_abs": 0.001,
            "min_edge_rel": 0.01,
            "participants": [
                {"id": "a", "name": "A", "rating": 2100},
                {"id": "b", "name": "B", "rating": 1500},
            ],
            "bracket": {
                "matches": [
                    {
                        "id": "final",
                        "a": {"participant": "a"},
                        "b": {"participant": "b"},
                    }
                ]
            },
            "markets": [{"participant_id": "a", "bid": 0.1}],
        }
    )

    row = next(row for row in run_simulation(config) if row.participant_id == "a")

    assert row.price_source == "bid_only"
    assert row.decision == "WATCH"


def test_multiple_venue_markets_do_not_overwrite_each_other() -> None:
    config = load_config(
        {
            "iterations": 1000,
            "seed": 29,
            "min_edge_abs": 0.001,
            "min_edge_rel": 0.01,
            "participants": [
                {"id": "a", "name": "A", "rating": 2100},
                {"id": "b", "name": "B", "rating": 1500},
            ],
            "bracket": {
                "matches": [
                    {
                        "id": "final",
                        "a": {"participant": "a"},
                        "b": {"participant": "b"},
                    }
                ]
            },
            "markets": [
                {"participant_id": "a", "venue": "polymarket", "bid": 0.2, "ask": 0.21},
                {
                    "participant_id": "a",
                    "venue": "hyperliquid",
                    "bid": 0.3,
                    "ask": 0.31,
                },
            ],
        }
    )

    rows = [row for row in run_simulation(config) if row.participant_id == "a"]
    by_venue = {row.venue: row for row in rows}

    assert set(by_venue) == {"polymarket", "hyperliquid"}
    assert round(by_venue["polymarket"].market_price or 0, 6) == 0.205
    assert by_venue["polymarket"].entry_price == 0.21
    assert round(by_venue["hyperliquid"].market_price or 0, 6) == 0.305
    assert by_venue["hyperliquid"].entry_price == 0.31


def test_legacy_evidence_direction_aliases_are_normalized() -> None:
    config = load_config(
        {
            "iterations": 1000,
            "seed": 31,
            "min_edge_abs": 0.001,
            "min_edge_rel": 0.01,
            "participants": [
                {
                    "id": "a",
                    "name": "A",
                    "rating": 2100,
                    "evidence": [
                        {
                            "claim": "legacy negative direction should count",
                            "direction": "sign_against",
                            "strength": "strong",
                        }
                    ],
                },
                {"id": "b", "name": "B", "rating": 1500},
            ],
            "bracket": {
                "matches": [
                    {
                        "id": "final",
                        "a": {"participant": "a"},
                        "b": {"participant": "b"},
                    }
                ]
            },
            "markets": [{"participant_id": "a", "bid": 0.1, "ask": 0.11}],
        }
    )

    row = next(row for row in run_simulation(config) if row.participant_id == "a")

    assert config.participants["a"].evidence[0]["direction"] == "against_yes"
    assert "invalid_evidence_direction" not in row.diagnostic_flags
    assert row.ignored_evidence == ()


def test_unsupported_evidence_direction_is_surfaced_and_gates_action() -> None:
    config = load_config(
        {
            "iterations": 1000,
            "seed": 31,
            "min_edge_abs": 0.001,
            "min_edge_rel": 0.01,
            "participants": [
                {
                    "id": "a",
                    "name": "A",
                    "rating": 2100,
                    "evidence": [
                        {
                            "claim": "neutral context should not move the posterior",
                            "direction": "neutral",
                            "strength": "strong",
                        }
                    ],
                },
                {"id": "b", "name": "B", "rating": 1500},
            ],
            "bracket": {
                "matches": [
                    {
                        "id": "final",
                        "a": {"participant": "a"},
                        "b": {"participant": "b"},
                    }
                ]
            },
            "markets": [{"participant_id": "a", "bid": 0.1, "ask": 0.11}],
        }
    )

    row = next(row for row in run_simulation(config) if row.participant_id == "a")

    assert row.decision == "WATCH"
    assert "invalid_evidence_direction" in row.diagnostic_flags
    assert row.ignored_evidence[0]["direction"] == "neutral"


def test_market_implied_ratings_are_diagnostic_only() -> None:
    config = load_config(
        {
            "iterations": 1000,
            "seed": 37,
            "min_edge_abs": 0.001,
            "min_edge_rel": 0.01,
            "modelProvenance": {
                "ratingSource": "sportsbook futures outright fair probabilities"
            },
            "participants": [
                {"id": "a", "name": "A", "rating": 2100},
                {"id": "b", "name": "B", "rating": 1500},
            ],
            "bracket": {
                "matches": [
                    {
                        "id": "final",
                        "a": {"participant": "a"},
                        "b": {"participant": "b"},
                    }
                ]
            },
            "markets": [{"participant_id": "a", "bid": 0.1, "ask": 0.11}],
        }
    )

    row = next(row for row in run_simulation(config) if row.participant_id == "a")

    assert row.decision == "WATCH"
    assert "market_implied_ratings_diagnostic_only" in row.diagnostic_flags


def test_approximate_bracket_downgrades_buy_candidate() -> None:
    config = load_config(
        {
            "iterations": 1000,
            "seed": 41,
            "min_edge_abs": 0.001,
            "min_edge_rel": 0.01,
            "modelProvenance": {"bracketSource": "simplified approximate bracket"},
            "participants": [
                {"id": "a", "name": "A", "rating": 2100},
                {"id": "b", "name": "B", "rating": 1500},
            ],
            "bracket": {
                "matches": [
                    {
                        "id": "final",
                        "a": {"participant": "a"},
                        "b": {"participant": "b"},
                    }
                ]
            },
            "markets": [{"participant_id": "a", "bid": 0.1, "ask": 0.11}],
        }
    )

    row = next(row for row in run_simulation(config) if row.participant_id == "a")

    assert row.decision == "WATCH"
    assert "approx_bracket" in row.diagnostic_flags


def test_event_sim_cli_writes_artifacts(tmp_path: Path) -> None:
    event_pack = tmp_path / "event.json"
    event_pack.write_text(
        json.dumps(
            {
                "iterations": 500,
                "seed": 1,
                "participants": [
                    {"id": "a", "name": "A", "rating": 1900},
                    {"id": "b", "name": "B", "rating": 1700},
                ],
                "bracket": {
                    "matches": [
                        {
                            "id": "final",
                            "a": {"participant": "a"},
                            "b": {"participant": "b"},
                        }
                    ]
                },
            }
        )
    )
    out_dir = tmp_path / "out"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "wayfinder_paths.quant.event_sim",
            "--input",
            str(event_pack),
            "--out",
            str(out_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "EVENT MARKET SIM" in proc.stdout
    assert (out_dir / "event_sim.json").exists()
    assert (out_dir / "event_sim.csv").exists()
