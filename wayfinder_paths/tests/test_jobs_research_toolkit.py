from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from wayfinder_paths.jobs.execution.engine import (
    EngineState,
    TickResult,
    _apply_market_event,
)
from wayfinder_paths.jobs.execution.gates import (
    latest_gate_state,
    summarize_gate_diagnostics,
)
from wayfinder_paths.jobs.execution.job import _funding_market_events
from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    ExecutionTrace,
    PositionRecord,
)
from wayfinder_paths.jobs.execution.simulator import PreparedExecutionDataset
from wayfinder_paths.jobs.execution.venues import MarketEvent
from wayfinder_paths.jobs.indicators import (
    compute_indicator,
    panel_breadth,
    realized_volatility,
    trailing_return,
)
from wayfinder_paths.jobs.research_contract import RESEARCH_CONTRACT_VERSION
from wayfinder_paths.jobs.robustness import (
    _matches,
    _warning_codes,
    robustness_check_job,
    validate_robustness_plan,
)


def _rows(*, gates: bool = False) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for hour, (a_gate, b_gate) in enumerate(((False, True), (True, False))):
        timestamp = f"2026-01-01T0{hour}:00:00Z"
        for symbol, value in (("A", a_gate), ("B", b_gate)):
            row: dict[str, object] = {
                "timestamp": timestamp,
                "symbol": symbol,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0 + hour,
                "volume": 10.0,
            }
            if gates:
                row["gate_portfolio"] = hour == 1
                row["gate_symbol"] = value
            rows.append(row)
    return rows


def test_shared_return_volatility_and_breadth_are_causal() -> None:
    close = pd.Series([100.0, 110.0, 121.0, 108.9, 119.79])
    expected_return = close.pct_change(2)
    expected_vol = close.pct_change().rolling(2, min_periods=2).std()
    pd.testing.assert_series_equal(trailing_return(close, 2), expected_return)
    pd.testing.assert_series_equal(realized_volatility(close, 2), expected_vol)

    frame = pd.DataFrame(
        {
            "close": close,
            "high": close + 1,
            "low": close - 1,
            "open": close,
            "volume": 1.0,
        }
    )
    assert compute_indicator(frame, "ret:2")["ret2"].iloc[-1] == pytest.approx(
        close.iloc[-1] / close.iloc[-3] - 1
    )
    assert compute_indicator(frame, "rv:2")["rv2"].iloc[-1] == pytest.approx(
        expected_vol.iloc[-1]
    )

    extended = pd.concat([close, pd.Series([10_000.0])], ignore_index=True)
    pd.testing.assert_series_equal(
        trailing_return(extended, 2).iloc[: len(close)].reset_index(drop=True),
        trailing_return(close, 2).reset_index(drop=True),
    )
    panel = pd.DataFrame({"A": [0.2, 0.2], "B": [0.1, None], "C": [0.0, None]})
    breadth = panel_breadth(panel, 0.1, min_assets=2)
    assert breadth.iloc[0] == pytest.approx(2 / 3)
    assert pd.isna(breadth.iloc[1])


def test_gate_snapshots_distinguish_portfolio_and_symbol_scope() -> None:
    view = CompletedBarsView.from_rows(_rows(gates=True))
    gates = latest_gate_state(view)
    assert gates["gate_portfolio"] == {
        "scope": "portfolio",
        "active": True,
        "by_symbol": {"A": True, "B": True},
    }
    assert gates["gate_symbol"]["scope"] == "symbol"
    assert gates["gate_symbol"]["active"] is None
    assert gates["gate_symbol"]["by_symbol"] == {"A": True, "B": False}


def test_gate_diagnostics_do_not_attribute_portfolio_pnl_to_symbol_gates() -> None:
    runs = [
        {
            "timestamp": "t0",
            "gates": {
                "gate_portfolio": {
                    "scope": "portfolio",
                    "active": False,
                    "by_symbol": {"A": False},
                },
                "gate_symbol": {
                    "scope": "symbol",
                    "active": None,
                    "by_symbol": {"A": False},
                },
            },
        },
        {
            "timestamp": "t1",
            "gates": {
                "gate_portfolio": {
                    "scope": "portfolio",
                    "active": True,
                    "by_symbol": {"A": True},
                },
                "gate_symbol": {
                    "scope": "symbol",
                    "active": None,
                    "by_symbol": {"A": True},
                },
            },
        },
    ]
    equity = [
        {"timestamp": "t0", "equity": 100.0},
        {"timestamp": "t1", "equity": 102.0},
    ]
    positions = [
        {"timestamp": "t0", "positions": {}},
        {
            "timestamp": "t1",
            "positions": {"A": {"side": "long", "size": 2.0, "avg_price": 10.0}},
        },
    ]
    diagnostics = summarize_gate_diagnostics(runs, equity, positions)
    portfolio = diagnostics["gate_portfolio"]
    assert portfolio["activation_transitions"] == 1
    assert portfolio["states"]["inactive"]["pnl_usd"] == pytest.approx(2.0)
    assert portfolio["states"]["active"]["max_gross_notional_usd"] == 20.0
    assert diagnostics["gate_symbol"]["pnl_attribution"] is None


def test_funding_events_align_to_completed_bars_and_round_trip() -> None:
    bars = CompletedBarsView.from_rows(_rows())
    funding = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-01-01T00:30:00Z"),
                "symbol": "A",
                "value": 0.001,
            },
            {
                "timestamp": pd.Timestamp("2026-01-01T00:30:00Z"),
                "symbol": "A",
                "value": 0.001,
            },
            {
                "timestamp": pd.Timestamp("2025-12-31T23:00:00Z"),
                "symbol": "A",
                "value": 0.002,
            },
        ]
    )
    events = _funding_market_events(bars, funding)
    assert len(events) == 1
    assert events[0].timestamp == "2026-01-01T01:00:00+00:00"
    assert events[0].payload == {
        "rate": 0.001,
        "source_timestamp": "2026-01-01T00:30:00+00:00",
    }
    dataset = PreparedExecutionDataset(bars, {"source": "test"}, events)
    rebuilt = PreparedExecutionDataset.from_rows(
        dataset.to_dict()["bars"],
        dataset.to_dict()["metadata"],
        dataset.to_dict()["market_events"],
    )
    assert rebuilt.market_events[0].to_dict() == events[0].to_dict()


@pytest.mark.parametrize(("side", "expected"), (("long", -1.0), ("short", 1.0)))
def test_funding_rate_cashflow_uses_position_direction(
    side: str, expected: float
) -> None:
    state = EngineState()
    state.ledger.positions["A"] = PositionRecord(
        symbol="A", side=side, size=2.0, avg_price=90.0
    )
    result = TickResult()
    trace = ExecutionTrace(execution_spec={})
    _apply_market_event(
        MarketEvent(
            kind="funding",
            symbol="A",
            timestamp="2026-01-01T01:00:00Z",
            payload={"rate": 0.005, "source_timestamp": "source"},
        ),
        state=state,
        trace=trace,
        result=result,
        timestamp="2026-01-01T01:00:00Z",
        reference_prices={"A": 100.0},
    )
    assert state.ledger.realized_pnl == pytest.approx(expected)
    assert result.guard_events[0]["reference_price"] == 100.0
    assert result.guard_events[0]["source_timestamp"] == "source"


def test_robustness_contract_validates_and_reuses_exact_artifacts() -> None:
    plan = {
        "neighbors": {"threshold": [0.05, 0.1, 0.15]},
        "phase": {"param": "offset", "values": [0, 1]},
        "leverage": [1, 2],
        "walk_forward": {"train_bars": 20, "test_bars": 5, "folds": 2},
        "scenarios": [{"name": "recent", "lookback_days": 7, "role": "development"}],
    }
    assert validate_robustness_plan(plan) == plan
    with pytest.raises(ValueError, match="unknown robustness plan keys"):
        validate_robustness_plan({"mystery": True})
    artifact = {
        "status": "complete",
        "research_contract_version": RESEARCH_CONTRACT_VERSION,
        "candidate_revision": "rev",
        "dataset_hash": "data",
        "plan_hash": "plan",
    }
    assert _matches(artifact, "rev", "data", "plan") is True
    assert _matches(artifact, "rev", "changed", "plan") is False


def test_robustness_job_persists_and_reuses_revision_bound_report(
    tmp_path: Path,
) -> None:
    from wayfinder_paths.tests.test_jobs_gating import _make_job

    store, job_id, root = _make_job(tmp_path)
    first = robustness_check_job(job_id, robustness_plan={}, store=store)
    assert first["status"] == "partial"
    assert first["advisory"] is True
    assert {row["code"] for row in first["warnings"]} == {"funding_incomplete"}
    assert (root / first["artifact"]).exists()
    assert (root / "results/research/robustness/latest.json").exists()

    second = robustness_check_job(job_id, robustness_plan={}, store=store)
    assert second["reused"] is True
    assert second["candidate_revision"] == first["candidate_revision"]
    assert second["dataset_hash"] == first["dataset_hash"]


def test_robustness_warnings_cover_unobserved_gates_and_funding() -> None:
    report = {
        "data_completeness": {"status": "incomplete"},
        "lanes": {
            "subject": {
                "net_return": 0.1,
                "gate_diagnostics": {
                    "gate_regime": {"scope": "portfolio", "active_bars": 0}
                },
            },
            "phase": {
                "runs": [
                    {"stats": {"net_return": -0.01}},
                    {"stats": {"net_return": 0.02}},
                ]
            },
            "walk_forward": {
                "folds": [{"status": "ok", "test_stats": {"net_return": -0.02}}]
            },
            "scenarios": [{"name": "run_up", "stats": {"net_return": -0.03}}],
        },
        "plan": {},
    }
    codes = {row["code"] for row in _warning_codes(report, {})}
    assert {
        "funding_incomplete",
        "gate_unobserved",
        "phase_sensitivity",
        "oos_decay",
        "scenario_loss",
    } <= codes


def test_research_context_files_stay_small_and_versioned() -> None:
    root = Path(__file__).parents[2]
    skill = root / ".claude/skills/developing-jobs-v1-strategies/SKILL.md"
    contract = root / "wayfinder_paths/jobs/prompts/research_contract.md"
    priors = root / "wayfinder_paths/jobs/prompts/research_priors.md"
    assert skill.stat().st_size <= 6_000
    assert contract.stat().st_size <= 4_000
    assert priors.stat().st_size <= 6_000
    for path in (
        skill,
        contract,
        root / ".opencode/agents/wayfinder-strategy-lab.md",
        root / ".opencode/agents/wayfinder-job-worker.md",
        root / ".opencode/agents/wayfinder-quant.md",
        root / ".opencode/agents/wayfinder-research.md",
        root / ".opencode/plugins/wayfinder-compaction.ts",
    ):
        assert RESEARCH_CONTRACT_VERSION in path.read_text(encoding="utf-8")
    assert "rules/factor-research.md" in skill.read_text(encoding="utf-8")
    assert "factor_scan" in contract.read_text(encoding="utf-8")


def test_worker_stable_prefix_loads_research_contract_once(tmp_path: Path) -> None:
    from wayfinder_paths.jobs.models import WayfinderJob
    from wayfinder_paths.jobs.store import JobStore
    from wayfinder_paths.jobs.worker import prepare_job_worker_prompt

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new("research-contract-prompt", agent_mode="monitor")
    store.save(job)
    sections = prepare_job_worker_prompt(store=store, job_id=job.id, mode="monitor")
    stable = sections["stable_prefix"]
    assert stable.count(f"# {RESEARCH_CONTRACT_VERSION}") == 1
    assert f'"research_contract_version": "{RESEARCH_CONTRACT_VERSION}"' in stable
    assert "do not reload the strategy skill on each wake" in stable
