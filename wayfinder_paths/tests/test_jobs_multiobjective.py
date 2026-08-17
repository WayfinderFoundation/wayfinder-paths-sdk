"""Multi-objective optuna search: NSGA-II over 2+ grid axes returns the
Pareto front (ranked leads with it, rows carry pareto flags), the grid
contract is preserved, and bad inputs fail loudly. Reuses the HoldK peaked
fixture from the single-objective optuna tests."""

from __future__ import annotations

import pytest

pytest.importorskip("optuna")

from wayfinder_paths.tests.test_jobs_optuna import _search  # noqa: E402


def test_multiobjective_returns_pareto_front() -> None:
    result = _search(n_trials=12, objectives=["net_return", "max_drawdown_pct"], seed=7)
    assert result.optimizer == "nsga2"
    assert result.search["sampler"] == "nsga2"
    assert result.search["objectives"] == ["net_return", "max_drawdown_pct"]

    front = result.search["pareto_front"]
    assert front, "peaked fixture must produce a non-empty Pareto front"
    for member in front:
        assert set(member) == {"number", "values", "params"}
        assert len(member["values"]) == 2

    flagged = [row for row in result.runs if row.get("pareto")]
    assert {row["trial"] for row in flagged} >= {m["number"] for m in front}
    # ranked leads with Pareto members; no non-member outranks a member.
    seen_non_pareto = False
    for row in result.ranked:
        if row.get("pareto"):
            assert not seen_non_pareto, "Pareto member ranked below non-member"
        else:
            seen_non_pareto = True
    # Front dominance sanity: no member dominates another on the raw axes.
    for a in front:
        for b in front:
            if a is b:
                continue
            dominates = (
                a["values"][0] >= b["values"][0]
                and a["values"][1] <= b["values"][1]
                and (a["values"][0] > b["values"][0] or a["values"][1] < b["values"][1])
            )
            assert not dominates


def test_multiobjective_determinism_and_validation() -> None:
    first = _search(n_trials=8, objectives=["net_return", "max_drawdown_pct"])
    second = _search(n_trials=8, objectives=["net_return", "max_drawdown_pct"])
    assert [r["params"] for r in first.runs] == [r["params"] for r in second.runs]

    with pytest.raises(ValueError, match="2\\+ axes"):
        _search(objectives=["net_return"])
    with pytest.raises(ValueError, match="rank_by"):
        _search(objectives=["net_return", "not_a_metric"])


def test_single_objective_contract_unchanged() -> None:
    result = _search(n_trials=8)
    assert result.optimizer == "optuna"
    assert result.search["best_trial"] is not None
    assert "objectives" not in result.search
    assert all("pareto" not in row for row in result.runs)
