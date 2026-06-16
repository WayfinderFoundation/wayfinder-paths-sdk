from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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
                    {"id": "final", "a": {"participant": "a"}, "b": {"participant": "b"}}
                ]
            },
            "markets": [{"participant_id": "a", "bid": 0.1}],
        }
    )

    row = next(row for row in run_simulation(config) if row.participant_id == "a")

    assert row.price_source == "bid_only"
    assert row.decision == "WATCH"


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
