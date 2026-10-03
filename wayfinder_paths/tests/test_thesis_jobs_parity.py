"""Frozen outputs from jobs-v1 + thesis bridge at 353c92b7, before extraction."""

import json
from pathlib import Path

import pytest

from wayfinder_paths.tests.test_thesis_backtest import market, position, run

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures/thesis_jobs_parity.json").read_text()
)["fixtures"]


@pytest.mark.parametrize(
    "fixture", FIXTURES, ids=lambda f: f"{f['name']}-{f['budget']}"
)
def test_jobs_v1_parity(fixture: dict) -> None:
    result = run(
        position(**fixture["position"]),
        market(fixture["prices"], **fixture["costs"]),
        fixture["budget"],
    )
    for key, expected in fixture["expected"].items():
        assert result[key] == expected, key
