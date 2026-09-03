from __future__ import annotations

import io
import json
import math
import shutil
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
import yaml

from wayfinder_paths.jobs.archive import (
    behavior_cell,
    evolution_lessons_block,
    load_archive,
    quality_diversity_snapshot,
    record_candidate,
)
from wayfinder_paths.jobs.compute_lock import ComputeLockBusy
from wayfinder_paths.jobs.evolution_campaign import (
    _archive_campaign_candidate,
    _attempt_cap,
    _candidate_handoff,
    _claim_full_dev,
    _commit_designed_attempt,
    _commit_full_dev,
    _complexity_budget,
    _failure_mode_summary,
    _focus_rank,
    _freeze_research_context,
    _gate_summary,
    _isolated_full_dev,
    _load_candidate_search_space,
    _materialize_candidate_seed,
    _min_fills_per_day,
    _neighborhood_dimension,
    _numeric_tunables,
    _parameter_tuning_preview,
    _parent_source,
    _persist_executable_bundle,
    _plateau_select,
    _prune_risky_trials,
    _research_context_instruction,
    _risk_ceiling_scale,
    _same_family_nonwins,
    _screen_confidence,
    _screen_slice_report,
    _screen_slices,
    _screen_verdict,
    _select_full_dev_candidate,
    _select_parent_plan,
    _select_validated_rows,
    _signal_timeframes,
    _slice_route,
    _starter_compatibility,
    _write_timeseries_prefix,
    campaign_due,
    campaign_prompt_block,
    campaign_status,
    evaluate_candidate,
    evolution_compute_window_open,
    finalize_campaign,
    maybe_start_campaign,
    prepare_candidate,
    recover_lost_candidate_evaluations,
    resolve_candidate_bundle,
    start_campaign,
    submit_campaign_design,
    submit_research_seed,
)
from wayfinder_paths.jobs.execution.op_process import op_runner_command
from wayfinder_paths.jobs.execution.op_runner import _nudge_evolution
from wayfinder_paths.jobs.execution.primitives import DEFAULT_WARMUP_BARS
from wayfinder_paths.jobs.failures import TransientInfrastructureError
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.starter_casebook import (
    MAX_PROMPT_CASES,
    load_starter_casebook,
    select_starter_cases,
)
from wayfinder_paths.jobs.starters import (
    STARTER_DEFINITIONS,
    starter_lookback_bars,
    starter_warmup_bars,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.worker import (
    _queue_evolution_worker,
    nudge_evolution_session,
    prepare_job_worker_prompt,
    recover_evolution_stage_session,
    retire_evolution_session,
    run_job_worker,
)


def _job(tmp_path, job_id: str) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        job_id,
        name="Majors momentum lab",
        goal="find robust momentum and factor strategies",
        script="workspace/src/strategy.py",
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    store.save(job)
    root = store.job_dir(job.id)
    (root / "workspace" / "src" / "strategy.py").write_text(
        "def decide(ctx):\n    return []\n", encoding="utf-8"
    )
    bars = root / "results" / "backtest" / "input_bars.json"
    bars.parent.mkdir(parents=True, exist_ok=True)
    bars.write_text('{"metadata":{"days":120},"bars":[]}\n', encoding="utf-8")
    # Most tests below pin the pre-design schema so they remain compatibility
    # coverage for campaigns that were already active during a roll. New
    # investigation/design behavior has dedicated tests at the end of this file.
    (root / "improver.yaml").write_text(
        yaml.safe_dump(
            {
                "evolution": {
                    "generated_programs": 12,
                    "investigation_design_enabled": False,
                }
            }
        ),
        encoding="utf-8",
    )
    return store, job.id


def _prepare_campaign_candidates(
    store: JobStore, job_id: str, started: datetime
) -> list[dict[str, Any]]:
    return [
        prepare_candidate(
            store,
            job_id,
            family=f"family-{index}",
            summary=f"candidate {index}",
            now=started + timedelta(hours=1),
        )
        for index in range(1, 5)
    ]


def _investigative_job(tmp_path) -> tuple[JobStore, str]:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    (store.job_dir(job_id) / "improver.yaml").write_text(
        yaml.safe_dump(
            {
                "evolution": {
                    "generated_programs": 8,
                    "investigation_design_enabled": True,
                    "max_attempts_per_idea": 3,
                    "max_quick_attempts": 24,
                    "wildcard_slots": 2,
                    # Depth-first compatibility coverage; screen-then-focus
                    # allocation has dedicated tests below.
                    "screen_before_repair": False,
                }
            }
        ),
        encoding="utf-8",
    )
    return store, job_id


def _campaign_design() -> dict[str, Any]:
    hypotheses = [
        {
            "id": f"h{index}",
            "family": f"measured-family-{index}",
            "causal_mechanism": f"repair measured failure {index}",
            "falsifier": f"same-window behavior does not improve for {index}",
            "evidence_refs": ["/baseline/reason"],
        }
        for index in range(1, 4)
    ]
    sources = (
        "starter_seed",
        "research_context",
        "de_novo",
        "incumbent",
        "starter_seed",
        "de_novo",
        "de_novo",
        "starter_seed",
    )
    slots = []
    for index, source in enumerate(sources, start=1):
        wildcard = index >= 7
        slots.append(
            {
                "slot_id": f"s{index:02d}",
                "wildcard": wildcard,
                "hypothesis_id": None if wildcard else f"h{((index - 1) % 3) + 1}",
                "parent_source": source,
                "mutation_kind": "parameter" if index in {4, 5} else "structural",
                "family": f"idea-{index}",
                "summary": f"test mechanism {index}",
            }
        )
    return {"hypotheses": hypotheses, "slots": slots}


def _regime_bars(count: int = 560) -> list[dict[str, Any]]:
    started = datetime(2026, 7, 1, tzinfo=UTC)
    rows: list[dict[str, Any]] = []
    for index in range(count):
        for symbol_index, symbol in enumerate(("SOL", "XRP", "POL", "HYPE")):
            close = 100.0 + symbol_index * 10 + index * 0.05
            rows.append(
                {
                    "timestamp": (started + timedelta(minutes=5 * index)).isoformat(),
                    "symbol": symbol,
                    "open": close - 0.02,
                    "high": close + 0.1,
                    "low": close - 0.1,
                    "close": close,
                    "volume": 1_000.0,
                }
            )
    return rows


def test_rollout_is_gated_and_campaign_context_is_bounded(tmp_path) -> None:
    other_store, other_id = _job(tmp_path / "other", "other-job")
    with pytest.raises(ValueError, match="disabled_or_excluded"):
        start_campaign(
            other_store,
            other_id,
            now=datetime(2026, 8, 25, 12, tzinfo=UTC),
            force=True,
        )

    store, job_id = _job(tmp_path / "target", "majors-5m-lab")
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    state = start_campaign(store, job_id, now=now)
    assert state["status"] == "active"
    assert state["counts"]["generated"] == 0
    assert state["budgets"] == {
        "generated": 12,
        "full_development": 4,
        "optuna": 2,
        "optuna_minimum": 1,
        "finalist_gate": 1,
    }
    assert campaign_status(store, job_id)["campaign_id"] == state["campaign_id"]
    root = store.job_dir(job_id)
    manifest_path = root / state["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    snapshot = manifest_path.parent / manifest["dataset"]["path"]
    original = snapshot.read_text(encoding="utf-8")
    (root / "results" / "backtest" / "input_bars.json").write_text(
        "[]\n", encoding="utf-8"
    )
    assert snapshot.read_text(encoding="utf-8") == original
    assert manifest["forward_context_cutoff"] == now.isoformat()
    block = campaign_prompt_block(store, job_id, now=now)
    assert block is not None
    assert block["job_id"] == job_id
    for required in (
        "wayfinder_core_jobs",
        'action="evolution_prepare"',
        f'job_id="{job_id}"',
        "family=",
        "summary=",
        "Do not pass mutation_kind",
    ):
        assert required in block["next_action"]
    assert 'mutation_kind="structural"' not in block["next_action"]
    assert block["historical_lessons"]["outcomes"] == []
    assert len(block["cases"]) <= MAX_PROMPT_CASES
    assert block["constraints"] == {
        "paper_only": True,
        "live_requires_owner": True,
        "candidate_inputs_frozen_at_campaign_start": True,
        "finalist_requires_24h_operational_burn_in": True,
        "starter_seed_evidence_resets": True,
        "refuted_family_matching": "exact_free_form_family_v1",
    }
    assert len(load_starter_casebook()) > len(block["cases"])


def test_investigative_campaign_requires_and_freezes_one_design_turn(tmp_path) -> None:
    store, job_id = _investigative_job(tmp_path)
    started = datetime(2099, 8, 25, 12, tzinfo=UTC)

    state = start_campaign(store, job_id, now=started)

    assert state["schema_version"] == "2.0"
    assert state["stage"] == "design"
    prompt = campaign_prompt_block(store, job_id, now=started + timedelta(minutes=1))
    assert prompt and prompt["agent_name"] == "wayfinder-evolution-designer"
    assert prompt["artifact_key"] == "design"
    assert "/baseline/reason" in prompt["valid_evidence_pointers"]
    assert (
        "/research_context/validated_positives" not in prompt["valid_evidence_pointers"]
    )
    assert prompt["constraints"]["idea_slots"] == 8
    assert prompt["constraints"]["wildcards"] == 2
    assert prompt["constraints"]["research_parent_required"] is False
    assert prompt["constraints"]["cost_budget"] is None
    assert (
        "Do not allocate a decorative research_seed/research_context"
        in prompt["next_action"]
    )
    assert (
        "parent_source must be exactly one of incumbent, qd_elite, crossover, "
        "de_novo, starter_seed, research_seed, research_context"
        in prompt["next_action"]
    )
    assert "it is an enum" in prompt["next_action"]
    assert (
        "mutation_kind must be exactly structural or parameter" in prompt["next_action"]
    )
    with pytest.raises(ValueError, match="design must be accepted"):
        prepare_candidate(store, job_id, now=started + timedelta(minutes=2))

    design = _campaign_design()
    malformed = json.loads(json.dumps(design))
    malformed["hypotheses"][0]["evidence_refs"] = "/baseline/reason"
    with pytest.raises(ValueError, match="non-empty list of JSON pointers"):
        submit_campaign_design(store, job_id, campaign_design=malformed)
    malformed = json.loads(json.dumps(design))
    malformed["slots"][0]["wildcard"] = "false"
    with pytest.raises(ValueError, match="wildcard must be true or false"):
        submit_campaign_design(store, job_id, campaign_design=malformed)
    malformed = json.loads(json.dumps(design))
    malformed["slots"][0]["starter_seed_id"] = "missing-starter"
    with pytest.raises(ValueError, match="unavailable starter_seed_id"):
        submit_campaign_design(store, job_id, campaign_design=malformed)

    manifest = store.read_json(job_id, str(state["manifest"]))
    selected_starter = manifest["starter_seeds"][3]["starter_id"]
    design["slots"][0]["starter_seed_id"] = selected_starter

    accepted = submit_campaign_design(store, job_id, campaign_design=design)
    repeated = submit_campaign_design(store, job_id, campaign_design=design)
    current = campaign_status(store, job_id)

    assert accepted == repeated
    assert current["stage"] == "generate"
    assert current["design"]["hypotheses"] == 3
    assert current["design"]["slots"] == 8
    assert current["design"]["wildcards"] == 2
    assert "starter_seed_id" not in accepted["slots"][1]
    assert "research_seed_id" not in accepted["slots"][1]
    candidate = prepare_candidate(store, job_id, now=started + timedelta(minutes=3))
    assert candidate["design_slot_id"] == "s01"
    assert candidate["hypothesis_id"] == "h1"
    assert candidate["parent_source"] == "starter_seed"
    assert candidate["starter_seed_id"] == selected_starter
    assert candidate["evidence_refs"] == ["/baseline/reason"]
    assert candidate["reference_bundle"]


def _screen_outcome(
    candidate_root, *, net: float, primary: str = "negative_after_costs", **extra
) -> dict[str, Any]:
    revision = compute_workspace_revision(candidate_root)
    codes = [primary] + list(extra.pop("codes", []))
    postmortem = {
        "viable": False,
        "primary_failure": primary,
        "failure_codes": codes,
        "behavior_diff": {"material_change": True},
        **extra,
    }
    return {
        "status": "low_fidelity_rejected",
        "evidence": "attempt receipt",
        "revision": revision,
        "quick": {"stats": {"trade_count": 40, "net_return": net}},
        "objective": {
            "net_log_growth": net,
            "downside_deviation": 0.0,
            "tail_loss": 0.0,
            "max_drawdown_pct": 0.0,
        },
        "attempt_receipt": {
            "revision": revision,
            "execution_valid": True,
            "stats": {"trade_count": 40, "net_return": net},
            "objective": {"net_log_growth": net},
            "behavior": {},
            "trades": [],
        },
        "postmortem": postmortem,
    }


def test_screen_then_focus_allocation_serves_ranked_candidates(tmp_path) -> None:
    store, job_id = _investigative_job(tmp_path)
    improver_path = store.job_dir(job_id) / "improver.yaml"
    improver = yaml.safe_load(improver_path.read_text(encoding="utf-8"))
    improver["evolution"].update(
        {
            "screen_before_repair": True,
            "focus_candidates": 1,
            "focus_attempts_per_candidate": 3,
        }
    )
    improver_path.write_text(yaml.safe_dump(improver), encoding="utf-8")
    started = datetime(2099, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    submit_campaign_design(store, job_id, campaign_design=_campaign_design())

    def commit(index: int, **kwargs: Any) -> dict[str, Any]:
        state = campaign_status(store, job_id)
        candidate = state["candidates"][index - 1]
        root = store.job_dir(job_id) / candidate["bundle"]
        _commit_designed_attempt(
            store,
            job_id,
            state=state,
            candidate=candidate,
            outcome=_screen_outcome(root, **kwargs),
        )
        store.write_json(job_id, "state/evolution_campaign.json", state)
        return candidate

    prepare_candidate(store, job_id, now=started + timedelta(minutes=1))
    first = commit(1, net=-0.01)
    assert first["status"] == "repair_pending"
    block = campaign_prompt_block(store, job_id, now=started + timedelta(minutes=2))
    # Screen phase: the next slot is prepared before any repair is served.
    assert block and block["artifact_key"] == "candidate-02-attempt-01"
    assert block["focus"]["phase"] == "screen"

    for slot in range(2, 9):
        prepare_candidate(store, job_id, now=started + timedelta(minutes=slot))
        if slot == 3:
            commit(
                slot,
                net=-0.5,
                primary="cost_bleed",
                codes=["negative_after_costs", "fees_erased_edge"],
                economics={
                    "candidate": {"fills_per_day": 59.0, "fee_pct_of_capital": 0.29},
                    "incumbent": {"fills_per_day": 2.7},
                },
            )
        else:
            commit(slot, net=-0.01 * slot)
    state = campaign_status(store, job_id)
    assert [item["status"] for item in state["candidates"]] == ["repair_pending"] * 8
    assert state["counts"]["quick_attempts"] == 8 and state["counts"]["repairs"] == 0

    # Focus phase: the fixable cost-bleed churner outranks the mildly negative
    # siblings, and only it is served.
    block = campaign_prompt_block(store, job_id, now=started + timedelta(minutes=20))
    assert block and block["artifact_key"] == "candidate-03-attempt-02"
    focus_id = state["candidates"][2]["candidate_id"]
    assert block["focus"] == {
        "phase": "focus",
        "candidate_ids": [focus_id],
        "computed_at": state["focus"]["computed_at"],
    }
    assert block["repair_work_order"]["primary_failure"] == "cost_bleed"
    assert "59.0 fills/day vs incumbent 2.7" in block["next_action"]
    assert "at most 3" in block["next_action"]
    policy = store.read_json(job_id, str(state["manifest"]))["policy"]
    assert _attempt_cap(state, state["candidates"][2], policy) == 3
    assert _attempt_cap(state, state["candidates"][7], policy) == 1
    outcome = next(
        item for item in block["candidate_outcomes"] if item["candidate_id"] == focus_id
    )
    assert outcome["trajectory"] == [
        {
            "attempt": 1,
            "primary_failure": "cost_bleed",
            "net_return": -0.5,
            "fills_per_day": 59.0,
            "fee_pct_of_capital": 0.29,
        }
    ]

    # A repair without causal progress closes the candidate; the budget moves
    # to the next-ranked parked candidate instead of a fresh free attempt.
    closed = commit(3, net=-0.5, primary="cost_bleed")
    assert closed["status"] == "low_fidelity_rejected"
    state = campaign_status(store, job_id)
    assert state["counts"]["repairs"] == 1
    block = campaign_prompt_block(store, job_id, now=started + timedelta(minutes=30))
    assert block and block["artifact_key"] == "candidate-01-attempt-02"
    assert state["candidates"][7]["status"] == "repair_pending"


def test_design_prompt_carries_cost_budget_from_baseline(tmp_path) -> None:
    store, job_id = _investigative_job(tmp_path)
    baseline = store.job_dir(job_id) / "results" / "backtest" / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "scope": {
                    "bars": 28_513,
                    "start": "2026-05-03T15:25:00+00:00",
                    "end": "2026-08-10T15:25:00+00:00",
                },
                "result": {
                    "params": {
                        "initial_capital": 100.0,
                        "fee_bps": 4.5,
                        "slippage_bps": 3.5,
                    },
                    "stats": {
                        "net_return": 0.148,
                        "trade_count": 271,
                        "total_fees": 11.22,
                        "total_turnover_usd": 22_441.0,
                        "exposure_pct": 0.106,
                        "avg_trade_duration_s": 7_160.0,
                        "max_drawdown_pct": -0.065,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    started = datetime(2099, 8, 25, 12, tzinfo=UTC)
    state = start_campaign(store, job_id, now=started)

    prompt = campaign_prompt_block(store, job_id, now=started + timedelta(minutes=1))
    assert prompt
    risk = prompt["constraints"]["risk_budget"]
    assert risk["max_drawdown_pct"] == 0.25 and risk["max_tail_loss"] == 0.15
    assert risk["incumbent_max_drawdown_pct"] == pytest.approx(0.065)
    assert risk["prior_risk_ceiling"] == []
    assert "Risk budget: OOS max drawdown <= 25%" in prompt["next_action"]
    assert "the incumbent runs at" in prompt["next_action"]
    assert "never a search dimension" in prompt["next_action"]
    budget = prompt["constraints"]["cost_budget"]
    assert budget["incumbent_fills_per_day"] == pytest.approx(2.74, abs=0.01)
    assert budget["max_fills_per_day"] == pytest.approx(8.2, abs=0.05)
    assert budget["round_trip_cost_bps"] == 16.0
    assert "Cost budget: the incumbent trades 2.7 fills/day" in prompt["next_action"]
    # The fixture freezes no bars, so the cadence floor has no window to use.
    assert "min_fills_per_day" not in budget
    pack = store.read_json(job_id, str(state["diagnostic_pack"]))
    assert pack["baseline"]["economics"]["fee_pct_of_capital"] == pytest.approx(0.1122)
    assert "/baseline/economics/fills_per_day" in prompt["valid_evidence_pointers"]


def test_screen_slices_are_disjoint_and_fall_back_when_short() -> None:
    from wayfinder_paths.jobs.execution.simulator import PreparedExecutionDataset

    def dataset(count: int) -> PreparedExecutionDataset:
        rows = []
        for index in range(count):
            stamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index)
            rows.append(
                {
                    "timestamp": stamp.isoformat(),
                    "symbol": "IMX",
                    "open": 10.0,
                    "high": 10.1,
                    "low": 9.9,
                    "close": 10.0,
                    "volume": 1.0,
                }
            )
        return PreparedExecutionDataset.from_rows(rows, {})

    slices = _screen_slices(dataset(15_000), bars=10_000)
    assert [label for label, _ in slices] == ["recent", "earlier"]
    recent = slices[0][1].bars.timestamps
    earlier = slices[1][1].bars.timestamps
    assert len(recent) == 10_000 and len(earlier) == 5_000
    assert earlier[-1] < recent[0]
    assert [label for label, _ in _screen_slices(dataset(11_000), bars=10_000)] == [
        "recent"
    ]
    assert [label for label, _ in _screen_slices(dataset(60), bars=10_000)] == [
        "recent"
    ]


def test_screen_verdict_names_regime_dependence_and_noise_fit() -> None:
    assert _screen_confidence(1) == 0.7
    assert _screen_confidence(3) == 0.9
    assert _screen_confidence(9) == 0.9

    both = {
        "recent": {"net_return": 0.03, "trade_count": 30, "lcb": 0.004},
        "earlier": {"net_return": 0.02, "trade_count": 28, "lcb": 0.001},
    }
    assert _screen_verdict(both, min_trades=12)["passed"] is True

    mixed = {**both, "earlier": {"net_return": -0.02, "trade_count": 28, "lcb": -0.01}}
    verdict = _screen_verdict(mixed, min_trades=12)
    assert verdict["passed"] is False and verdict["code"] == "screen_regime_dependent"

    noisy = {**both, "recent": {"net_return": 0.03, "trade_count": 30, "lcb": -0.002}}
    assert (
        _screen_verdict(noisy, min_trades=12)["code"] == "screen_edge_not_significant"
    )

    sparse = {**both, "earlier": {"net_return": 0.02, "trade_count": 5, "lcb": 0.001}}
    assert _screen_verdict(sparse, min_trades=12)["code"] == "activity_below_floor"

    # Negative everywhere is plain failure: the postmortem's own code stands.
    losing = {
        "recent": {"net_return": -0.01, "trade_count": 30, "lcb": None},
        "earlier": {"net_return": -0.02, "trade_count": 28, "lcb": None},
    }
    verdict = _screen_verdict(losing, min_trades=12)
    assert verdict["passed"] is False and verdict["code"] is None
    # Too few days for a bootstrap: a positive point estimate still passes.
    tiny = {"recent": {"net_return": 0.01, "trade_count": 3, "lcb": None}}
    assert _screen_verdict(tiny, min_trades=1)["passed"] is True


def test_screen_report_splits_the_reference_losing_days() -> None:
    from types import SimpleNamespace

    days = [
        datetime(2026, 6, 1, tzinfo=UTC) + timedelta(days=index) for index in range(12)
    ]
    # Reference loses on days 3-6 and earns elsewhere; the candidate repairs
    # exactly those days and matches the reference on the rest.
    reference_returns = [
        0.01,
        0.01,
        -0.02,
        -0.02,
        -0.02,
        -0.02,
        0.01,
        0.01,
        0.01,
        0.01,
        0.01,
    ]
    candidate_returns = [
        0.01,
        0.01,
        0.005,
        0.005,
        0.005,
        0.005,
        0.01,
        0.01,
        0.01,
        0.01,
        0.01,
    ]

    def curve(returns):
        equity, rows = 100.0, [{"timestamp": days[0].isoformat(), "equity": 100.0}]
        for stamp, value in zip(days[1:], returns, strict=True):
            equity *= 1 + value
            rows.append({"timestamp": stamp.isoformat(), "equity": equity})
        return rows

    from wayfinder_paths.jobs.economics import daily_log_returns

    reference_daily = daily_log_returns(curve(reference_returns))
    result = SimpleNamespace(
        equity_curve=curve(candidate_returns),
        stats={"net_return": 0.06, "trade_count": 30},
    )
    report = _screen_slice_report(result, reference_daily, confidence=0.7)
    mode = report["failure_mode"]
    assert mode["losing_days"] == 4 and mode["winning_days"] == 7
    assert mode["losing_delta"] > 0.09 and abs(mode["winning_delta"]) < 1e-9
    assert _slice_route(report) in {"failure_mode", "global"}
    verdict = _screen_verdict({"recent": report, "earlier": report}, min_trades=12)
    assert verdict["passed"] is True
    assert verdict["slices"]["recent"]["route"] in {"failure_mode", "global"}

    # Repairing the losing days at the cost of the winning days is not a repair.
    hurt = {**report, "lcb": -0.01, "failure_mode": {**mode, "winning_delta": -0.02}}
    assert _slice_route(hurt) is None
    assert _screen_verdict({"recent": hurt}, min_trades=12)["code"] == (
        "screen_edge_not_significant"
    )


def test_failure_mode_summary_names_worst_days_and_regime() -> None:
    daily = [
        ("2026-06-01", 0.01),
        ("2026-06-02", -0.03),
        ("2026-06-03", -0.01),
        ("2026-06-04", 0.02),
        ("2026-06-05", -0.005),
    ]
    labels = {
        "2026-06-01": "up_lowvol",
        "2026-06-02": "down_highvol",
        "2026-06-03": "down_highvol",
        "2026-06-04": "up_lowvol",
    }
    summary = _failure_mode_summary(daily, labels)
    assert summary["days"] == 5 and summary["losing_days"] == 3
    assert summary["worst_days"][0] == {
        "day": "2026-06-02",
        "return": -0.03,
        "regime": "down_highvol",
    }
    assert summary["worst_regime"] == "down_highvol"
    assert summary["by_regime"]["mixed"]["days"] == 1
    assert summary["losing_return"] == pytest.approx(math.exp(-0.045) - 1, abs=1e-6)


def test_strategy_complexity_budget_scales_with_the_incumbent() -> None:
    from wayfinder_paths.jobs.evolution_campaign import strategy_complexity

    source = (
        "def decide(ctx):\n"
        "    if ctx.rsi < 30 and ctx.atr > 0.5:\n"
        "        return [1]\n"
        "    if ctx.close >= ctx.sma:\n"
        "        return []\n"
        "    return [0.25]\n"
    )
    size = strategy_complexity(source)
    assert size["comparisons"] == 3
    assert size["numeric_literals"] == 4
    assert _complexity_budget({}, {"comparisons": 32}) == 48
    assert _complexity_budget({}, {"comparisons": 4}) == 24
    assert _complexity_budget({"complexity_multiple": 2.0}, {"comparisons": 20}) == 40
    assert strategy_complexity("def broken(:")["comparisons"] == 0


def test_plateau_selection_prefers_a_robust_neighborhood_over_a_lone_peak() -> None:
    from types import SimpleNamespace

    runs = [
        {"trial": 0, "params": {"lookback": 20}, "net_return": 0.10},
        {"trial": 1, "params": {"lookback": 22}, "net_return": 0.02},
        {"trial": 2, "params": {"lookback": 18}, "net_return": 0.01},
        {"trial": 3, "params": {"lookback": 60}, "net_return": 0.06},
        {"trial": 4, "params": {"lookback": 63}, "net_return": 0.058},
        {"trial": 5, "params": {"lookback": 57}, "net_return": 0.062},
    ]
    grid = SimpleNamespace(runs=runs, ranked=[runs[0]], rank_by="net_return")
    selected, plateau = _plateau_select(grid, ["lookback"], "net_return")
    assert selected["trial"] == 3 and plateau["selected_by"] == "plateau"
    assert plateau["neighbors_of_best"] == 2 and plateau["ratio"] < 0.5

    runs[1]["net_return"], runs[2]["net_return"] = 0.09, 0.08
    selected, plateau = _plateau_select(grid, ["lookback"], "net_return")
    assert selected["trial"] == 0 and plateau["selected_by"] == "peak"
    bare = SimpleNamespace(ranked=[{"params": {"lookback": 30}}])
    selected, plateau = _plateau_select(bare, ["lookback"], "net_return")
    assert (
        selected == {"params": {"lookback": 30}} and plateau["selected_by"] == "ranked"
    )


def test_incumbent_neighborhood_tunables_and_dimensions() -> None:
    tunables = _numeric_tunables(
        {"stop_pct": 0.03, "hold_bars": 24, "enabled": True, "zero": 0},
        {"fee_bps": 4.5, "symbols": ["A"], "warmup_bars": 400, "leverage": 3.0},
    )
    assert tunables == {"hold_bars": 24.0, "stop_pct": 0.03}
    assert _neighborhood_dimension(24.0, 0.3) == {"type": "int", "low": 16, "high": 32}
    assert _neighborhood_dimension(0.03, 0.3) == {
        "type": "float",
        "low": 0.021,
        "high": 0.039,
    }


def test_validated_signal_selection_requires_density_and_slice_agreement() -> None:
    assert _signal_timeframes(300) == ["300s", "15m", "1h", "4h"]
    assert _signal_timeframes(3600) == ["3600s", "4h"]

    def row(symbol, signal, t, *, n, t_net=1.5, fold_stable=True, edge=8.0):
        return {
            "symbol": symbol,
            "signal": signal,
            "timeframe": "1h",
            "horizon": 4,
            "t_stat_vs_drift": t,
            "direction": "short" if t <= -2 else "long" if t >= 2 else None,
            "t_net": t_net,
            "q_value": 0.7,
            "verdict": "candidate",
            "fold_stable": fold_stable,
            "folds_agreeing": 3,
            "edge_net_bps": edge,
            "n": n,
            "family": "breakout",
            "description": "d",
        }

    full = [
        row("HYPE", "dense", 3.1, n=60),
        row("HYPE", "sparse", 3.4, n=12),
        row("SOL", "flips", 2.8, n=50),
        row("POL", "short_side", -2.6, n=50, t_net=1.9),
        row("SOL", "weak", 1.4, n=50),
        row("SOL", "unstable", 2.9, n=50, fold_stable=False),
        row("SOL", "costly", 2.7, n=50, edge=-1.0),
        row("SOL", "marginal", 2.7, n=50, t_net=0.4),
    ]
    slices = {
        "recent": {
            ("HYPE", "dense", "1h", 4): {"t_stat_vs_drift": 1.4},
            ("HYPE", "sparse", "1h", 4): {"t_stat_vs_drift": 1.6},
            ("SOL", "flips", "1h", 4): {"t_stat_vs_drift": 1.9},
            ("POL", "short_side", "1h", 4): {"t_stat_vs_drift": -2.1},
        },
        "earlier": {
            ("HYPE", "dense", "1h", 4): {"t_stat_vs_drift": 1.1},
            ("HYPE", "sparse", "1h", 4): {"t_stat_vs_drift": 1.2},
            ("SOL", "flips", "1h", 4): {"t_stat_vs_drift": -1.5},
            ("POL", "short_side", "1h", 4): {"t_stat_vs_drift": -1.3},
        },
    }
    selected, near = _select_validated_rows(
        full, slices, days=100.0, min_events_per_day=0.3, slice_min_t=1.0, min_t_net=1.0
    )
    # Ranked by the cost-adjusted t, and the short side keeps its direction.
    assert [(item["signal"], item["direction"]) for item in selected] == [
        ("short_side", "short"),
        ("dense", "long"),
    ]
    assert selected[1]["events_per_day"] == 0.6
    assert {item["signal"]: item["shortfall"] for item in near} == {
        "sparse": "sparse",
        "flips": "slice_disagreement",
    }


def test_library_signal_aligns_causally_to_base_bars() -> None:
    from wayfinder_paths.jobs.research import (
        library_signal_on_bars,
        library_signal_warmup_bars,
        resample_ohlcv,
    )
    from wayfinder_paths.jobs.signal_library import build_signal_frame, signal_defs

    start = datetime(2026, 6, 1, tzinfo=UTC)
    rows = []
    price = 100.0
    for index in range(600):
        price *= 1 + ((index * 7919) % 11 - 5) / 1_000
        rows.append(
            {
                "timestamp": (start + timedelta(minutes=5 * (index + 1))).isoformat(),
                "symbol": "HYPE",
                "open": price,
                "high": price * 1.001,
                "low": price * 0.999,
                "close": price,
                "volume": 10.0,
            }
        )
    frame = pd.DataFrame(rows)
    signal = "new_low_5"
    aligned = library_signal_on_bars(frame, signal, "1h", bar_seconds=300)
    assert aligned.dtype == bool and len(aligned) == 600
    hourly = resample_ohlcv(frame, 3600, bar_seconds=300)
    spec = signal_defs()[signal]
    hourly_signal = build_signal_frame(
        hourly, include_canonical=False, canonical_signals=(spec,)
    )[signal]
    hourly_stamps = pd.to_datetime(hourly["timestamp"], utc=True)
    base_stamps = pd.to_datetime(frame["timestamp"], utc=True)
    for index in (120, 300, 599):
        completed = hourly_stamps <= base_stamps.iloc[index]
        expected = bool(hourly_signal[completed].iloc[-1]) if completed.any() else False
        assert bool(aligned.iloc[index]) == expected
    assert library_signal_warmup_bars(signal, "1h", bar_seconds=300) == (7 + 2) * 12


def test_cadence_floor_follows_the_elite_participation_floor() -> None:
    policy = {
        "split": {"train": 0.8, "validation": 0.2},
        "elite_min_validation_trades": 8,
    }
    assert _min_fills_per_day(policy, {"days": 99.0}) == pytest.approx(0.404)
    assert _min_fills_per_day(policy, {}) is None
    assert _min_fills_per_day({"split": {}}, {"days": 99.0}) is None


def test_candidate_search_space_accepts_shorthand_and_names_untyped_keys(
    tmp_path,
) -> None:
    root = tmp_path / "candidate"
    root.mkdir()
    (root / "search_space.json").write_text(
        json.dumps({"lookback": [12, 96], "threshold": {"low": 0.5, "high": 5.0}}),
        encoding="utf-8",
    )
    loaded = _load_candidate_search_space(root, required=True)
    assert loaded == {
        "lookback": {"type": "int", "low": 12, "high": 96},
        "threshold": {"type": "float", "low": 0.5, "high": 5.0},
    }

    (root / "search_space.json").write_text(
        json.dumps({"lookback": [1, 2, 3], "mode": "fast"}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match=r"keys \['lookback'\]"):
        _load_candidate_search_space(root, required=True)


def test_starter_seeds_are_stamped_with_universe_compatibility(tmp_path) -> None:
    store, job_id = _investigative_job(tmp_path)
    job_path = store.job_dir(job_id) / "job.yaml"
    job_data = yaml.safe_load(job_path.read_text(encoding="utf-8"))
    job_data["execution_params"] = {"symbols": ["ETH", "SOL", "HYPE"], "fee_bps": 4.5}
    job_path.write_text(yaml.safe_dump(job_data, sort_keys=False), encoding="utf-8")
    started = datetime(2099, 8, 25, 12, tzinfo=UTC)
    state = start_campaign(store, job_id, now=started)
    manifest = store.read_json(job_id, str(state["manifest"]))
    starters = {item["starter_id"]: item for item in manifest["starter_seeds"]}

    # The pair starter adapts to any two target symbols, so only the count
    # blocks it here; the sleeve starter embeds symbol literals in params.
    pair = starters["btc-eth-relative-strength-1d"]
    assert pair["compatible"] is False
    assert pair["missing_symbols"] == []
    assert "exactly two symbols" in pair["incompatibility_reason"]
    sleeves = starters["mixed-sleeve-momentum-15m"]
    assert sleeves["compatible"] is False and "xyz:COIN" in sleeves["missing_symbols"]
    # The pilot's failure shape: no target symbols, so the starter's own list
    # survives install and must fit the frozen universe.
    pair_definition = next(
        item
        for item in STARTER_DEFINITIONS
        if item.id == "btc-eth-relative-strength-1d"
    )
    unoverridden = _starter_compatibility(
        pair_definition,
        {"HYPE", "SOL", "XRP", "POL"},
        target_overrides_symbols=False,
    )
    assert unoverridden["compatible"] is False
    assert unoverridden["missing_symbols"] == ["BTC", "ETH"]
    compatible_ids = [
        starter_id for starter_id, item in starters.items() if item["compatible"]
    ]
    assert compatible_ids

    prompt = campaign_prompt_block(store, job_id, now=started + timedelta(minutes=1))
    assert prompt and "btc-eth-relative-strength-1d" not in prompt["next_action"]
    plan = _select_parent_plan(
        manifest, requested_source="starter_seed", slot=1, candidates=[]
    )
    assert plan["source"] == "starter_seed"
    assert plan["starter"]["compatible"] is True

    design = _campaign_design()
    design["slots"][0]["starter_seed_id"] = "mixed-sleeve-momentum-15m"
    with pytest.raises(ValueError, match="requires symbols"):
        submit_campaign_design(store, job_id, campaign_design=design)

    with pytest.raises(ValueError, match="requires symbols"):
        _materialize_candidate_seed(
            store,
            job_id,
            campaign_id=state["campaign_id"],
            candidate_root=tmp_path / "never-created",
            plan={"source": "starter_seed", "starter": sleeves},
        )
    assert not (tmp_path / "never-created").exists()


def test_design_prompt_offers_validated_signals_when_seeding_is_on(tmp_path) -> None:
    store, job_id = _investigative_job(tmp_path)
    improver_path = store.job_dir(job_id) / "improver.yaml"
    improver = yaml.safe_load(improver_path.read_text(encoding="utf-8"))
    improver["evolution"]["signal_first_seeding"] = True
    improver_path.write_text(yaml.safe_dump(improver), encoding="utf-8")
    started = datetime(2099, 8, 25, 12, tzinfo=UTC)
    state = start_campaign(store, job_id, now=started)
    pack_path = str(state["diagnostic_pack"])
    pack = store.read_json(job_id, pack_path)
    # The fixture freezes no bars: the scan is unavailable and the prompt is
    # silent about signals.
    assert pack["validated_signals"]["available"] is False
    prompt = campaign_prompt_block(store, job_id, now=started + timedelta(minutes=1))
    assert prompt and prompt["constraints"]["validated_signals"] is None
    assert "Validated signals" not in prompt["next_action"]

    pack["validated_signals"] = {
        "available": True,
        "signals": [
            {
                "symbol": "HYPE",
                "signal": "new_low_5",
                "timeframe": "1h",
                "horizon": 4,
                "direction": "long",
                "events_per_day": 0.8,
                "how_to_use": "in precompute(): library_signal_on_bars(...)",
            }
        ],
        "near_misses": [],
    }
    store.write_json(job_id, pack_path, pack)
    prompt = campaign_prompt_block(store, job_id, now=started + timedelta(minutes=2))
    assert prompt and prompt["constraints"]["validated_signals"] == [
        "HYPE:new_low_5:1h:4"
    ]
    assert "new_low_5 long HYPE 1h x4 (0.8 events/day)" in prompt["next_action"]
    assert "library_signal_on_bars" in prompt["next_action"]
    assert (
        "/validated_signals/signals/0/how_to_use" in prompt["valid_evidence_pointers"]
    )


def test_regime_design_requires_counter_cell_and_stamps_candidate(tmp_path) -> None:
    store, job_id = _investigative_job(tmp_path)
    root = store.job_dir(job_id)
    improver_path = root / "improver.yaml"
    improver = yaml.safe_load(improver_path.read_text(encoding="utf-8"))
    improver["evolution"]["regime_specialist_enabled"] = True
    improver_path.write_text(yaml.safe_dump(improver), encoding="utf-8")
    (root / "results/backtest/input_bars.json").write_text(
        json.dumps({"metadata": {"days": 2}, "bars": _regime_bars()}),
        encoding="utf-8",
    )
    started = datetime(2099, 8, 25, 12, tzinfo=UTC)
    state = start_campaign(store, job_id, now=started)
    manifest = store.read_json(job_id, str(state["manifest"]))
    regime = manifest["regime_context"]

    assert regime["available"] is True
    prompt = campaign_prompt_block(store, job_id, now=started + timedelta(minutes=1))
    assert prompt and prompt["constraints"]["regime_specialist_design"] is True
    assert "target_regimes" in prompt["next_action"]

    design = _campaign_design()
    for slot in design["slots"]:
        slot["target_regimes"] = [regime["primary_regime"]]
    alternate = next(
        cell
        for cell in ("up_lowvol", "up_highvol", "down_lowvol", "down_highvol")
        if cell not in {regime["primary_regime"], regime["counter_regime"]}
    )
    design["slots"][-1]["target_regimes"] = [alternate]
    with pytest.raises(ValueError, match="counter-regime slot"):
        submit_campaign_design(store, job_id, campaign_design=design)

    design["slots"][0]["target_regimes"] = [regime["counter_regime"]]
    submit_campaign_design(store, job_id, campaign_design=design)
    candidate = prepare_candidate(store, job_id, now=started + timedelta(minutes=2))
    job_data = yaml.safe_load(
        (root / candidate["bundle"] / "job.yaml").read_text(encoding="utf-8")
    )

    assert candidate["target_regimes"] == [regime["counter_regime"]]
    assert job_data["execution_params"]["target_regimes"] == [regime["counter_regime"]]


def test_investigative_attempts_repair_failures_but_close_viable_ideas(
    tmp_path,
) -> None:
    store, job_id = _investigative_job(tmp_path)
    started = datetime(2099, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    submit_campaign_design(store, job_id, campaign_design=_campaign_design())
    prepared = prepare_candidate(store, job_id, now=started + timedelta(minutes=1))
    candidate_root = store.job_dir(job_id) / prepared["bundle"]
    script = candidate_root / "workspace" / "src" / "strategy.py"

    def outcome(
        *,
        net: float,
        viable: bool,
        progress: bool = False,
    ) -> dict[str, Any]:
        revision = compute_workspace_revision(candidate_root)
        return {
            "status": "quick_complete" if viable else "low_fidelity_rejected",
            "evidence": "attempt receipt",
            "revision": revision,
            "quick": {"stats": {"trade_count": 4, "net_return": net}},
            "objective": {
                "net_log_growth": net,
                "downside_deviation": 0.0,
                "tail_loss": 0.0,
                "max_drawdown_pct": 0.0,
            },
            "attempt_receipt": {
                "revision": revision,
                "execution_valid": True,
                "stats": {"trade_count": 4, "net_return": net},
                "objective": {"net_log_growth": net},
                "behavior": {},
                "trades": [],
            },
            "postmortem": {
                "viable": viable,
                "primary_failure": None if viable else "negative_after_costs",
                "failure_codes": [] if viable else ["negative_after_costs"],
                "behavior_diff": {"material_change": True},
                **(
                    {"progress_from_previous": {"net_return_delta": 0.01}}
                    if progress
                    else {}
                ),
            },
        }

    state = campaign_status(store, job_id)
    candidate = state["candidates"][0]
    script.write_text(script.read_text() + "\nATTEMPT_1 = True\n")
    _commit_designed_attempt(
        store,
        job_id,
        state=state,
        candidate=candidate,
        outcome=outcome(net=-0.02, viable=False),
    )
    store.write_json(job_id, "state/evolution_campaign.json", state)
    assert candidate["status"] == "repair_pending"
    assert state["counts"]["quick_evaluated"] == 0
    repair_prompt = campaign_prompt_block(
        store, job_id, now=started + timedelta(minutes=2)
    )
    assert repair_prompt and repair_prompt["artifact_key"] == (
        "candidate-01-attempt-02"
    )
    assert repair_prompt["postmortem_path"].endswith("a01/postmortem.json")

    script.write_text(script.read_text() + "\nATTEMPT_2 = True\n")
    _commit_designed_attempt(
        store,
        job_id,
        state=state,
        candidate=candidate,
        outcome=outcome(net=-0.01, viable=False, progress=True),
    )
    store.write_json(job_id, "state/evolution_campaign.json", state)
    assert candidate["status"] == "repair_pending"

    script.write_text(script.read_text() + "\nATTEMPT_3 = True\n")
    _commit_designed_attempt(
        store,
        job_id,
        state=state,
        candidate=candidate,
        outcome=outcome(net=0.03, viable=True, progress=True),
    )

    assert candidate["status"] == "quick_complete"
    assert candidate["attempt_count"] == 3
    assert candidate["best_attempt"] == 3
    assert state["counts"] == {
        "generated": 1,
        "quick_evaluated": 1,
        "full_dev": 0,
        "proposed": 0,
        "quick_attempts": 3,
        "repairs": 2,
    }
    _archive_campaign_candidate(store, job_id, candidate)
    lessons = evolution_lessons_block(store, job_id)["outcomes"]
    assert lessons[0]["postmortem"]["viable"] is True
    assert lessons[0]["postmortem"]["behavior_diff"]["material_change"] is True

    store.write_json(job_id, "state/evolution_campaign.json", state)
    prepared_two = prepare_candidate(store, job_id, now=started + timedelta(minutes=2))
    state = campaign_status(store, job_id)
    candidate_two = state["candidates"][1]
    candidate_root = store.job_dir(job_id) / prepared_two["bundle"]
    script = candidate_root / "workspace" / "src" / "strategy.py"
    script.write_text(script.read_text() + "\nFIRST_PASS = True\n")
    _commit_designed_attempt(
        store,
        job_id,
        state=state,
        candidate=candidate_two,
        outcome=outcome(net=0.04, viable=True),
    )
    assert candidate_two["status"] == "quick_complete"
    assert candidate_two["attempt_count"] == 1
    assert state["counts"]["quick_evaluated"] == 2
    assert state["counts"]["quick_attempts"] == 4
    assert state["counts"]["repairs"] == 2


def test_invalid_investigative_attempt_preserves_bounded_repair_cause(
    tmp_path,
) -> None:
    store, job_id = _investigative_job(tmp_path)
    started = datetime(2099, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    submit_campaign_design(store, job_id, campaign_design=_campaign_design())
    prepare_candidate(store, job_id, now=started + timedelta(minutes=1))
    state = campaign_status(store, job_id)
    candidate = state["candidates"][0]

    _commit_designed_attempt(
        store,
        job_id,
        state=state,
        candidate=candidate,
        outcome={
            "status": "invalid",
            "evidence": {
                "error": "window-invariance probe consumed more than warmup_bars"
            },
        },
    )

    repair_context = candidate["attempts"][0]["postmortem"]["repair_context"]
    assert "warmup_bars" in repair_context["error"]
    persisted = store.read_json(
        job_id,
        candidate["attempts"][0]["postmortem_path"],
        default={},
    )
    assert persisted["repair_context"] == repair_context


def test_sensor_research_seed_is_frozen_then_consumed_as_real_parent(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    root = store.job_dir(job_id)
    source_revision = compute_workspace_revision(root)
    seed_root = root / "research" / "sensor_candidates" / "regime-break"
    (seed_root / "workspace/src").mkdir(parents=True)
    (seed_root / "workspace/src/strategy.py").write_text(
        "# sensor hypothesis\ndef decide(ctx):\n    return []\n", encoding="utf-8"
    )
    (seed_root / "job.yaml").write_bytes((root / "job.yaml").read_bytes())
    store.write_json(job_id, "results/research/regime_health.json", {"ready": True})
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign.validate_execution_job",
        lambda *args, **kwargs: {"checks": []},
    )

    seed = submit_research_seed(
        store,
        job_id,
        candidate_root=seed_root,
        family="regime-break",
        hypothesis="remove HYPE entries after a structural regime alarm",
        base_revision=source_revision,
        evidence_refs=["results/research/regime_health.json"],
        now=datetime(2026, 8, 25, 11, tzinfo=UTC),
    )
    campaign = start_campaign(
        store,
        job_id,
        now=datetime(2026, 8, 25, 12, tzinfo=UTC),
        force=True,
    )
    manifest = json.loads((root / campaign["manifest"]).read_text(encoding="utf-8"))
    assert manifest["research_seeds"][0]["seed_id"] == seed["seed_id"]
    assert manifest["research_seeds"][0]["evidence_reset"] is True

    candidate = prepare_candidate(
        store,
        job_id,
        family="regime-break",
        summary="materialize the sensor seed",
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )

    assert candidate["parent_source"] == "research_seed"
    assert candidate["research_seed_id"] == seed["seed_id"]
    assert candidate["evidence_reset"] is True
    seed_state = store.read_json(job_id, "state/evolution_research_seeds.json")
    assert seed_state["seeds"][0]["status"] == "consumed"


def test_sensor_research_seed_requires_checked_in_research_result(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    root = store.job_dir(job_id)
    seed_root = root / "research" / "sensor_candidates" / "unsupported"
    (seed_root / "workspace/src").mkdir(parents=True)
    (seed_root / "workspace/src/strategy.py").write_text(
        "# unsupported\ndef decide(ctx):\n    return []\n", encoding="utf-8"
    )
    (seed_root / "job.yaml").write_bytes((root / "job.yaml").read_bytes())
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign.validate_execution_job",
        lambda *args, **kwargs: {"checks": []},
    )

    with pytest.raises(ValueError, match="checked-in research result"):
        submit_research_seed(
            store,
            job_id,
            candidate_root=seed_root,
            family="unsupported",
            hypothesis="no evidence",
            base_revision=compute_workspace_revision(root),
            evidence_refs=["results/research/missing.json"],
        )


def test_campaign_start_defers_while_intervention_worker_is_busy(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign.require_evolution_launch_headroom",
        lambda: {"ready": True},
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.worker.job_worker_session_busy", lambda *args: True
    )

    with pytest.raises(TransientInfrastructureError, match="intervention worker"):
        start_campaign(store, job_id, now=datetime(2026, 8, 25, 19, tzinfo=UTC))
    assert campaign_status(store, job_id) == {}


def test_only_one_automatic_campaign_owns_a_machine(tmp_path) -> None:
    store, first = _job(tmp_path, "first-lab")
    _, second = _job(tmp_path, "second-lab")
    for job_id in (first, second):
        (store.job_dir(job_id) / "improver.yaml").write_text(
            yaml.safe_dump(
                {"evolution": {"allowed_job_ids": ["first-lab", "second-lab"]}}
            ),
            encoding="utf-8",
        )
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, first, now=now)

    assert campaign_due(store, second, now=now) is False
    with pytest.raises(TransientInfrastructureError, match="machine evolution"):
        start_campaign(store, second, now=now)
    with pytest.raises(TransientInfrastructureError, match="machine evolution"):
        start_campaign(store, second, now=now, force=True)


def test_runner_command_declares_control_and_heavy_resource_tiers() -> None:
    assert "--resource-tier=control" in op_runner_command("evolution_prepare")
    assert "--resource-tier=control" in op_runner_command("evolution_design")
    assert "--resource-tier=control" in op_runner_command("evolution_start")
    assert "--resource-tier=heavy" in op_runner_command("evolution_evaluate")
    assert "--resource-tier=heavy" in op_runner_command("backtest_job")


def test_due_campaign_defers_without_spawning_when_burst_credit_is_low(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")

    class FakeClient:
        def healthy(self) -> bool:
            return True

    monkeypatch.setattr("wayfinder_paths.jobs.worker.OPENCODE_CLIENT", FakeClient())
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign.campaign_due", lambda *args: True
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.resource_envelope.evolution_launch_readiness",
        lambda: {
            "ready": False,
            "source": "governor",
            "reason": "burst reserve is low",
        },
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.background.spawn_detached_op",
        lambda *args, **kwargs: pytest.fail("campaign start should not spawn"),
    )

    result = _queue_evolution_worker(store, job_id)

    assert result == {
        "queued": False,
        "starting": False,
        "deferred": True,
        "reason": "burst reserve is low",
    }


def test_active_evolution_suppresses_the_parallel_intervention_llm(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    monkeypatch.setattr("wayfinder_paths.jobs.worker.JobStore", lambda: store)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.worker._queue_evolution_worker",
        lambda *args: {"queued": True, "session_id": "evolution-session"},
    )

    report = run_job_worker(job_id, mode="intervene")

    assert report["queued"] is False
    assert report["session_id"] == "evolution-session"
    assert "deferred while evolution owns the lane" in report["summary"]


def test_evolution_enabled_intervention_prompt_is_sensor_and_safety_only(
    tmp_path,
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")

    sections = prepare_job_worker_prompt(store=store, job_id=job_id, mode="intervene")

    prefix = sections["stable_prefix"]
    prompt = sections["prompt"]
    assert "sensor/safety lane" in prefix
    assert "belong exclusively to the evolution campaign" in prefix
    assert "supersedes generic candidate/probation mandates" in prefix
    assert "exact behavior-equivalence maintenance" in prefix
    assert "EVOLUTION SENSOR CONTRACT" in prompt
    assert "Regime alarm triage" in prompt
    assert "action='risk_block_symbol'" in prompt
    assert "wake_id=<wake.id>" in prompt
    assert "action='evolution_submit_seed'" in prompt
    assert "read ALL failed check names" in prompt
    assert "wayfinder job revalidate <job_id> <proposal_id>" in prompt
    assert "open_paper_probation_leg" not in prompt
    assert "When you want to RECOMMEND a strategy/params change" not in prompt


def test_governor_diagnostic_error_does_not_wedge_the_intervention_lane(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    normal_report = {"status": "green", "summary": "normal lane reached"}
    monkeypatch.setattr("wayfinder_paths.jobs.worker.JobStore", lambda: store)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.worker._queue_evolution_worker",
        lambda *args: {"queued": False, "error": "governor state is stale"},
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.worker.maybe_skip_wake",
        lambda *args, **kwargs: normal_report,
    )

    assert run_job_worker(job_id, mode="intervene") is normal_report


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (0, 59, True),
        (1, 0, False),
        (3, 59, False),
        (4, 0, True),
        (6, 0, False),
        (9, 59, False),
        (10, 0, True),
    ],
)
def test_evolution_pauses_during_deepseek_peak_pricing(
    tmp_path, hour: int, minute: int, expected: bool
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    now = datetime(2026, 8, 25, hour, minute, tzinfo=UTC)
    assert evolution_compute_window_open(store, job_id, now=now) is expected


def test_campaign_start_reserves_full_window_before_peak_pricing(tmp_path) -> None:
    safe_store, safe_id = _job(tmp_path / "safe", "majors-5m-lab")
    safe = datetime(2026, 8, 25, 19, tzinfo=UTC)
    assert evolution_compute_window_open(
        safe_store, safe_id, now=safe, reserve_campaign=True
    )
    assert campaign_due(safe_store, safe_id, now=safe)

    blocked_store, blocked_id = _job(tmp_path / "blocked", "majors-5m-lab")
    blocked = datetime(2026, 8, 25, 21, tzinfo=UTC)
    assert not evolution_compute_window_open(
        blocked_store, blocked_id, now=blocked, reserve_campaign=True
    )
    assert not campaign_due(blocked_store, blocked_id, now=blocked)
    assert maybe_start_campaign(blocked_store, blocked_id, now=blocked) is None
    with pytest.raises(ValueError, match="peak pricing"):
        start_campaign(blocked_store, blocked_id, now=blocked)


def test_active_campaign_prompt_is_suppressed_during_peak_pricing(tmp_path) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    store.write_json(
        job_id,
        "state/evolution_campaign.json",
        {"campaign_id": "campaign-1", "status": "active"},
    )
    assert campaign_prompt_block(
        store, job_id, now=datetime(2026, 8, 25, 2, tzinfo=UTC)
    ) == {
        "status": "blocked",
        "reason": "evolution worker paused during peak model pricing",
    }


def test_prepare_candidate_isolated_lineage_and_mutation_budget(tmp_path) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    start_campaign(store, job_id, now=datetime(2026, 8, 25, 12, tzinfo=UTC))
    active_script = store.job_dir(job_id) / "workspace/src/strategy.py"
    active_script.write_text("raise RuntimeError('post-freeze change')\n")
    first = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="structural breadth and exit mutation",
        mutation_kind="parameter",  # slot 1 is forced structural by budget
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )
    assert first["mutation_kind"] == "structural"
    root = store.job_dir(job_id)
    bundle = root / first["bundle"]
    assert first["bundle_path"] == str(bundle.resolve())
    assert "bundle_path" not in campaign_status(store, job_id)["candidates"][0]
    assert bundle != root
    assert (bundle / "workspace" / "src" / "strategy.py").exists()
    assert (
        "post-freeze change"
        not in (bundle / "workspace" / "src" / "strategy.py").read_text()
    )
    assert (bundle / "job.yaml").exists()
    archive = quality_diversity_snapshot(store, job_id)
    assert archive == {}  # generated candidate has no behavior until evaluated

    second = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="second structural attempt",
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )
    state = campaign_status(store, job_id)
    state["candidates"][0]["status"] = "low_fidelity_rejected"
    state["candidates"][1]["status"] = "invalid"
    store.write_json(job_id, "state/evolution_campaign.json", state)
    third = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="stuck rule jumps basin",
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )
    assert second["requested_parent_source"] == "qd_elite"
    assert second["parent_source"] == "starter_seed"
    assert second["starter_seed_id"]
    assert second["parent_candidate_ids"] == []
    assert third["forced_jump"] is True
    assert third["parent_source"] == "de_novo"

    state["candidates"].append({"family": "breakout", "status": "dev_frontier"})
    assert _same_family_nonwins(state, "breakout", 2) is False

    mix = {"incumbent": 0.3, "qd_elite": 0.3, "crossover": 0.2, "de_novo": 0.2}
    sources = [_parent_source(slot, mix) for slot in range(1, 13)]
    assert {source: sources.count(source) for source in mix} == {
        "incumbent": 4,
        "qd_elite": 4,
        "crossover": 2,
        "de_novo": 2,
    }


def test_cold_start_seed_uses_starter_window_and_resets_evidence(tmp_path) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    job_path = store.job_dir(job_id) / "job.yaml"
    job_data = yaml.safe_load(job_path.read_text(encoding="utf-8"))
    job_data["execution_params"] = {
        "symbols": ["BTC", "ETH"],
        "fee_bps": 4.5,
        "slippage_bps": 3.5,
    }
    job_path.write_text(yaml.safe_dump(job_data, sort_keys=False), encoding="utf-8")
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    prepare_candidate(
        store,
        job_id,
        family="incumbent-neighbor",
        summary="consume incumbent slot",
        now=started + timedelta(hours=1),
    )
    seeded = prepare_candidate(
        store,
        job_id,
        family="starter-transfer",
        summary="adapt an audited starter",
        now=started + timedelta(hours=1),
    )

    definition = next(
        item for item in STARTER_DEFINITIONS if item.id == seeded["starter_seed_id"]
    )
    params = _read_bundle_params(store, job_id, seeded["bundle"])
    script = (
        store.job_dir(job_id) / seeded["bundle"] / "workspace" / "src" / "strategy.py"
    ).read_text(encoding="utf-8")

    assert seeded["requested_parent_source"] == "qd_elite"
    assert seeded["parent_source"] == "starter_seed"
    assert seeded["evidence_reset"] is True
    assert params["warmup_bars"] == starter_warmup_bars(definition)
    assert params["lookback_bars"] == starter_lookback_bars(definition)
    assert definition.module.rsplit(".", 1)[-1].split("_")[0] in script.lower()
    assert params["symbols"] == ["BTC", "ETH"]
    assert params["fee_bps"] == 4.5


def test_de_novo_seed_keeps_target_infra_but_drops_incumbent_alpha(tmp_path) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    job_path = store.job_dir(job_id) / "job.yaml"
    job_data = yaml.safe_load(job_path.read_text(encoding="utf-8"))
    job_data["execution_params"] = {
        "symbols": ["BTC", "ETH"],
        "fee_bps": 4.5,
        "slippage_bps": 3.5,
        "incumbent_alpha_threshold": 0.73,
    }
    job_path.write_text(yaml.safe_dump(job_data, sort_keys=False), encoding="utf-8")
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    candidates = _prepare_campaign_candidates(store, job_id, started)
    de_novo = candidates[3]
    params = _read_bundle_params(store, job_id, de_novo["bundle"])
    script = (
        store.job_dir(job_id) / de_novo["bundle"] / "workspace" / "src" / "strategy.py"
    ).read_text(encoding="utf-8")

    assert de_novo["requested_parent_source"] == "de_novo"
    assert de_novo["parent_source"] == "de_novo"
    assert "incumbent_alpha_threshold" not in params
    assert params["symbols"] == ["BTC", "ETH"]
    assert params["fee_bps"] == 4.5
    assert params["warmup_bars"] == DEFAULT_WARMUP_BARS
    assert "Clean de-novo evolution scaffold" in script


def test_next_campaign_materializes_real_qd_parent_bytes(tmp_path) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    job_path = store.job_dir(job_id) / "job.yaml"
    job_data = yaml.safe_load(job_path.read_text(encoding="utf-8"))
    job_data["execution_params"] = {
        "symbols": ["SOL"],
        "fee_bps": 1.0,
        "leverage": 1.0,
        "wallet_label": "old-wallet",
        "parent_alpha_threshold": 0.7,
    }
    job_path.write_text(yaml.safe_dump(job_data, sort_keys=False), encoding="utf-8")
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    first_state = start_campaign(store, job_id, now=started)
    elite = prepare_candidate(
        store,
        job_id,
        family="compression-breakout",
        summary="validated executable elite",
        now=started + timedelta(hours=1),
    )
    elite_root = store.job_dir(job_id) / elite["bundle"]
    elite_script = elite_root / "workspace" / "src" / "strategy.py"
    elite_script.write_text(
        "def decide(ctx):\n    return []\n\nELITE_MARKER = 'real-parent'\n",
        encoding="utf-8",
    )
    revision = compute_workspace_revision(elite_root)
    state = campaign_status(store, job_id)
    state["candidates"][0].update(
        {
            "status": "dev_frontier",
            "revision": revision,
            "objective": {
                "net_log_growth": 1.0,
                "downside_deviation": 0.1,
                "tail_loss": 0.1,
                "max_drawdown_pct": 0.1,
            },
            "behavior": {
                "direction_bias": 0.5,
                "average_hold_bars": 24,
                "trades_per_asset_30d": 10,
            },
            "dev": {"validation": {"stats": {"net_return": 0.1}}},
            "elite_eligible": True,
            "elite_activity": {
                "validation_trades": 12,
                "minimum": 8,
                "target": 12,
            },
        }
    )
    state["status"] = "complete"
    store.write_json(job_id, "state/evolution_campaign.json", state)
    _archive_campaign_candidate(store, job_id, state["candidates"][0])

    job_data = yaml.safe_load(job_path.read_text(encoding="utf-8"))
    job_data["execution_params"].update(
        {
            "symbols": ["BTC", "ETH"],
            "fee_bps": 4.5,
            "leverage": 2.0,
            "wallet_label": "current-wallet",
        }
    )
    job_path.write_text(yaml.safe_dump(job_data, sort_keys=False), encoding="utf-8")

    second_state = start_campaign(
        store, job_id, now=started + timedelta(days=1), force=True
    )
    prepare_candidate(
        store,
        job_id,
        family="incumbent-neighbor",
        summary="consume incumbent slot",
        now=started + timedelta(days=1, hours=1),
    )
    child = prepare_candidate(
        store,
        job_id,
        family="compression-crossover",
        summary="mutate the frozen elite",
        now=started + timedelta(days=1, hours=1),
    )
    child_script = (
        store.job_dir(job_id) / child["bundle"] / "workspace" / "src" / "strategy.py"
    ).read_text(encoding="utf-8")
    child_params = _read_bundle_params(store, job_id, child["bundle"])
    manifest = json.loads(
        (store.job_dir(job_id) / second_state["manifest"]).read_text(encoding="utf-8")
    )

    assert second_state["campaign_id"] != first_state["campaign_id"]
    assert child["parent_source"] == "qd_elite"
    assert child["parent_candidate_ids"] == [elite["candidate_id"]]
    assert child["seed_revision"] != revision
    assert "ELITE_MARKER = 'real-parent'" in child_script
    assert child_params["parent_alpha_threshold"] == 0.7
    assert child_params["symbols"] == ["BTC", "ETH"]
    assert child_params["fee_bps"] == 4.5
    assert child_params["leverage"] == 2.0
    assert child_params["wallet_label"] == "current-wallet"
    assert manifest["parent_pool"]["qd_elite_ids"] == [elite["candidate_id"]]
    archived = next(
        item
        for item in load_archive(store, job_id)["candidates"]
        if item["candidate_id"] == elite["candidate_id"]
    )
    assert archived["metadata"]["executable_bundle"].endswith(revision)


def test_crossover_requires_two_real_parents_and_uses_stronger_primary() -> None:
    weak = {
        "candidate_id": "weak",
        "bundle": "parents/weak",
        "objective": {"net_log_growth": 0.1},
    }
    strong = {
        "candidate_id": "strong",
        "bundle": "parents/strong",
        "objective": {"net_log_growth": 0.5},
    }
    incumbent = {
        "candidate_id": "archived-incumbent",
        "bundle": "parents/archived-incumbent",
        "status": "incumbent",
        "objective": {"net_log_growth": 1.0},
    }
    plan = _select_parent_plan(
        {
            "parent_pool": {
                "candidates": [incumbent, weak, strong],
                "qd_elite_ids": [],
            },
            "starter_seeds": [],
        },
        requested_source="crossover",
        slot=1,
        candidates=[],
    )

    assert plan["source"] == "crossover"
    assert plan["primary"]["candidate_id"] == "strong"
    assert plan["secondary"]["candidate_id"] == "weak"
    assert "archived-incumbent" not in {
        item["candidate_id"] for item in plan["parents"]
    }


def test_parent_bundle_integrity_is_checked_at_both_copy_boundaries(tmp_path) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    state = start_campaign(store, job_id, now=started)
    manifest_path = store.job_dir(job_id) / state["manifest"]
    campaign_root = manifest_path.parent
    frozen_source = campaign_root / "source"
    revision = compute_workspace_revision(frozen_source)

    with pytest.raises(ValueError, match="source executable parent revision mismatch"):
        _persist_executable_bundle(
            store,
            job_id,
            candidate_id="tampered-source",
            revision="wrong-revision",
            source=frozen_source,
        )

    parent = campaign_root / "parents" / "elite"
    parent.mkdir(parents=True)
    shutil.copy2(frozen_source / "job.yaml", parent / "job.yaml")
    shutil.copytree(frozen_source / "workspace", parent / "workspace")
    assert compute_workspace_revision(parent) == revision
    (parent / "workspace" / "src" / "strategy.py").write_text(
        "def decide(ctx):\n    raise RuntimeError('tampered')\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="frozen executable parent revision mismatch"):
        _materialize_candidate_seed(
            store,
            job_id,
            campaign_id=state["campaign_id"],
            candidate_root=campaign_root / "candidates" / "poisoned-child",
            plan={
                "source": "qd_elite",
                "primary": {"bundle": "parents/elite", "revision": revision},
            },
        )


def test_campaign_freezes_only_existing_refutations_and_validated_wins(
    tmp_path,
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    record_candidate(
        store,
        job_id,
        candidate_id="refuted-1",
        family="volatility-regime-entry-gate",
        summary="failed branch",
        status="refuted",
        objective=None,
        evidence="negative paired folds",
    )
    store.write_json(
        job_id,
        "probation.json",
        {
            "legs": [
                {
                    "name": "compression-gate",
                    "symbol": "HYPE",
                    "status": "graduated",
                    "graduate": {"progress": "positive forward effect"},
                    "closed_at": "2026-08-24T00:00:00+00:00",
                }
            ]
        },
    )
    store.write_json(
        job_id,
        "state/promotion_verdicts.json",
        {
            "prop-win": {
                "verdict": "beat",
                "strategy_effect": 3.5,
                "recorded_at": "2026-08-25T00:00:00+00:00",
            },
            "prop-execution-only": {
                "verdict": "beat",
                "strategy_effect": 0.0,
                "recorded_at": "2026-08-26T00:00:00+00:00",
            },
        },
    )

    state = start_campaign(store, job_id, now=datetime(2026, 8, 27, 12, tzinfo=UTC))
    manifest = json.loads(
        (store.job_dir(job_id) / state["manifest"]).read_text(encoding="utf-8")
    )
    context = manifest["research_context"]

    assert context["refuted_families"] == [
        {
            "family": "volatility-regime-entry-gate",
            "candidate_id": "refuted-1",
            "evidence": "negative paired folds",
        }
    ]
    assert {item["id"] for item in context["validated_positives"]} == {
        "compression-gate",
        "prop-win",
    }
    assert context["matching"] == "exact_free_form_family_v1"
    assert "not deletion of a refutation" in context["accepted_limitation"]
    block = campaign_prompt_block(
        store, job_id, now=datetime(2026, 8, 27, 13, tzinfo=UTC)
    )
    assert block is not None
    assert block["research_context"] == context
    assert "volatility-regime-entry-gate" in block["next_action"]
    assert "prop-win" in block["next_action"]


def test_campaign_assigns_parameter_slots_and_preserves_optuna_budget(tmp_path) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    candidates = _prepare_campaign_candidates(store, job_id, started)
    assert [candidate["mutation_kind"] for candidate in candidates] == [
        "structural",
        "structural",
        "structural",
        "parameter",
    ]
    structural_prompt = campaign_prompt_block(
        store, job_id, now=started + timedelta(hours=1)
    )
    assert structural_prompt is not None
    assert "meaningful numeric behavior knobs" in structural_prompt["next_action"]
    assert "Otherwise omit it" in structural_prompt["next_action"]

    state = campaign_status(store, job_id)
    for candidate in state["candidates"][:3]:
        candidate["status"] = "invalid"
    store.write_json(job_id, "state/evolution_campaign.json", state)
    parameter_prompt = campaign_prompt_block(
        store, job_id, now=started + timedelta(hours=1)
    )
    assert parameter_prompt is not None
    assert '"type":"int","low":12,"high":96' in parameter_prompt["next_action"]

    parameter_root = store.job_dir(job_id) / candidates[-1]["bundle"]
    (parameter_root / "search_space.json").write_text(
        json.dumps(
            {
                "entry_threshold": {
                    "type": "float",
                    "low": 0.1,
                    "high": 0.9,
                }
            }
        ),
        encoding="utf-8",
    )
    state = campaign_status(store, job_id)
    state["candidates"][0].update(
        {"status": "quick_complete", "objective": {"net_log_growth": 1.0}}
    )
    state["candidates"][-1].update(
        {"status": "quick_complete", "objective": {"net_log_growth": 0.5}}
    )
    for candidate in state["candidates"][1:3]:
        candidate["status"] = "invalid"
    state["counts"]["quick_evaluated"] = 4
    store.write_json(job_id, "state/evolution_campaign.json", state)

    structural_claim = _claim_full_dev(store, job_id)
    assert structural_claim is not None
    campaign_id, claim_id, structural, tune = structural_claim
    assert structural["candidate_id"] == candidates[0]["candidate_id"]
    assert tune is False
    _commit_full_dev(
        store,
        job_id,
        campaign_id=campaign_id,
        candidate_id=structural["candidate_id"],
        claim_id=claim_id,
        outcome={"status": "low_fidelity_rejected", "evidence": "done"},
    )

    parameter_claim = _claim_full_dev(store, job_id)
    assert parameter_claim is not None
    _, _, parameter, tune = parameter_claim
    assert parameter["candidate_id"] == candidates[-1]["candidate_id"]
    assert tune is True
    assert parameter["full_dev_selection_reason"] == "ranked_parameter_search"


def test_full_dev_reservation_uses_parameter_preview_and_never_wedges() -> None:
    structural = {
        "candidate_id": "c01",
        "mutation_kind": "structural",
        "objective": {"net_log_growth": 0.8},
    }
    weaker_parameter = {
        "candidate_id": "c04",
        "mutation_kind": "parameter",
        "objective": {"net_log_growth": 0.2},
        "tuning_preview": {"objective": {"net_log_growth": 0.3}},
    }
    stronger_parameter = {
        "candidate_id": "c08",
        "mutation_kind": "parameter",
        "objective": {"net_log_growth": 0.1},
        "tuning_preview": {"objective": {"net_log_growth": 0.5}},
    }
    typed_search = {"c01": False, "c04": True, "c08": True}

    selected = _select_full_dev_candidate(
        [structural, weaker_parameter, stronger_parameter],
        typed_search=typed_search,
        remaining=1,
        tuning_limit=2,
        tuning_allocated=0,
        parameter_tuning_allocated=0,
        parameter_tuning_minimum=1,
    )

    assert selected == (stronger_parameter, True, "reserved_parameter")

    fallback = _select_full_dev_candidate(
        [structural],
        typed_search={"c01": False},
        remaining=1,
        tuning_limit=2,
        tuning_allocated=0,
        parameter_tuning_allocated=0,
        parameter_tuning_minimum=1,
    )
    assert fallback == (structural, False, "ranked_structural")


def test_parameter_preview_is_seeded_bounded_and_compact(monkeypatch) -> None:
    import wayfinder_paths.jobs.evolution_campaign as campaign_module

    preview_data = SimpleNamespace(bars=SimpleNamespace(timestamps=list(range(2_000))))
    captured: dict[str, Any] = {}

    def fake_search(script, dataset, spec, search_space, **kwargs):
        captured.update(
            {
                "script": script,
                "dataset": dataset,
                "spec": spec,
                "search_space": search_space,
                **kwargs,
            }
        )
        best_params = {
            "initial_capital": 1_000.0,
            "symbols": ["IMX"],
            "lookback": 48,
        }
        return SimpleNamespace(
            runs=[{}, {}, {}],
            invalid=[{}, {}],
            ranked=[
                {
                    "params": best_params,
                    "stats": {
                        "net_return": 0.12,
                        "avg_drawdown": -0.02,
                        "worst_trade_pnl": -5.0,
                        "max_drawdown_pct": -0.04,
                    },
                }
            ],
        )

    monkeypatch.setattr(campaign_module, "_tail", lambda dataset, bars: preview_data)
    monkeypatch.setattr(campaign_module, "run_optuna_search", fake_search)

    preview = _parameter_tuning_preview(
        {"script": "strategy.py", "spec": {"market_kind": "perp"}},
        SimpleNamespace(),
        {"initial_capital": 1_000.0, "symbols": ["IMX"]},
        {"lookback": {"type": "int", "low": 12, "high": 96}},
        {
            "inner_optuna_preview_trials": 3,
            "inner_optuna_preview_bars": 2_000,
            "inner_optuna_preview_timeout_seconds": 300,
        },
    )

    assert captured["dataset"] is preview_data
    assert captured["n_trials"] == 3
    assert captured["seed"] == 42
    assert captured["timeout"] == 300
    assert captured["objectives"] == ["net_return", "max_drawdown_pct"]
    assert preview == {
        "status": "complete",
        "risk_pruned": 0,
        "max_drawdown_pct": None,
        "trials": 3,
        "valid_trials": 1,
        "bars": 2_000,
        "seed": 42,
        "wall_seconds": preview["wall_seconds"],
        "objective": {
            "net_log_growth": 0.11332869,
            "downside_deviation": 0.02,
            "tail_loss": 0.005,
            "max_drawdown_pct": 0.04,
        },
        "selected_params": {"lookback": 48},
    }
    assert preview["wall_seconds"] >= 0

    monkeypatch.setattr(
        campaign_module,
        "run_optuna_search",
        lambda *args, **kwargs: SimpleNamespace(
            runs=[{}, {}, {}], invalid=[{}, {}, {}], ranked=[]
        ),
    )
    no_valid_preview = _parameter_tuning_preview(
        {"script": "strategy.py", "spec": {}},
        SimpleNamespace(),
        {},
        {"lookback": {"type": "int", "low": 12, "high": 96}},
        {
            "inner_optuna_preview_trials": 3,
            "inner_optuna_preview_bars": 2_000,
        },
    )
    assert no_valid_preview is not None
    assert no_valid_preview["status"] == "no_valid_trials"
    assert no_valid_preview["valid_trials"] == 0
    assert "objective" not in no_valid_preview

    assert (
        _parameter_tuning_preview(
            {"script": "strategy.py", "spec": {}},
            SimpleNamespace(),
            {},
            {"lookback": {"type": "int", "low": 12, "high": 96}},
            {},
        )
        is None
    )


def test_next_campaign_freezes_compact_prior_outcomes(tmp_path) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    first = start_campaign(store, job_id, now=started)
    quick_candidate = prepare_candidate(
        store,
        job_id,
        family="position-sizing",
        summary="scale exposure by recent volatility",
        now=started + timedelta(hours=1),
    )
    rejected_candidate = prepare_candidate(
        store,
        job_id,
        family="regime-filter",
        summary="suppress entries in weak trend regimes",
        now=started + timedelta(hours=1),
    )
    state = campaign_status(store, job_id)
    state["status"] = "complete"
    state["candidates"][0].update(
        {
            "status": "quick_complete",
            "quick": {"stats": {"net_return": 0.02, "trade_count": 8}},
            "objective": {"net_log_growth": 0.0198},
            "evidence": "low-fidelity train screen passed",
        }
    )
    state["candidates"][1].update(
        {
            "status": "low_fidelity_rejected",
            "quick": {"stats": {"net_return": -0.01, "trade_count": 5}},
            "dev": {
                "validation": {
                    "stats": {
                        "net_return": -0.03,
                        "max_drawdown_pct": 0.08,
                        "trade_count": 2,
                    }
                }
            },
            "objective": {"net_log_growth": -0.0305},
            "evidence": "failed independent validation",
        }
    )
    store.write_json(job_id, "state/evolution_campaign.json", state)
    assert {entry["status"] for entry in load_archive(store, job_id)["candidates"]} == {
        "generated"
    }

    second = start_campaign(store, job_id, now=started + timedelta(days=1), force=True)
    manifest = store.read_json(job_id, second["manifest"])
    lessons = manifest["historical_lessons"]["outcomes"]
    assert [lesson["candidate_id"] for lesson in lessons[:2]] == [
        rejected_candidate["candidate_id"],
        quick_candidate["candidate_id"],
    ]
    assert lessons[0]["quick_result"] == {"net_return": -0.01, "trade_count": 5}
    assert lessons[0]["validation_result"] == {
        "net_return": -0.03,
        "max_drawdown_pct": 0.08,
        "trade_count": 2,
    }
    assert lessons[0]["rejection_reason"] == "failed independent validation"
    statuses = {
        entry["candidate_id"]: entry["status"]
        for entry in load_archive(store, job_id)["candidates"]
    }
    assert statuses[quick_candidate["candidate_id"]] == "quick_complete"
    assert statuses[rejected_candidate["candidate_id"]] == "low_fidelity_rejected"
    assert first["campaign_id"] != second["campaign_id"]


def test_campaign_cooldown_is_enforced(tmp_path) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    state = start_campaign(store, job_id, now=now)
    state["status"] = "complete"
    store.write_json(job_id, "state/evolution_campaign.json", state)
    with pytest.raises(ValueError, match="start interval"):
        start_campaign(store, job_id, now=now)


def test_finalize_enforces_stage_budgets_and_isolates_candidate_failure(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    candidates = []
    for index in range(6):
        candidate = prepare_candidate(
            store,
            job_id,
            family=f"family-{index}",
            summary=f"candidate {index}",
            now=started.replace(hour=13),
        )
        candidates.append(candidate)
    state = campaign_status(store, job_id)
    for index, candidate in enumerate(state["candidates"]):
        candidate["status"] = "quick_complete"
        candidate["objective"] = {
            "net_log_growth": 1.0 - index / 10,
            "downside_deviation": 0.01,
            "tail_loss": 0.01,
            "max_drawdown_pct": 0.05,
        }
    # Resume after one full-dev result was durably written: it remains proposal
    # eligible and does not consume another development evaluation.
    state["candidates"][0]["status"] = "dev_frontier"
    state["counts"]["quick_evaluated"] = 6
    state["counts"]["full_dev"] = 1
    store.write_json(job_id, "state/evolution_campaign.json", state)

    calls = {"dev": 0, "gate": 0, "proposal": 0}

    def fake_dev(store, job_id, candidate, *, tune):
        calls["dev"] += 1
        if calls["dev"] == 2:
            raise RuntimeError("bad candidate")
        return {"status": "dev_frontier", "evidence": "passed"}

    def fake_gate(*args, **kwargs):
        calls["gate"] += 1
        return {
            "status": "ok",
            "ready": True,
            "objective": {"candidate": {"trade_count": 12}},
            "sim_wall_seconds": 1.0,
        }

    def fake_stage(*args, **kwargs):
        calls["proposal"] += 1
        return {
            "status": "queued",
            "candidate_id": kwargs["candidate_id"],
            "revision": kwargs["revision"],
        }

    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign._isolated_full_dev", fake_dev
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign._isolated_economic_gate", fake_gate
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.probation.stage_evolution_probation", fake_stage
    )
    result = finalize_campaign(store, job_id)
    assert result["status"] == "complete"
    assert result["counts"] == {
        "generated": 6,
        "quick_evaluated": 6,
        "full_dev": 4,
        "proposed": 1,
    }
    assert calls == {"dev": 3, "gate": 1, "proposal": 1}
    assert any(item["status"] == "invalid" for item in result["candidates"])
    completed = next(
        row
        for row in store.read_jsonl(job_id, "journal.jsonl")
        if row.get("type") == "evolution_campaign_completed"
    )
    assert completed["funnel"]["full_development"] == {
        "evaluated": 4,
        "target": 4,
        "passed": 3,
        "rejected": 1,
        "running": 0,
        "awaiting_regime": 0,
    }
    assert completed["funnel"]["finalist_gate"]["advanced_to_paper"] == 1


def test_finalize_rejects_economically_unready_finalist(tmp_path, monkeypatch) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    prepare_candidate(
        store,
        job_id,
        family="noise",
        summary="candidate with available but negative paired evidence",
        now=started.replace(hour=13),
    )
    state = campaign_status(store, job_id)
    state["candidates"][0].update(
        {
            "status": "quick_complete",
            "objective": {
                "net_log_growth": 0.1,
                "downside_deviation": 0.01,
                "tail_loss": 0.01,
                "max_drawdown_pct": 0.05,
            },
        }
    )
    state["counts"]["quick_evaluated"] = 1
    store.write_json(job_id, "state/evolution_campaign.json", state)

    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign._isolated_full_dev",
        lambda *args, **kwargs: {"status": "dev_frontier", "evidence": "passed"},
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign._isolated_economic_gate",
        lambda *args, **kwargs: {
            "status": "ok",
            "ready": False,
            "reasons": ["paired utility estimate not > 0"],
        },
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.paper_experiment.stage_paper_proposal",
        lambda *args, **kwargs: pytest.fail("unready finalist reached paper staging"),
    )

    result = finalize_campaign(store, job_id)

    candidate = result["candidates"][0]
    assert candidate["status"] == "proposal_rejected"
    assert candidate["evidence"] == "paired utility estimate not > 0"


def test_finalize_releases_campaign_state_lock_during_heavy_compute(
    tmp_path, monkeypatch
) -> None:
    import wayfinder_paths.jobs.evolution_campaign as campaign_module

    store, job_id = _job(tmp_path, "majors-5m-lab")
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    prepare_candidate(
        store,
        job_id,
        family="lock-probe",
        summary="prove compute does not pin campaign state",
        now=started + timedelta(hours=1),
    )
    state = campaign_status(store, job_id)
    state["candidates"][0]["status"] = "quick_complete"
    state["candidates"][0]["objective"] = {"net_log_growth": 1.0}
    state["counts"]["quick_evaluated"] = 1
    store.write_json(job_id, "state/evolution_campaign.json", state)

    real_lock = campaign_module.job_state_lock
    depth = 0

    @contextmanager
    def tracked_lock(*args, **kwargs):
        nonlocal depth
        tracks_campaign_state = kwargs.get("name") == "evolution_campaign"
        with real_lock(*args, **kwargs):
            depth += int(tracks_campaign_state)
            try:
                yield
            finally:
                depth -= int(tracks_campaign_state)

    def fake_dev(*args, **kwargs):
        assert depth == 0
        return {"status": "low_fidelity_rejected", "evidence": "probe complete"}

    monkeypatch.setattr(campaign_module, "job_state_lock", tracked_lock)
    monkeypatch.setattr(campaign_module, "_isolated_full_dev", fake_dev)

    result = finalize_campaign(store, job_id)

    assert result["status"] == "complete"
    assert result["counts"]["full_dev"] == 1


def test_quality_diversity_keeps_two_non_dominated_per_cell(tmp_path) -> None:
    store, job_id = _job(tmp_path, "archive-job")
    behavior = {
        "direction_bias": 0.8,
        "median_hold_bars": 4,
        "trades_per_asset_30d": 12,
    }
    assert behavior_cell(behavior) == "long/fast/regular"
    for index, growth in enumerate((0.1, 0.2, 0.3)):
        record_candidate(
            store,
            job_id,
            candidate_id=f"candidate-{index}",
            family="momentum",
            summary="qd",
            status="dev_frontier",
            objective={
                "net_log_growth": growth,
                "downside_deviation": 0.01,
                "tail_loss": 0.01,
                "max_drawdown_pct": 0.05,
            },
            behavior=behavior,
        )
    snapshot = quality_diversity_snapshot(store, job_id)
    assert [row["candidate_id"] for row in snapshot["long/fast/regular"]] == [
        "candidate-2"
    ]


def test_candidate_bundle_is_confined_to_its_exact_campaign_slot(tmp_path) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    state = start_campaign(store, job_id, now=datetime(2026, 8, 25, 12, tzinfo=UTC))
    candidate = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="containment fixture",
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )
    expected = (store.job_dir(job_id) / candidate["bundle"]).resolve()
    assert resolve_candidate_bundle(store, job_id, candidate) == expected

    invalid = [
        {**candidate, "bundle": ""},
        {**candidate, "bundle": str(expected)},
        {**candidate, "bundle": "../job.yaml"},
        {
            **candidate,
            "bundle": (
                "research/evolution/campaigns/sibling/candidates/"
                f"{candidate['candidate_id']}"
            ),
        },
        {**candidate, "candidate_id": "different-name"},
        {**candidate, "campaign_id": "sibling"},
    ]
    for row in invalid:
        with pytest.raises(ValueError):
            resolve_candidate_bundle(store, job_id, row)

    link_id = "symlink-candidate"
    link = expected.parent / link_id
    link.symlink_to(store.job_dir(job_id), target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        resolve_candidate_bundle(
            store,
            job_id,
            {
                **candidate,
                "candidate_id": link_id,
                "bundle": str(link.relative_to(store.job_dir(job_id))),
            },
        )

    manifest = json.loads(
        (store.job_dir(job_id) / state["manifest"]).read_text(encoding="utf-8")
    )
    serialized = json.dumps(manifest)
    assert str(tmp_path / "audit") not in serialized
    assert "allocation.json" not in serialized
    assert not (tmp_path / "audit" / job_id / "evolution").exists()


def test_jsonl_features_are_frozen_at_the_campaign_cutoff(tmp_path) -> None:
    source = tmp_path / "features.jsonl"
    destination = tmp_path / "snapshot" / "features.jsonl"
    source.write_text(
        "\n".join(
            json.dumps({"timestamp": f"2026-08-25T0{hour}:00:00Z", "value": hour})
            for hour in range(3)
        )
        + "\n",
        encoding="utf-8",
    )

    assert _write_timeseries_prefix(
        source,
        destination,
        cutoff=datetime(2026, 8, 25, 1, tzinfo=UTC),
    )
    rows = [json.loads(line) for line in destination.read_text().splitlines()]
    assert [row["value"] for row in rows] == [0, 1]


def test_next_action_isolates_one_candidate_per_stage_session(tmp_path) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    working = started.replace(hour=13)
    prepare_candidate(
        store, job_id, family="breakout", summary="pipeline probe", now=working
    )

    block = campaign_prompt_block(store, job_id, now=working)
    assert block is not None
    assert block["session_stage"] == "candidate-01"
    assert 'action="evolution_evaluate"' in block["next_action"]
    assert 'action="evolution_prepare"' not in block["next_action"]
    assert f'job_id="{job_id}"' in block["next_action"]
    expected_root = (
        store.job_dir(job_id)
        / campaign_status(store, job_id)["candidates"][0]["bundle"]
    ).resolve()
    assert str(expected_root) in block["next_action"]
    assert "background=true" in block["next_action"]
    assert "Do not wait" in block["next_action"]
    assert "END THIS STAGE" in block["next_action"]
    assert block["candidate_outcomes"][0]["summary"] == "pipeline probe"

    for index in range(2):
        prepare_candidate(
            store, job_id, family="breakout", summary=f"queued {index}", now=working
        )
    drain = campaign_prompt_block(store, job_id, now=working)
    assert drain["session_stage"] == "candidate-01"
    assert 'action="evolution_evaluate"' in drain["next_action"]
    assert 'action="evolution_prepare"' not in drain["next_action"]

    state = campaign_status(store, job_id)
    manifest = json.loads(
        (store.job_dir(job_id) / state["manifest"]).read_text(encoding="utf-8")
    )
    budget = int(manifest["policy"]["generated_programs"])
    for candidate in state["candidates"]:
        candidate["status"] = "quick_complete"
    while len(state["candidates"]) < budget:
        state["candidates"].append({"status": "quick_complete"})
    store.write_json(job_id, "state/evolution_campaign.json", state)
    done = campaign_prompt_block(store, job_id, now=working)
    assert done["session_stage"] == "finalize"
    assert 'action="evolution_finalize"' in done["next_action"]
    assert f'job_id="{job_id}"' in done["next_action"]
    assert "background=true" in done["next_action"]

    # Budget exhausted with a candidate still awaiting evaluation: drain only.
    state["candidates"][0]["status"] = "prepared"
    store.write_json(job_id, "state/evolution_campaign.json", state)
    exhausted = campaign_prompt_block(store, job_id, now=working)
    assert exhausted["session_stage"] == "candidate-01"
    assert 'action="evolution_evaluate"' in exhausted["next_action"]
    assert 'action="evolution_prepare"' not in exhausted["next_action"]

    draining = campaign_prompt_block(
        store, job_id, now=datetime(2026, 8, 25, 15, 40, tzinfo=UTC)
    )
    assert draining["session_stage"] == "candidate-01"
    assert 'action="evolution_evaluate"' in draining["next_action"]

    elapsed = campaign_prompt_block(
        store, job_id, now=datetime(2026, 8, 25, 16, 30, tzinfo=UTC)
    )
    assert elapsed["session_stage"] == "candidate-01"
    assert 'action="evolution_evaluate"' in elapsed["next_action"]
    assert elapsed["deadline_elapsed"] is True

    state["candidates"][0]["status"] = "quick_complete"
    store.write_json(job_id, "state/evolution_campaign.json", state)
    elapsed = campaign_prompt_block(
        store, job_id, now=datetime(2026, 8, 25, 16, 30, tzinfo=UTC)
    )
    assert 'action="evolution_finalize"' in elapsed["next_action"]
    assert f'job_id="{job_id}"' in elapsed["next_action"]
    assert "background=true" in elapsed["next_action"]
    assert elapsed["deadline_elapsed"] is True

    state["candidates"][0]["status"] = "quick_running"
    store.write_json(job_id, "state/evolution_campaign.json", state)
    running = campaign_prompt_block(store, job_id, now=working)
    assert running == {
        "status": "blocked",
        "campaign_id": state["campaign_id"],
        "reason": f"candidate {state['candidates'][0]['candidate_id']} evaluation is running",
    }


def test_stage_context_carries_bounded_candidate_evidence(tmp_path) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    prepare_candidate(
        store,
        job_id,
        family="asymmetric-exit",
        summary="retain gains while clipping reversals",
        now=started + timedelta(minutes=5),
    )
    state = campaign_status(store, job_id)
    state["candidates"][0].update(
        {
            "status": "quick_complete",
            "objective": {"net_log_growth": 0.03},
            "behavior": {"average_hold_bars": 8.0},
            "quick": {
                "stats": {
                    "net_return": 0.031,
                    "sharpe": 1.4,
                    "trade_count": 27,
                    "equity_curve": [1] * 5_000,
                }
            },
            "evidence": "low-fidelity train screen passed",
        }
    )
    store.write_json(job_id, "state/evolution_campaign.json", state)

    block = campaign_prompt_block(store, job_id, now=started + timedelta(minutes=10))

    assert block and block["session_stage"] == "candidate-02"
    outcome = block["candidate_outcomes"][0]
    assert outcome["objective"] == {"net_log_growth": 0.03}
    assert outcome["behavior"] == {"average_hold_bars": 8.0}
    assert outcome["quick_stats"] == {
        "net_return": 0.031,
        "sharpe": 1.4,
        "trade_count": 27,
    }
    assert "equity_curve" not in json.dumps(outcome)


def test_evolution_uses_a_dedicated_stage_session(tmp_path, monkeypatch) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    store.write_json(
        job_id,
        "state/evolution_campaign.json",
        {"campaign_id": "campaign-1", "status": "active"},
    )

    class FakeClient:
        def __init__(self) -> None:
            self.prompts = []

        def healthy(self):
            return True

        def session_exists(self, session_id):
            return False

        def session_statuses(self):
            return {}

        def find_child_session(self, *, parent_id, title):
            assert title == f"job/{job_id}/evolution/campaign-1/candidate-01"
            return "evolution-session"

        def create_session(self, **kwargs):
            raise AssertionError("the stable evolution session should be reused")

        def prompt_async(self, *, session_id, text, agent):
            self.prompts.append((session_id, text, agent))
            return True

    client = FakeClient()
    monkeypatch.setattr("wayfinder_paths.jobs.worker.OPENCODE_CLIENT", client)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign.campaign_prompt_block",
        lambda store, job_id: {
            "campaign_id": "campaign-1",
            "session_stage": "candidate-01",
            "next_action": "prepare the next candidate",
        },
    )

    result = _queue_evolution_worker(store, job_id)

    assert result == {"queued": True, "session_id": "evolution-session"}
    assert len(client.prompts) == 1
    assert "PAPER-ONLY evolution stage" in client.prompts[0][1]
    assert "end the stage" in client.prompts[0][1]
    assert client.prompts[0][2] == "wayfinder-evolution-worker"
    session = store.read_json(job_id, "reports/evolution/session.json")
    assert session["session_id"] == "evolution-session"
    assert session["session_stage"] == "candidate-01"


def test_evolution_design_stage_uses_the_normal_temperature_designer(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    store.write_json(
        job_id,
        "state/evolution_campaign.json",
        {"campaign_id": "campaign-1", "status": "active"},
    )

    class FakeClient:
        def __init__(self) -> None:
            self.created: list[dict[str, Any]] = []
            self.prompts: list[tuple[str, str]] = []

        def healthy(self):
            return True

        def session_exists(self, session_id):
            return False

        def session_statuses(self):
            return {}

        def find_child_session(self, *, parent_id, title):
            assert title == f"job/{job_id}/evolution/campaign-1/design"
            return None

        def create_session(self, **kwargs):
            self.created.append(kwargs)
            return "designer-session"

        def prompt_async(self, *, session_id, text, agent):
            self.prompts.append((session_id, agent))
            return True

    client = FakeClient()
    monkeypatch.setattr("wayfinder_paths.jobs.worker.OPENCODE_CLIENT", client)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign.campaign_prompt_block",
        lambda store, job_id: {
            "campaign_id": "campaign-1",
            "session_stage": "design",
            "agent_name": "wayfinder-evolution-designer",
            "next_action": "submit the grounded design",
        },
    )

    result = _queue_evolution_worker(store, job_id)

    assert result == {"queued": True, "session_id": "designer-session"}
    assert client.created[0]["agent"] == "wayfinder-evolution-designer"
    assert client.prompts == [("designer-session", "wayfinder-evolution-designer")]


class _FakeEvolutionClient:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.prompts: list[tuple[str, str, str]] = []

    def healthy(self) -> bool:
        return True

    def session_exists(self, session_id: str) -> bool:
        return False

    def session_statuses(self) -> dict[str, Any]:
        return {}

    def find_child_session(self, *, parent_id: Any, title: str) -> str:
        assert title == f"job/{self.job_id}/evolution/campaign-1/candidate-01"
        return "evolution-session"

    def create_session(self, **kwargs: Any) -> str:
        raise AssertionError("the stable evolution session should be reused")

    def prompt_async(self, *, session_id: str, text: str, agent: str) -> bool:
        self.prompts.append((session_id, text, agent))
        return True


def test_op_completion_nudge_reuses_session_and_respects_kill_switch(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    store.write_json(
        job_id,
        "state/evolution_campaign.json",
        {"campaign_id": "campaign-1", "status": "active"},
    )
    client = _FakeEvolutionClient(job_id)
    monkeypatch.setattr("wayfinder_paths.jobs.worker.OPENCODE_CLIENT", client)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign.campaign_prompt_block",
        lambda store, job_id: {
            "campaign_id": "campaign-1",
            "session_stage": "candidate-01",
            "next_action": "prepare the next candidate",
        },
    )

    result = nudge_evolution_session(store, job_id)

    assert result == {"queued": True, "session_id": "evolution-session"}
    assert len(client.prompts) == 1
    session = store.read_json(job_id, "reports/evolution/session.json")
    assert session["session_id"] == "evolution-session"
    latest = store.read_json(job_id, "reports/evolution/latest.json")
    assert latest["source"] == "op_completion"
    journal = store.job_dir(job_id) / "journal.jsonl"
    entry = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["type"] == "evolution_worker_wakeup"
    assert entry["source"] == "op_completion"

    # No nudge without an active campaign.
    store.write_json(
        job_id,
        "state/evolution_campaign.json",
        {"campaign_id": "campaign-1", "status": "complete"},
    )
    assert nudge_evolution_session(store, job_id) is None
    assert len(client.prompts) == 1

    # Kill-switch disables op-completion nudges entirely.
    store.write_json(
        job_id,
        "state/evolution_campaign.json",
        {"campaign_id": "campaign-1", "status": "active"},
    )
    monkeypatch.setenv("WAYFINDER_EVOLUTION_NUDGE", "0")
    assert nudge_evolution_session(store, job_id) is None
    assert len(client.prompts) == 1


def test_design_completion_retries_only_while_stage_transition_is_busy(
    monkeypatch,
) -> None:
    calls: list[str] = []
    sleeps: list[int] = []

    def nudge(store, job_id):
        calls.append(job_id)
        if len(calls) == 1:
            return {"transition_pending": True}
        return {"queued": True}

    monkeypatch.setattr("wayfinder_paths.jobs.worker.nudge_evolution_session", nudge)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.execution.op_runner.time.sleep", sleeps.append
    )

    _nudge_evolution("evolution_design", {"job_id": "majors-5m-lab"})

    assert calls == ["majors-5m-lab", "majors-5m-lab"]
    assert sleeps == [1]


def test_parentless_evolution_nudges_reuse_persisted_campaign_session(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    store.write_json(
        job_id,
        "state/evolution_campaign.json",
        {"campaign_id": "campaign-1", "status": "active"},
    )
    actions = iter(["prepare c1", "edit c1", "edit c1"])
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign.campaign_prompt_block",
        lambda store, job_id: {
            "campaign_id": "campaign-1",
            "session_stage": "candidate-01",
            "next_action": next(actions),
        },
    )
    monkeypatch.delenv("OPENCODE_SESSION_ID", raising=False)
    monkeypatch.delenv("OPENCODE_SESSIONID", raising=False)

    class ParentlessClient:
        def __init__(self) -> None:
            self.created = 0
            self.prompts: list[str] = []
            self.sessions: set[str] = set()

        def healthy(self) -> bool:
            return True

        def session_exists(self, session_id: str) -> bool:
            return session_id in self.sessions

        def session_statuses(self) -> dict[str, Any]:
            return {}

        def find_child_session(self, *, parent_id: Any, title: str) -> None:
            assert parent_id is None
            return None

        def create_session(self, **kwargs: Any) -> str:
            self.created += 1
            session_id = f"session-{self.created}"
            self.sessions.add(session_id)
            return session_id

        def prompt_async(self, *, session_id: str, text: str, agent: str) -> bool:
            self.prompts.append(session_id)
            return True

    client = ParentlessClient()
    monkeypatch.setattr("wayfinder_paths.jobs.worker.OPENCODE_CLIENT", client)

    first = _queue_evolution_worker(store, job_id)
    second = nudge_evolution_session(store, job_id)
    duplicate = nudge_evolution_session(store, job_id)

    assert first == {"queued": True, "session_id": "session-1"}
    assert second == {"queued": True, "session_id": "session-1"}
    assert duplicate == {
        "queued": False,
        "session_id": "session-1",
        "deduplicated": True,
    }
    assert client.created == 1
    assert client.prompts == ["session-1", "session-1"]


def test_evolution_repair_rotates_to_a_fresh_attempt_session(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    store.write_json(
        job_id,
        "state/evolution_campaign.json",
        {
            "campaign_id": "campaign-1",
            "status": "active",
            "candidates": [
                {
                    "slot": 1,
                    "attempts": [
                        {"postmortem": {"behavior_diff": {"material_change": True}}}
                    ],
                }
            ],
        },
    )
    store.write_json(
        job_id,
        "reports/evolution/session.json",
        {
            "schema_version": "1.2",
            "campaign_id": "campaign-1",
            "session_stage": "candidate-01",
            "artifact_key": "candidate-01-attempt-01",
            "session_id": "session-1",
        },
    )

    class RepairClient:
        def __init__(self) -> None:
            self.sessions = {"session-1"}
            self.deleted: list[str] = []

        def healthy(self) -> bool:
            return True

        def session_exists(self, session_id: str) -> bool:
            return session_id in self.sessions

        def session_statuses(self) -> dict[str, Any]:
            return {}

        def delete_session(self, session_id: str) -> bool:
            self.deleted.append(session_id)
            self.sessions.discard(session_id)
            return True

        def find_child_session(self, *, parent_id: Any, title: str) -> None:
            assert title.endswith("/candidate-01-attempt-02")
            return None

        def create_session(self, **kwargs: Any) -> str:
            self.sessions.add("session-2")
            return "session-2"

        def prompt_async(self, **kwargs: Any) -> bool:
            return True

    client = RepairClient()
    monkeypatch.setattr("wayfinder_paths.jobs.worker.OPENCODE_CLIENT", client)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign.campaign_prompt_block",
        lambda store, job_id: {
            "campaign_id": "campaign-1",
            "session_stage": "candidate-01",
            "artifact_key": "candidate-01-attempt-02",
            "next_action": "repair candidate one",
            "postmortem_path": "attempts/c01/a01/postmortem.json",
        },
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.benchmarks.agent_adapter.meter_session_ids",
        lambda session_ids: {"sessions": 1, "tool_calls": 2},
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.benchmarks.agent_adapter.session_diagnostic_summary",
        lambda session_id: {
            "tool_calls": [],
            "final_assistant_text": "attempt one evaluated",
        },
    )

    result = nudge_evolution_session(store, job_id)

    assert result == {"queued": True, "session_id": "session-2"}
    assert client.deleted == ["session-1"]
    current = store.read_json(job_id, "reports/evolution/session.json")
    assert current["artifact_key"] == "candidate-01-attempt-02"
    retired = store.read_json(job_id, "reports/evolution/sessions.json")["sessions"]
    assert retired[0]["artifact_key"] == "candidate-01-attempt-01"
    assert retired[0]["behavior_changed"] is True


def test_evaluation_completion_rotates_stage_and_passes_bounded_handoff(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    store.write_json(
        job_id,
        "state/evolution_campaign.json",
        {"campaign_id": "campaign-1", "status": "active"},
    )
    store.write_json(
        job_id,
        "reports/evolution/session.json",
        {
            "schema_version": "1.1",
            "campaign_id": "campaign-1",
            "session_stage": "candidate-01",
            "session_id": "session-1",
            "created_at": "2026-08-25T12:00:00+00:00",
        },
    )

    class StageClient:
        def __init__(self) -> None:
            self.sessions = {"session-1"}
            self.deleted: list[str] = []
            self.prompts: list[tuple[str, str]] = []

        def healthy(self) -> bool:
            return True

        def session_exists(self, session_id: str) -> bool:
            return session_id in self.sessions

        def session_statuses(self) -> dict[str, Any]:
            return {}

        def find_child_session(self, *, parent_id: Any, title: str) -> None:
            assert title.endswith("/candidate-02")
            return None

        def create_session(self, **kwargs: Any) -> str:
            self.sessions.add("session-2")
            return "session-2"

        def delete_session(self, session_id: str) -> bool:
            self.deleted.append(session_id)
            self.sessions.discard(session_id)
            return True

        def prompt_async(self, *, session_id: str, text: str, agent: str) -> bool:
            self.prompts.append((session_id, text))
            return True

    client = StageClient()
    monkeypatch.setattr("wayfinder_paths.jobs.worker.OPENCODE_CLIENT", client)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign.campaign_prompt_block",
        lambda store, job_id: {
            "campaign_id": "campaign-1",
            "session_stage": "candidate-02",
            "next_action": "author candidate two",
            "candidate_outcomes": [
                {
                    "candidate_id": "candidate-01",
                    "status": "quick_complete",
                    "summary": "candidate one improved drawdown",
                }
            ],
        },
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.benchmarks.agent_adapter.meter_session_ids",
        lambda session_ids: {
            "sessions": 1,
            "tokens_in": 100,
            "tokens_out": 20,
            "tokens_reasoning": 40,
            "tool_calls": 3,
            "tool_result_bytes": 500,
        },
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.benchmarks.agent_adapter.session_diagnostic_summary",
        lambda session_id: {
            "schema_version": "1.0",
            "tool_calls": [
                {
                    "tool": "wayfinder_core_jobs",
                    "action": "evolution_evaluate",
                    "status": "completed",
                    "output": "raw result must never enter the next prompt",
                }
            ],
            "omitted_tool_calls": 0,
            "final_assistant_text": "candidate one authored and evaluation launched",
        },
    )

    result = nudge_evolution_session(store, job_id)

    assert result == {"queued": True, "session_id": "session-2"}
    assert client.deleted == ["session-1"]
    assert len(client.prompts) == 1
    prompt = client.prompts[0][1]
    assert "candidate-02" in prompt
    assert "candidate one improved drawdown" in prompt
    assert "candidate one authored and evaluation launched" in prompt
    assert "tool_result_bytes" not in prompt
    assert "raw result must never enter the next prompt" not in prompt
    current = store.read_json(job_id, "reports/evolution/session.json")
    assert current["session_id"] == "session-2"
    assert current["session_stage"] == "candidate-02"
    archive = store.read_json(job_id, "reports/evolution/sessions.json")
    assert archive["sessions"][0]["session_stage"] == "candidate-01"


def test_stage_transition_waits_for_the_previous_session_to_be_idle(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    store.write_json(
        job_id,
        "state/evolution_campaign.json",
        {"campaign_id": "campaign-1", "status": "active"},
    )
    store.write_json(
        job_id,
        "reports/evolution/session.json",
        {
            "schema_version": "1.1",
            "campaign_id": "campaign-1",
            "session_stage": "candidate-01",
            "session_id": "session-1",
        },
    )

    class BusyClient:
        def healthy(self) -> bool:
            return True

        def session_exists(self, session_id: str) -> bool:
            return True

        def session_statuses(self) -> dict[str, Any]:
            return {"session-1": {"type": "busy"}}

        def abort_session(self, session_id: str) -> bool:
            pytest.fail("a stage transition must not abort active authoring")

        def delete_session(self, session_id: str) -> bool:
            pytest.fail("a busy stage must not be deleted")

    monkeypatch.setattr("wayfinder_paths.jobs.worker.OPENCODE_CLIENT", BusyClient())
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign.campaign_prompt_block",
        lambda store, job_id: {
            "campaign_id": "campaign-1",
            "session_stage": "candidate-02",
            "next_action": "author candidate two",
        },
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.benchmarks.agent_adapter.meter_session_ids",
        lambda session_ids: pytest.fail("busy session was exported prematurely"),
    )

    result = nudge_evolution_session(store, job_id)

    assert result == {
        "queued": False,
        "transition_pending": True,
        "session_id": "session-1",
        "from_stage": "candidate-01",
        "to_stage": "candidate-02",
        "error": "previous stage session is still busy",
    }


def test_watchdog_restarts_stale_idle_stage_in_a_fresh_session(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    store.write_json(
        job_id,
        "reports/evolution/session.json",
        {
            "schema_version": "1.1",
            "campaign_id": "campaign-1",
            "session_stage": "candidate-01",
            "session_id": "session-1",
            "created_at": "2026-08-25T12:00:00+00:00",
            "last_prompt_at": "2026-08-25T12:00:00+00:00",
        },
    )

    class RecoveryClient:
        def __init__(self) -> None:
            self.sessions = {"session-1"}
            self.created = 0

        def healthy(self) -> bool:
            return True

        def session_exists(self, session_id: str) -> bool:
            return session_id in self.sessions

        def session_statuses(self) -> dict[str, Any]:
            return {}

        def delete_session(self, session_id: str) -> bool:
            self.sessions.discard(session_id)
            return True

        def find_child_session(self, **kwargs: Any) -> None:
            return None

        def create_session(self, **kwargs: Any) -> str:
            self.created += 1
            session_id = f"recovery-{self.created}"
            self.sessions.add(session_id)
            return session_id

        def prompt_async(self, **kwargs: Any) -> bool:
            return True

    client = RecoveryClient()
    monkeypatch.setattr("wayfinder_paths.jobs.worker.OPENCODE_CLIENT", client)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.evolution_campaign.campaign_prompt_block",
        lambda store, job_id, now=None: {
            "campaign_id": "campaign-1",
            "session_stage": "candidate-01",
            "next_action": "finish candidate one",
        },
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.benchmarks.agent_adapter.meter_session_ids",
        lambda session_ids: {"sessions": 1},
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.benchmarks.agent_adapter.session_diagnostic_summary",
        lambda session_id: {
            "tool_calls": [],
            "final_assistant_text": "partial candidate work is persisted",
        },
    )

    recovered = recover_evolution_stage_session(
        store, job_id, now=datetime(2026, 8, 25, 12, 11, tzinfo=UTC)
    )

    assert recovered == {"queued": True, "session_id": "recovery-1"}
    session = store.read_json(job_id, "reports/evolution/session.json")
    assert session["session_id"] == "recovery-1"
    assert session["session_stage"] == "candidate-01"


def test_evolution_session_retirement_exports_before_exact_delete(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    store.write_json(
        job_id,
        "reports/evolution/session.json",
        {
            "campaign_id": "campaign-1",
            "session_id": "session-1",
            "created_at": "2026-08-25T12:00:00+00:00",
        },
    )

    class RetireClient:
        def __init__(self) -> None:
            self.aborted: list[str] = []
            self.deleted: list[str] = []

        def session_statuses(self) -> dict[str, Any]:
            return {"session-1": {"type": "busy"}}

        def abort_session(self, session_id: str) -> bool:
            self.aborted.append(session_id)
            return True

        def session_exists(self, session_id: str) -> bool:
            return True

        def delete_session(self, session_id: str) -> bool:
            self.deleted.append(session_id)
            return True

    client = RetireClient()
    monkeypatch.setattr("wayfinder_paths.jobs.worker.OPENCODE_CLIENT", client)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.benchmarks.agent_adapter.meter_session_ids",
        lambda session_ids: {
            "sessions": 1,
            "tokens_in": 123,
            "tokens_out": 7,
            "tool_result_bytes": 456,
        },
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.benchmarks.agent_adapter.session_diagnostic_summary",
        lambda session_id: {
            "schema_version": "1.0",
            "tool_calls": [
                {
                    "tool": "wayfinder_core_jobs",
                    "action": "evolution_prepare",
                    "status": "completed",
                }
            ],
            "omitted_tool_calls": 0,
            "final_assistant_text": "candidate prepared",
        },
    )

    retired = retire_evolution_session(store, job_id, "campaign-1")

    assert retired and retired["retired"] is True
    assert client.aborted == ["session-1"]
    assert client.deleted == ["session-1"]
    archive = store.read_json(job_id, "reports/evolution/sessions.json")
    assert archive["sessions"][0]["metrics"]["tool_result_bytes"] == 456
    assert archive["sessions"][0]["diagnostics"]["tool_calls"][0]["action"] == (
        "evolution_prepare"
    )
    assert archive["sessions"][0]["retired_at"]


def test_evolution_session_retirement_refuses_unresolved_metering(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    store.write_json(
        job_id,
        "reports/evolution/session.json",
        {"campaign_id": "campaign-1", "session_id": "session-1"},
    )

    class RetireClient:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def session_statuses(self) -> dict[str, Any]:
            return {}

        def session_exists(self, session_id: str) -> bool:
            return True

        def delete_session(self, session_id: str) -> bool:
            self.deleted.append(session_id)
            return True

    client = RetireClient()
    monkeypatch.setattr("wayfinder_paths.jobs.worker.OPENCODE_CLIENT", client)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.benchmarks.agent_adapter.meter_session_ids",
        lambda session_ids: {"sessions": 0},
    )

    retired = retire_evolution_session(store, job_id, "campaign-1")

    assert retired and retired["retired"] is False
    assert "exact persisted session" in retired["error"]
    assert client.deleted == []
    assert not (store.job_dir(job_id) / "reports/evolution/sessions.json").exists()


def test_evolution_session_retirement_refuses_failed_diagnostic_export(
    tmp_path, monkeypatch
) -> None:
    store, job_id = _job(tmp_path, "majors-5m-lab")
    store.write_json(
        job_id,
        "reports/evolution/session.json",
        {"campaign_id": "campaign-1", "session_id": "session-1"},
    )

    class RetireClient:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def session_statuses(self) -> dict[str, Any]:
            return {}

        def session_exists(self, session_id: str) -> bool:
            return True

        def delete_session(self, session_id: str) -> bool:
            self.deleted.append(session_id)
            return True

    client = RetireClient()
    monkeypatch.setattr("wayfinder_paths.jobs.worker.OPENCODE_CLIENT", client)
    monkeypatch.setattr(
        "wayfinder_paths.jobs.benchmarks.agent_adapter.meter_session_ids",
        lambda session_ids: {"sessions": 1},
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.benchmarks.agent_adapter.session_diagnostic_summary",
        lambda session_id: (_ for _ in ()).throw(RuntimeError("database locked")),
    )

    retired = retire_evolution_session(store, job_id, "campaign-1")

    assert retired and retired["retired"] is False
    assert retired["error"] == "session diagnostic export failed: database locked"
    assert client.deleted == []
    assert not (store.job_dir(job_id) / "reports/evolution/sessions.json").exists()


_EVAL_SPEC = {
    "market_kind": "perp",
    "data_contract": {"bar_interval": "1h", "symbols": ["IMX"]},
}


def _hourly_bars(count: int) -> list[dict[str, Any]]:
    rows = []
    for index in range(count):
        price = 10.0 + index * 0.05
        stamp = datetime(2026, 8, 1, tzinfo=UTC) + timedelta(hours=index)
        rows.append(
            {
                "timestamp": stamp.isoformat(),
                "symbol": "IMX",
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 100.0,
            }
        )
    return rows


def _evaluatable_job(
    tmp_path: Any, *, source_params: dict[str, Any] | None = None
) -> tuple[JobStore, str]:
    """Like _job but with a real spec, runnable strategy, and enough bars for
    the low-fidelity screen — so evaluate_candidate runs end to end."""
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "majors-5m-lab",
        name="Majors momentum lab",
        goal="find robust momentum and factor strategies",
        script="workspace/src/strategy.py",
        agent_mode="intervene",
        execution_contract="jobs_v1",
        interval_seconds=3600,
    )
    job.execution_spec = dict(_EVAL_SPEC)
    job.execution_params = {
        "symbols": ["IMX"],
        "initial_capital": 1_000.0,
        **(source_params or {}),
    }
    store.save(job)
    root = store.job_dir(job.id)
    script = root / "workspace" / "src" / "strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("def decide(ctx):\n    return []\n", encoding="utf-8")
    bars = root / "results" / "backtest" / "input_bars.json"
    bars.parent.mkdir(parents=True, exist_ok=True)
    bars.write_text(
        json.dumps({"metadata": {"days": 120}, "bars": _hourly_bars(60)}),
        encoding="utf-8",
    )
    (root / "improver.yaml").write_text(
        yaml.safe_dump(
            {
                "evolution": {
                    "generated_programs": 12,
                    "investigation_design_enabled": False,
                }
            }
        ),
        encoding="utf-8",
    )
    return store, job.id


def _read_bundle_params(store: JobStore, job_id: str, bundle: str) -> dict[str, Any]:
    data = yaml.safe_load(
        (store.job_dir(job_id) / bundle / "job.yaml").read_text(encoding="utf-8")
    )
    return dict(data["execution_params"])


def test_prepare_candidate_seeds_declared_window(tmp_path) -> None:
    store, job_id = _evaluatable_job(tmp_path / "default")
    start_campaign(store, job_id, now=datetime(2026, 8, 25, 12, tzinfo=UTC))
    candidate = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="seed default",
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )
    params = _read_bundle_params(store, job_id, candidate["bundle"])
    assert params["warmup_bars"] == DEFAULT_WARMUP_BARS
    assert candidate["warmup_bars"] == DEFAULT_WARMUP_BARS
    # The frozen source was seeded identically, so an UNEDITED candidate still
    # matches the baseline revision (the identical-to-source guard holds).
    state = campaign_status(store, job_id)
    manifest = json.loads(
        (store.job_dir(job_id) / state["manifest"]).read_text(encoding="utf-8")
    )
    bundle_root = store.job_dir(job_id) / candidate["bundle"]
    assert (
        compute_workspace_revision(bundle_root) == manifest["source_bundle"]["revision"]
    )

    # A source that already declared a window (legacy lookback_bars) is
    # inherited, never overwritten with the default.
    inherit_store, inherit_id = _evaluatable_job(
        tmp_path / "inherit", source_params={"lookback_bars": 48}
    )
    start_campaign(inherit_store, inherit_id, now=datetime(2026, 8, 25, 12, tzinfo=UTC))
    inherited = prepare_candidate(
        inherit_store,
        inherit_id,
        family="breakout",
        summary="seed inherited",
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )
    params = _read_bundle_params(inherit_store, inherit_id, inherited["bundle"])
    assert params["warmup_bars"] == 48


def test_evaluate_rejects_undeclared_window_candidate(tmp_path) -> None:
    """A candidate that dodges the bounded-window contract (full_history)
    is invalid with the bounded-window hint as the reason."""
    store, job_id = _evaluatable_job(tmp_path)
    start_campaign(store, job_id, now=datetime(2026, 8, 25, 12, tzinfo=UTC))
    candidate = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="undeclared window",
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )
    bundle = store.job_dir(job_id) / candidate["bundle"]
    data = yaml.safe_load((bundle / "job.yaml").read_text(encoding="utf-8"))
    data["execution_params"].pop("warmup_bars")
    data["execution_params"]["full_history"] = True
    data["execution_params"]["lookback_bars"] = 20
    (bundle / "job.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )

    result = evaluate_candidate(store, job_id, candidate["candidate_id"])

    assert result["status"] == "invalid"
    evidence = json.dumps(result["evidence"])
    assert "warmup_bars" in evidence
    assert "cannot exist in production" in evidence


def test_evaluate_rejects_zero_trade_candidate_and_archives_quick_result(
    tmp_path,
) -> None:
    store, job_id = _evaluatable_job(tmp_path)
    start_campaign(store, job_id, now=datetime(2026, 8, 25, 12, tzinfo=UTC))
    candidate = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="seeded and edited",
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )
    script = (
        store.job_dir(job_id)
        / candidate["bundle"]
        / "workspace"
        / "src"
        / "strategy.py"
    )
    script.write_text(
        script.read_text(encoding="utf-8") + "\nSTRUCTURAL_PROBE = True\n",
        encoding="utf-8",
    )

    result = evaluate_candidate(store, job_id, candidate["candidate_id"])

    assert result["status"] == "low_fidelity_rejected"
    assert result["evidence"] == "quick screen produced no closed trades"
    entry = next(
        row
        for row in load_archive(store, job_id)["candidates"]
        if row["candidate_id"] == candidate["candidate_id"]
    )
    assert entry["status"] == "low_fidelity_rejected"
    assert entry["metadata"]["quick"]["stats"]["trade_count"] == 0
    assert quality_diversity_snapshot(store, job_id) == {}


def test_evaluate_rejects_parameter_candidate_without_typed_search_space(
    tmp_path,
) -> None:
    store, job_id = _evaluatable_job(tmp_path)
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    candidates = _prepare_campaign_candidates(store, job_id, started)
    parameter = candidates[-1]
    assert parameter["mutation_kind"] == "parameter"

    result = evaluate_candidate(store, job_id, parameter["candidate_id"])

    # A deterministic authoring mistake: nothing ran, nothing was charged.
    assert result["status"] == "prepared"
    assert "requires search_space.json" in result["submission_rejection"]["error"]
    assert result["submission_rejection"]["attempt_charged"] is False
    assert not result.get("attempts") and int(result.get("attempt_count") or 0) == 0
    assert int(campaign_status(store, job_id)["counts"].get("quick_attempts") or 0) == 0
    entry = next(
        row
        for row in load_archive(store, job_id)["candidates"]
        if row["candidate_id"] == parameter["candidate_id"]
    )
    assert entry["status"] == "generated"
    handoff = _candidate_handoff(result)
    assert "resubmit" in handoff["submission_rejection"]["instruction"]


def test_evaluate_rejects_oversized_candidate_search_space(tmp_path) -> None:
    store, job_id = _evaluatable_job(tmp_path)
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    candidates = _prepare_campaign_candidates(store, job_id, started)
    parameter = candidates[-1]
    bundle = store.job_dir(job_id) / parameter["bundle"]
    (bundle / "search_space.json").write_text(
        json.dumps(
            {
                f"knob_{index}": {"type": "int", "low": 1, "high": 10}
                for index in range(4)
            }
        ),
        encoding="utf-8",
    )

    result = evaluate_candidate(store, job_id, parameter["candidate_id"])

    assert result["status"] == "prepared"
    assert "three-dimension evolution budget" in result["submission_rejection"]["error"]


def test_evaluate_rejects_sizing_dimensions_without_charging(tmp_path) -> None:
    store, job_id = _evaluatable_job(tmp_path)
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    candidates = _prepare_campaign_candidates(store, job_id, started)
    parameter = candidates[-1]
    bundle = store.job_dir(job_id) / parameter["bundle"]
    (bundle / "search_space.json").write_text(
        json.dumps(
            {
                "hold_hours": {"type": "int", "low": 4, "high": 24},
                "notional_fraction": {"type": "float", "low": 0.5, "high": 1.0},
            }
        ),
        encoding="utf-8",
    )

    result = evaluate_candidate(store, job_id, parameter["candidate_id"])

    assert result["status"] == "prepared"
    assert (
        "sizing dimensions are not search dimensions"
        in (result["submission_rejection"]["error"])
    )
    assert "notional_fraction" in result["submission_rejection"]["error"]


def test_behavior_preview_rejects_noop_parameter_before_simulation(
    tmp_path, monkeypatch
) -> None:
    import wayfinder_paths.jobs.evolution_campaign as campaign_module

    store, job_id = _evaluatable_job(tmp_path, source_params={"warmup_bars": 20})
    improver = store.job_dir(job_id) / "improver.yaml"
    policy = yaml.safe_load(improver.read_text(encoding="utf-8"))
    policy["evolution"]["behavior_preview_enabled"] = True
    improver.write_text(yaml.safe_dump(policy), encoding="utf-8")
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    parameter = _prepare_campaign_candidates(store, job_id, started)[-1]
    assert parameter["mutation_kind"] == "parameter"
    bundle = store.job_dir(job_id) / parameter["bundle"]
    job_yaml = yaml.safe_load((bundle / "job.yaml").read_text(encoding="utf-8"))
    job_yaml["execution_params"]["warmup_bars"] = 20
    (bundle / "job.yaml").write_text(
        yaml.safe_dump(job_yaml, sort_keys=False), encoding="utf-8"
    )
    (bundle / "search_space.json").write_text(
        json.dumps({"unused_knob": {"type": "int", "low": 1, "high": 2}}),
        encoding="utf-8",
    )

    def simulation_must_not_run(*args, **kwargs):
        raise AssertionError("full quick simulation ran before behavior preview")

    monkeypatch.setattr(campaign_module, "simulate_execution", simulation_must_not_run)

    result = evaluate_candidate(store, job_id, parameter["candidate_id"])

    assert result["status"] == "low_fidelity_rejected", json.dumps(result, indent=2)
    assert result["quick_simulation_ran"] is False
    assert result["behavior_preview"]["status"] == "unchanged"
    assert result["postmortem"]["primary_failure"] == "no_behavior_change"
    assert result["postmortem"]["behavior_diff"]["material_change"] is False


def test_evaluate_releases_claim_when_compute_is_transiently_busy(
    tmp_path, monkeypatch
) -> None:
    import wayfinder_paths.jobs.evolution_campaign as campaign_module

    store, job_id = _evaluatable_job(tmp_path)
    start_campaign(store, job_id, now=datetime(2026, 8, 25, 12, tzinfo=UTC))
    candidate = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="transient compute probe",
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )

    @contextmanager
    def busy_compute(*args, **kwargs):
        raise ComputeLockBusy("box busy")
        yield

    monkeypatch.setattr(campaign_module, "experiment_compute_lock", busy_compute)

    with pytest.raises(ComputeLockBusy, match="box busy"):
        evaluate_candidate(store, job_id, candidate["candidate_id"])

    state = campaign_status(store, job_id)
    recovered = state["candidates"][0]
    assert recovered["status"] == "quick_failed"
    assert "evaluation_claim_id" not in recovered
    assert state["counts"]["quick_evaluated"] == 0


def test_lost_evaluator_claim_is_released_for_a_fresh_stage(tmp_path) -> None:
    store, job_id = _evaluatable_job(tmp_path)
    start_campaign(store, job_id, now=datetime(2026, 8, 25, 12, tzinfo=UTC))
    candidate = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="recover after machine restart",
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )
    state = campaign_status(store, job_id)
    state["candidates"][0].update(
        {
            "status": "quick_running",
            "evaluation_claim_id": "lost-claim",
            "evaluation_claimed_at": "2026-08-25T13:05:00+00:00",
        }
    )
    store.write_json(job_id, "state/evolution_campaign.json", state)

    recovered = recover_lost_candidate_evaluations(
        store,
        job_id,
        reason="detached evaluator exited during machine roll",
        now=datetime(2026, 8, 25, 13, 6, tzinfo=UTC),
    )

    assert recovered == [candidate["candidate_id"]]
    current = campaign_status(store, job_id)["candidates"][0]
    assert current["status"] == "quick_failed"
    assert "evaluation_claim_id" not in current
    assert current["evaluation_recovery_reason"].endswith("machine roll")
    assert "evolution_candidate_evaluation_recovered" in (
        store.job_dir(job_id) / "journal.jsonl"
    ).read_text(encoding="utf-8")


def test_finalize_refuses_to_skip_a_pending_candidate_stage(tmp_path) -> None:
    store, job_id = _evaluatable_job(tmp_path)
    start_campaign(store, job_id, now=datetime(2026, 8, 25, 12, tzinfo=UTC))
    candidate = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="must be evaluated before finalization",
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )

    with pytest.raises(TransientInfrastructureError, match=candidate["candidate_id"]):
        finalize_campaign(store, job_id)

    assert campaign_status(store, job_id)["status"] == "active"


def test_full_dev_runs_real_small_simulations_in_disposable_child(tmp_path) -> None:
    store, job_id = _evaluatable_job(tmp_path)
    start_campaign(store, job_id, now=datetime(2026, 8, 25, 12, tzinfo=UTC))
    candidate = prepare_candidate(
        store,
        job_id,
        family="breakout",
        summary="real isolated full-dev probe",
        now=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )
    script = (
        store.job_dir(job_id)
        / candidate["bundle"]
        / "workspace"
        / "src"
        / "strategy.py"
    )
    script.write_text(
        script.read_text(encoding="utf-8") + "\nFULL_DEV_PROBE = True\n",
        encoding="utf-8",
    )

    outcome = _isolated_full_dev(store, job_id, candidate, tune=False)

    assert outcome["status"] in {"dev_frontier", "low_fidelity_rejected"}
    resources = store.read_json(job_id, "reports/evolution/resources.json")
    assert resources["latest"]["phase"] == "full_dev"
    assert resources["latest"]["candidate_id"] == candidate["candidate_id"]


def test_full_dev_optuna_uses_bounded_train_tail_and_timeout(
    tmp_path, monkeypatch
) -> None:
    import wayfinder_paths.jobs.evolution_campaign as campaign_module

    store, job_id = _evaluatable_job(tmp_path)
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    state = start_campaign(store, job_id, now=started)
    candidates = _prepare_campaign_candidates(store, job_id, started)
    parameter = candidates[-1]
    bundle = store.job_dir(job_id) / parameter["bundle"]
    (bundle / "search_space.json").write_text(
        json.dumps({"lookback": {"type": "int", "low": 12, "high": 96}}),
        encoding="utf-8",
    )
    manifest_path = store.job_dir(job_id) / state["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["policy"].update(
        {
            "inner_optuna_trials": 5,
            "inner_optuna_train_bars": 20,
            "inner_optuna_timeout_seconds": 17,
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_search(script, dataset, spec, search_space, **kwargs):
        captured.update({"dataset": dataset, "search_space": search_space, **kwargs})
        params = {
            name: 48 if name == "lookback" else value
            for name, value in search_space.items()
            if not isinstance(value, dict)
        }
        params["lookback"] = 48
        return SimpleNamespace(
            runs=[{} for _ in range(5)],
            invalid=[{}],
            ranked=[
                {
                    "params": params,
                    "stats": {"net_return": 0.01, "max_drawdown_pct": -0.01},
                }
            ],
        )

    monkeypatch.setattr(campaign_module, "run_optuna_search", fake_search)

    outcome = campaign_module._full_dev(store, job_id, parameter, tune=True)

    assert len(captured["dataset"].bars.timestamps) == 20
    assert captured["n_trials"] == 5
    assert captured["seed"] == 42
    assert captured["timeout"] == 17
    assert outcome["tuning"] == {
        "plateau": {"neighbors_of_best": 0, "ratio": None, "selected_by": "ranked"},
        "status": "complete",
        "risk_pruned": 0,
        "max_drawdown_pct": 0.2,
        "trials": 5,
        "valid_trials": 4,
        "bars": 20,
        "seed": 42,
        "wall_seconds": outcome["tuning"]["wall_seconds"],
        "selected_params": {"lookback": 48},
    }


def test_casebook_includes_bounded_window_parity_case() -> None:
    cases = {case["id"] for case in load_starter_casebook()}
    assert "bounded-window-parity" in cases
    selected = select_starter_cases({"warmup", "performance"})
    assert len(selected) <= MAX_PROMPT_CASES
    assert any(case["id"] == "bounded-window-parity" for case in selected)


def test_op_runner_nudges_after_evolution_ops_and_contains_failures(
    monkeypatch, capsys
) -> None:
    from wayfinder_paths.jobs.execution import op_runner

    nudged: list[str] = []
    synced: list[str] = []
    monkeypatch.setattr(op_runner, "_lower_priority", lambda: None)
    monkeypatch.setattr(op_runner, "_run", lambda op, kwargs: {"ok": True})
    monkeypatch.setattr(
        "wayfinder_paths.jobs.worker.nudge_evolution_session",
        lambda store, job_id: nudged.append(job_id),
    )
    monkeypatch.setattr(
        "wayfinder_paths.jobs.sync.sync_all_jobs",
        lambda *, store: synced.append("sync"),
    )

    def run_main(op: str, kwargs: dict[str, Any]) -> None:
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(json.dumps({"op": op, "kwargs": kwargs}))
        )
        op_runner.main()

    run_main("evolution_evaluate", {"job_id": "majors-5m-lab", "candidate_id": "c1"})
    # evolution_start completion nudges too: the first campaign prompt no
    # longer waits out a full hourly wake interval.
    run_main("evolution_start", {"job_id": "majors-5m-lab"})
    run_main("evolution_finalize", {"job_id": "majors-5m-lab"})
    run_main("backtest_job", {"job_id": "majors-5m-lab"})
    assert nudged == ["majors-5m-lab", "majors-5m-lab"]
    assert synced == ["sync", "sync", "sync"]

    def boom(store: Any, job_id: str) -> None:
        raise RuntimeError("nudge failed")

    def sync_boom(*, store: Any) -> None:
        raise RuntimeError("sync failed")

    monkeypatch.setattr("wayfinder_paths.jobs.worker.nudge_evolution_session", boom)
    monkeypatch.setattr("wayfinder_paths.jobs.sync.sync_all_jobs", sync_boom)
    run_main("evolution_evaluate", {"job_id": "majors-5m-lab", "candidate_id": "c1"})
    # The op result was written all five times; failed observability hooks stay
    # inside their guards instead of failing the compute operation.
    assert capsys.readouterr().out.count('{"ok": true}') == 5


def test_cli_evolution_start_nudges_the_session(monkeypatch, tmp_path) -> None:
    """An inline CLI force-start must nudge the evolution session itself —
    it never passes through the op runner whose completion hook nudges."""
    from click.testing import CliRunner

    from wayfinder_paths.jobs import cli as cli_module

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        cli_module, "start_campaign", lambda store, job_id, force: {"status": "active"}
    )
    monkeypatch.setattr(
        cli_module,
        "nudge_evolution_session",
        lambda store, job_id: calls.append(("nudge", job_id)) or {"queued": True},
    )
    outcome = CliRunner().invoke(
        cli_module.job_cli, ["evolution-start", "job-nudge-demo", "--force"]
    )
    assert outcome.exit_code == 0, outcome.output
    assert calls == [("nudge", "job-nudge-demo")]
    assert '"queued": true' in outcome.output


def test_risk_ceiling_scale_only_for_risk_only_rejections_with_edge() -> None:
    economic = {
        "status": "ok",
        "ready": False,
        "reasons": ["OOS max drawdown 0.319 exceeds ceiling 0.25"],
        "objective": {
            "candidate": {
                "max_drawdown_pct": 0.319,
                "tail_loss": 0.074,
                "net_log_growth": 0.11,
            }
        },
        "paired_incumbent_delta": {"estimate": 0.07, "lcb": -0.1},
    }
    hard = {"max_drawdown_pct": 0.25, "max_tail_loss": 0.15}
    scale, before = _risk_ceiling_scale(economic, hard, margin=0.9)
    assert scale == pytest.approx(0.9 * 0.25 / 0.319, abs=1e-3)
    assert before["ceiling_max_drawdown_pct"] == 0.25
    summary = _gate_summary(economic, hard, {"scale": scale})
    assert summary["class"] == "risk_ceiling" and summary["implied_scale"] == scale
    assert summary["observed_max_drawdown_pct"] == 0.319
    mixed = {
        **economic,
        "reasons": [*economic["reasons"], "paired utility estimate not > 0"],
    }
    assert _risk_ceiling_scale(mixed, hard, margin=0.9) is None
    assert _gate_summary(mixed, hard, None)["class"] == "no_edge"
    flat = {**economic, "paired_incumbent_delta": {"estimate": -0.01}}
    assert _risk_ceiling_scale(flat, hard, margin=0.9) is None
    assert (
        _gate_summary({**economic, "ready": True, "reasons": []}, hard, None)["class"]
        == "staged"
    )


def _finalist_fixture(tmp_path, monkeypatch, gate_results):
    from wayfinder_paths.jobs import evolution_campaign as campaign_module

    store, job_id = _job(tmp_path, "majors-5m-lab")
    started = datetime(2026, 8, 25, 12, tzinfo=UTC)
    start_campaign(store, job_id, now=started)
    prepare_candidate(
        store,
        job_id,
        family="trend-hold",
        summary="edge with too much size",
        now=started.replace(hour=13),
    )
    state = campaign_status(store, job_id)
    state["candidates"][0].update(
        {
            "status": "quick_complete",
            "objective": {
                "net_log_growth": 0.1,
                "downside_deviation": 0.01,
                "tail_loss": 0.01,
                "max_drawdown_pct": 0.05,
            },
        }
    )
    state["counts"]["quick_evaluated"] = 1
    store.write_json(job_id, "state/evolution_campaign.json", state)
    bundle = store.job_dir(job_id) / state["candidates"][0]["bundle"]
    monkeypatch.setattr(
        campaign_module,
        "_isolated_full_dev",
        lambda *args, **kwargs: {
            "status": "dev_frontier",
            "evidence": "passed",
            "revision": compute_workspace_revision(bundle),
        },
    )
    calls: list[dict[str, Any]] = []

    def fake_gate(store_, job_id_, candidate, *, campaign_id):
        calls.append(dict(candidate))
        return dict(gate_results[min(len(calls) - 1, len(gate_results) - 1)])

    monkeypatch.setattr(campaign_module, "_isolated_economic_gate", fake_gate)
    staged: dict[str, Any] = {}

    def fake_stage(store_, job_id_, **kwargs):
        staged.update(kwargs)
        return {"status": "burn_in", "trial_id": "trial-1"}

    monkeypatch.setattr(
        "wayfinder_paths.jobs.probation.stage_evolution_probation", fake_stage
    )
    return store, job_id, calls, staged


_RISK_ONLY_REJECTION = {
    "status": "ok",
    "ready": False,
    "reasons": ["OOS max drawdown 0.319 exceeds ceiling 0.25"],
    "objective": {
        "candidate": {
            "max_drawdown_pct": 0.319,
            "tail_loss": 0.074,
            "net_log_growth": 0.11,
            "trade_count": 12,
        }
    },
    "paired_incumbent_delta": {"estimate": 0.07, "lcb": -0.1},
}
_SIZED_READY = {
    "status": "ok",
    "ready": True,
    "reasons": [],
    "objective": {
        "candidate": {
            "max_drawdown_pct": 0.2,
            "tail_loss": 0.05,
            "net_log_growth": 0.09,
            "trade_count": 12,
        }
    },
    "paired_incumbent_delta": {"estimate": 0.06, "lcb": -0.05},
}


def test_finalize_sizes_a_risk_only_rejection_to_the_ceiling(
    tmp_path, monkeypatch
) -> None:
    from wayfinder_paths.jobs.archive import find_candidate

    store, job_id, calls, staged = _finalist_fixture(
        tmp_path, monkeypatch, [_RISK_ONLY_REJECTION, _SIZED_READY]
    )

    result = finalize_campaign(store, job_id)

    candidate = result["candidates"][0]
    assert candidate["status"] == "probation"
    assert len(calls) == 2
    bundle = store.job_dir(job_id) / candidate["bundle"]
    job_yaml = yaml.safe_load((bundle / "job.yaml").read_text(encoding="utf-8"))
    assert job_yaml["execution_params"]["size_scale"] == pytest.approx(
        0.9 * 0.25 / 0.319, abs=1e-3
    )
    assert candidate["revision"] == compute_workspace_revision(bundle)
    assert candidate["revision"] != calls[0]["revision"]
    assert calls[1]["revision"] == candidate["revision"]
    assert staged["revision"] == candidate["revision"]
    normalization = candidate["risk_normalization"]
    assert normalization["applied"] is True and normalization["class"] == "risk_ceiling"
    assert normalization["before"]["max_drawdown_pct"] == 0.319
    assert normalization["after"]["max_drawdown_pct"] == 0.2
    assert candidate["gate"]["class"] == "staged"
    entry = find_candidate(store, job_id, candidate["candidate_id"])
    assert entry["metadata"]["gate"]["class"] == "staged"
    assert entry["metadata"]["risk_normalization"]["scale"] == normalization["scale"]


def test_finalize_records_no_edge_rejections_without_resizing(
    tmp_path, monkeypatch
) -> None:
    from wayfinder_paths.jobs.archive import find_candidate

    mixed = {
        **_RISK_ONLY_REJECTION,
        "reasons": [
            *_RISK_ONLY_REJECTION["reasons"],
            "paired utility estimate not > 0",
        ],
    }
    store, job_id, calls, staged = _finalist_fixture(tmp_path, monkeypatch, [mixed])

    result = finalize_campaign(store, job_id)

    candidate = result["candidates"][0]
    assert candidate["status"] == "proposal_rejected" and len(calls) == 1
    assert not staged
    bundle = store.job_dir(job_id) / candidate["bundle"]
    job_yaml = yaml.safe_load((bundle / "job.yaml").read_text(encoding="utf-8"))
    assert "size_scale" not in job_yaml["execution_params"]
    assert candidate["gate"]["class"] == "no_edge"
    assert "risk_normalization" not in candidate
    assert (
        find_candidate(store, job_id, candidate["candidate_id"])["metadata"]["gate"][
            "class"
        ]
        == "no_edge"
    )


def test_finalize_keeps_a_design_below_the_size_floor_rejected(
    tmp_path, monkeypatch
) -> None:
    hopeless = {
        **_RISK_ONLY_REJECTION,
        "reasons": ["OOS max drawdown 1.500 exceeds ceiling 0.25"],
        "objective": {
            "candidate": {
                **_RISK_ONLY_REJECTION["objective"]["candidate"],
                "max_drawdown_pct": 1.5,
            }
        },
    }
    store, job_id, calls, staged = _finalist_fixture(tmp_path, monkeypatch, [hopeless])

    result = finalize_campaign(store, job_id)

    candidate = result["candidates"][0]
    assert candidate["status"] == "proposal_rejected" and len(calls) == 1
    assert candidate["risk_normalization"]["applied"] is False
    assert candidate["risk_normalization"]["reason"] == "scale_below_floor"
    assert candidate["gate"]["class"] == "risk_ceiling"


def test_research_context_files_risk_ceiling_rejections_as_validated_edge(
    tmp_path,
) -> None:
    from wayfinder_paths.jobs.archive import evolution_lessons_block, record_candidate

    store, job_id = _job(tmp_path, "majors-5m-lab")
    record_candidate(
        store,
        job_id,
        candidate_id="sized",
        family="trend-hold",
        summary="proven edge, too much size",
        status="proposal_rejected",
        objective=None,
        evidence="OOS max drawdown 0.319 exceeds ceiling 0.25",
        metadata={
            "campaign_id": "c1",
            "gate": {
                "class": "risk_ceiling",
                "oos_net_log_growth": 0.11,
                "paired_estimate": 0.07,
                "observed_max_drawdown_pct": 0.319,
                "ceiling_max_drawdown_pct": 0.25,
                "implied_scale": 0.705,
            },
        },
    )
    record_candidate(
        store,
        job_id,
        candidate_id="flat",
        family="noise",
        summary="no edge",
        status="proposal_rejected",
        objective=None,
        evidence="paired utility estimate not > 0",
        metadata={"campaign_id": "c1", "gate": {"class": "no_edge"}},
    )

    context = _freeze_research_context(store, job_id)

    assert [row["family"] for row in context["refuted_families"]] == ["noise"]
    positive = next(
        row
        for row in context["validated_positives"]
        if row["source"] == "gate_edge_risk_ceiling"
    )
    assert positive["family"] == "trend-hold" and positive["implied_scale"] == 0.705
    assert "size <= 0.705x" in positive["evidence"]
    assert "sized (edge proven, size <= 0.705x)" in _research_context_instruction(
        context
    )
    lessons = {
        row["candidate_id"]: row
        for row in evolution_lessons_block(store, job_id)["outcomes"]
    }
    assert lessons["sized"]["gate"]["class"] == "risk_ceiling"
    assert lessons["flat"]["gate"]["class"] == "no_edge"
    state = {
        "candidates": [
            {
                "family": "trend-hold",
                "status": "proposal_rejected",
                "gate": {"class": "risk_ceiling"},
            },
            {"family": "trend-hold", "status": "low_fidelity_rejected"},
        ]
    }
    assert _same_family_nonwins(state, "trend-hold", 2) is False


def test_focus_rank_keeps_incumbent_parameter_slots_out_of_focus() -> None:
    def candidate(source: str, slot: int) -> dict[str, Any]:
        return {
            "parent_source": source,
            "slot": slot,
            "attempts": [
                {
                    "execution_valid": True,
                    "outcome": {},
                    "postmortem": {
                        "primary_failure": "cost_bleed",
                        "failure_codes": ["cost_bleed", "fees_erased_edge"],
                    },
                }
            ],
        }

    assert _focus_rank(candidate("de_novo", 5)) > _focus_rank(candidate("incumbent", 1))
    assert _focus_rank(candidate("starter_seed", 5)) > _focus_rank(
        candidate("incumbent", 1)
    )


def test_plateau_selection_rejects_an_isolated_peak_when_neighborhoods_exist() -> None:
    from types import SimpleNamespace

    runs = [
        {"trial": 0, "params": {"lookback": 20}, "net_return": 0.10},
        {"trial": 1, "params": {"lookback": 60}, "net_return": 0.06},
        {"trial": 2, "params": {"lookback": 63}, "net_return": 0.058},
        {"trial": 3, "params": {"lookback": 57}, "net_return": 0.062},
    ]
    grid = SimpleNamespace(runs=runs, ranked=[runs[0]], rank_by="net_return")
    selected, plateau = _plateau_select(grid, ["lookback"], "net_return")
    assert selected["trial"] in {1, 2, 3}
    assert plateau["selected_by"] == "plateau"
    assert plateau["isolated_peak_rejected"] is True and plateau["isolated"] is False
    lonely = SimpleNamespace(runs=runs[:1], ranked=[runs[0]], rank_by="net_return")
    selected, plateau = _plateau_select(lonely, ["lookback"], "net_return")
    assert selected["trial"] == 0 and plateau["isolated"] is True


def test_tuning_prune_drops_trials_past_the_drawdown_ceiling() -> None:
    from wayfinder_paths.jobs.execution.simulator import ExecutionGridResult

    rows = [
        {"trial": 0, "params": {"k": 1}, "net_return": 0.3, "max_drawdown_pct": -0.3},
        {"trial": 1, "params": {"k": 2}, "net_return": 0.1, "max_drawdown_pct": -0.1},
        {"trial": 2, "params": {"k": 3}, "net_return": 0.2},
    ]
    grid = ExecutionGridResult(
        grid_id="g",
        rank_by="net_return",
        runs=rows,
        ranked=[rows[0], rows[2], rows[1]],
        invalid=[],
    )
    pruned, dropped = _prune_risky_trials(grid, 0.2)
    assert dropped == 1
    assert [row["trial"] for row in pruned.ranked] == [2, 1]
    assert pruned.invalid[0]["invalid_reason"] == "drawdown_over_tuning_ceiling"
    assert _prune_risky_trials(grid, 0.5)[1] == 0


def _ideation_artifact(store, job_id, *, sources: int = 3) -> None:
    doc = {
        "generated_at": "2099-08-25T12:00:00+00:00",
        "sources_consulted": [
            {
                "tool": f"results/research/file{index}.json",
                "query": "q",
                "takeaway": "t",
            }
            for index in range(sources)
        ],
        "hypotheses": [
            {
                "title": "Starved idea",
                "thesis": "needs data",
                "bucket": "starved",
                "next_step": "unlock",
            },
            {
                "title": "Fade the shakeout",
                "thesis": "x",
                "bucket": "testable",
                "next_step": "scan",
            },
            {
                "title": "Dead idea",
                "thesis": "y",
                "bucket": "refuted",
                "next_step": "evidence",
            },
        ],
    }
    path = store.job_dir(job_id) / "research" / "ideation" / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_campaign_freezes_researcher_hypotheses_into_the_pack(tmp_path) -> None:
    store, job_id = _investigative_job(tmp_path)
    _ideation_artifact(store, job_id)
    started = datetime(2099, 8, 25, 12, tzinfo=UTC)
    state = start_campaign(store, job_id, now=started)

    pack = store.read_json(job_id, str(state["diagnostic_pack"]))
    block = pack["research_ideation"]
    assert block["valid"] is True
    # Testable first, so the designer's pointer /research_ideation/hypotheses/0
    # is the one worth building on.
    assert [row["bucket"] for row in block["hypotheses"]] == [
        "testable",
        "starved",
        "refuted",
    ]
    assert block["hypotheses"][0]["title"] == "Fade the shakeout"
    manifest = store.read_json(job_id, str(state["manifest"]))
    assert (
        manifest["research_ideation"]["hypotheses"][0]["title"] == "Fade the shakeout"
    )
    prompt = campaign_prompt_block(store, job_id, now=started + timedelta(minutes=1))
    assert prompt
    assert prompt["constraints"]["research_ideation"] == ["Fade the shakeout"]
    assert "Researcher hypotheses from the latest expedition" in prompt["next_action"]
    assert "[0] Fade the shakeout" in prompt["next_action"]
    assert "/research_ideation/hypotheses/0/title" in prompt["valid_evidence_pointers"]


def test_campaign_flags_an_invalid_researcher_artifact_instead_of_offering_it(
    tmp_path,
) -> None:
    store, job_id = _investigative_job(tmp_path)
    _ideation_artifact(store, job_id, sources=1)
    started = datetime(2099, 8, 25, 12, tzinfo=UTC)
    state = start_campaign(store, job_id, now=started)

    pack = store.read_json(job_id, str(state["diagnostic_pack"]))
    assert pack["research_ideation"]["valid"] is False
    assert "hypotheses" not in pack["research_ideation"]
    prompt = campaign_prompt_block(store, job_id, now=started + timedelta(minutes=1))
    assert prompt and prompt["constraints"]["research_ideation"] is None
    assert "failed its contract (sources_consulted has 1" in prompt["next_action"]
