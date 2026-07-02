from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def load_eval_module():
    path = REPO / "scripts" / "eval_wayfinder_jobs.py"
    spec = importlib.util.spec_from_file_location("eval_wayfinder_jobs", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_wayfinder_jobs"] = module
    spec.loader.exec_module(module)
    return module


def test_wayfinder_jobs_eval_validates_all_creation_types(tmp_path: Path) -> None:
    module = load_eval_module()

    for case in module.CREATION_CASES:
        workspace = tmp_path / case.id
        workspace.mkdir()
        module.create_expected_job_bundle(workspace, case)

        report = module.validate_creation_case(workspace, case)

        assert report["status"] == "passed"
        assert all(check["passed"] for check in report["checks"])


def test_wayfinder_jobs_creation_validator_requires_forward_telemetry(
    tmp_path: Path,
) -> None:
    module = load_eval_module()
    case = module.CREATION_CASES[0]
    workspace = tmp_path / case.id
    workspace.mkdir()
    module.create_expected_job_bundle(workspace, case)

    script = workspace / ".wayfinder_runs" / "eval_inputs" / "sma_rearm_strategy.py"
    script.write_text(
        "print({'status': 'ok', 'decision': 'wait'})\n",
        encoding="utf-8",
    )

    report = module.validate_creation_case(workspace, case)

    assert report["status"] == "failed"
    assert any(
        check["name"] == "script_imports_forward_recorder" and not check["passed"]
        for check in report["checks"]
    )
    assert any(
        check["name"] == "script_records_forward_run" and not check["passed"]
        for check in report["checks"]
    )


def test_wayfinder_jobs_eval_validates_two_iteration_workers(tmp_path: Path) -> None:
    module = load_eval_module()

    script_workspace = tmp_path / "script-agent"
    script_workspace.mkdir()
    script_case = module.setup_script_agent_worker_fixture(
        script_workspace,
        iteration=1,
    )
    module.write_valid_worker_artifacts(script_workspace, script_case, iteration=1)
    first = module.validate_worker_case(script_workspace, script_case, iteration=1)
    assert first["status"] == "passed"

    module.setup_script_agent_worker_fixture(script_workspace, iteration=2)
    module.write_valid_worker_artifacts(script_workspace, script_case, iteration=2)
    second = module.validate_worker_case(script_workspace, script_case, iteration=2)
    assert second["status"] == "passed"

    auto_workspace = tmp_path / "auto"
    auto_workspace.mkdir()
    auto_case = module.setup_auto_worker_fixture(auto_workspace, iteration=1)
    module.write_valid_worker_artifacts(auto_workspace, auto_case, iteration=1)
    auto_first = module.validate_worker_case(auto_workspace, auto_case, iteration=1)
    assert auto_first["status"] == "passed"

    first_report_path = (
        auto_workspace
        / ".wayfinder"
        / "jobs"
        / auto_case.job_id
        / "reports"
        / "auto"
        / "latest.json"
    )
    first_report = module.read_json(first_report_path)
    first_report["orders"] = {
        "attempted": [],
        "successful": [],
        "note": "weak edge skip",
    }
    module.write_json(first_report_path, first_report)
    structured_empty_orders = module.validate_worker_case(
        auto_workspace, auto_case, iteration=1
    )
    assert structured_empty_orders["status"] == "passed", structured_empty_orders

    module.setup_auto_worker_fixture(auto_workspace, iteration=2)
    module.write_valid_worker_artifacts(auto_workspace, auto_case, iteration=2)
    auto_second = module.validate_worker_case(auto_workspace, auto_case, iteration=2)
    assert auto_second["status"] == "passed"

    report_path = (
        auto_workspace
        / ".wayfinder"
        / "jobs"
        / auto_case.job_id
        / "reports"
        / "auto"
        / "latest.json"
    )
    report = module.read_json(report_path)
    report["orders"] = {"attempted": [], "successful": [], "note": "no intervention"}
    module.write_json(report_path, report)
    missing_intervention = module.validate_worker_case(
        auto_workspace, auto_case, iteration=2
    )
    assert missing_intervention["status"] == "failed", missing_intervention
    assert any(
        check["name"] == "strong_edge_intervenes" and not check["passed"]
        for check in missing_intervention["checks"]
    )

    unsafe_auto = module.validate_worker_case(
        auto_workspace,
        auto_case,
        iteration=2,
        log_text="Tool wayfinder_hyperliquid_place_order called",
    )
    assert unsafe_auto["status"] == "failed", unsafe_auto
    assert any(
        check["name"] == "no_real_order_tool_calls" and not check["passed"]
        for check in unsafe_auto["checks"]
    )

    complex_workspace = tmp_path / "complex-apply"
    complex_workspace.mkdir()
    complex_case = next(
        case
        for case in module.WORKER_CASES
        if case.id == "worker_script_agent_complex_apply"
    )
    module.setup_script_agent_worker_fixture(
        complex_workspace, iteration=2, case=complex_case
    )
    module.write_valid_worker_artifacts(complex_workspace, complex_case, iteration=2)
    module.approve_worker_proposal_for_application(complex_workspace, complex_case)
    module.write_valid_application_artifacts(complex_workspace, complex_case)
    complex_apply = module.validate_application_case(complex_workspace, complex_case)
    assert complex_apply["status"] == "passed", complex_apply
    assert any(
        check["name"] == "complex_apply_feedback_loop" and check["passed"]
        for check in complex_apply["checks"]
    )


def test_wayfinder_jobs_eval_validates_hard_execution_backtest(
    tmp_path: Path,
) -> None:
    module = load_eval_module()
    case = module.EXECUTION_BACKTEST_CASES[0]
    workspace = tmp_path / "execution-backtest"
    workspace.mkdir()

    module.create_expected_execution_backtest_bundle(workspace, case)
    report = module.validate_execution_backtest_case(workspace, case)

    assert report["status"] == "passed", report
    for name in (
        "execution_spec_present",
        "execution_spec_completed_bars",
        "strategy_unified_entrypoint",
        "strategy_uses_order_intent",
        "single_backtest_trace_valid",
        "grid_summary_written",
        "validation_report_passed",
        "visualization_has_entry_exit_markers",
    ):
        assert any(
            check["name"] == name and check["passed"] for check in report["checks"]
        ), name


def test_wayfinder_jobs_eval_hard_live_forces_live_judge(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = load_eval_module()
    captured: dict[str, object] = {}

    def fake_run(case, **kwargs):
        captured["case_id"] = case.id
        captured["live"] = kwargs["live"]
        captured["judge"] = kwargs["judge"]
        return {
            "case_id": case.id,
            "status": "passed",
            "kind": "execution_backtest",
        }

    monkeypatch.setattr(module, "run_creation_case", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "run_worker_case", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "run_execution_backtest_case", fake_run)
    monkeypatch.setattr(module, "resolve_wayfinder_model_env", lambda *args: None)
    monkeypatch.setattr(
        module,
        "resolve_judge_model",
        lambda requested, **kwargs: "openai/gpt-5.5",
    )

    rc = module.main(
        [
            "--hard-live",
            "--output-dir",
            str(tmp_path / "evals"),
        ]
    )

    assert rc == 0
    assert captured == {
        "case_id": "hard_execution_backtest_creation",
        "live": True,
        "judge": True,
    }


def test_wayfinder_jobs_judge_prompt_is_repo_grounded(tmp_path: Path) -> None:
    module = load_eval_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    case = module.setup_script_agent_worker_fixture(workspace, iteration=2)
    module.write_valid_worker_artifacts(workspace, case, iteration=2)
    validator = module.validate_worker_case(workspace, case, iteration=2)
    rubric = (REPO / module.JUDGE_RUBRIC).read_text("utf-8")

    prompt = module.build_jobs_judge_prompt(
        rubric_text=rubric,
        case_id="worker_script_agent_two_step:iteration_2",
        task="Run the intervention worker.",
        workspace=workspace,
        job_id=case.job_id,
        validator_report=validator,
        agent_output="FINAL ANSWER: proposal created.",
    )

    for needle in (
        "Wayfinder Jobs Eval Judge Rubric",
        'verdict": "pass|fail"',
        "wayfinder_paths/jobs/models.py",
        "wayfinder_paths/jobs/worker.py",
        ".wayfinder/jobs/eval-snx-imx-rearm/job.yaml",
        "prop_rearm_guard_v1",
        "VALIDATOR REPORT",
        "AGENT OUTPUT",
    ):
        assert needle in prompt


def test_wayfinder_jobs_eval_command_shapes() -> None:
    module = load_eval_module()
    directory = Path("/tmp/repo")

    candidate = module.build_candidate_command(
        "/bin/opencode",
        "wayfinder/deepseek-v4-pro",
        "create job",
        directory=directory,
        title="eval-title",
    )
    assert candidate[:4] == ["/bin/opencode", "run", "-m", "wayfinder/deepseek-v4-pro"]
    assert "--agent" not in candidate
    assert "--dir" in candidate
    assert "--title" in candidate

    worker = module.build_worker_command(
        "/bin/opencode",
        "wayfinder/deepseek-v4-pro",
        module.JOB_WORKER_AGENT_NAME,
        "wake up",
        directory=directory,
        title="worker-title",
    )
    assert worker[:4] == [
        "/bin/opencode",
        "run",
        "--agent",
        module.JOB_WORKER_AGENT_NAME,
    ]
    assert "--dangerously-skip-permissions" not in worker

    judge = module.build_judge_command(
        "/bin/opencode",
        "openai/gpt-5.5",
        "judge",
        directory=directory,
        title="judge-title",
    )
    assert judge[:4] == ["/bin/opencode", "run", "--agent", "wayfinder-eval-judge"]
    assert "--dir" in judge

    execution_prompt = module.build_execution_backtest_prompt(
        module.EXECUTION_BACKTEST_CASES[0]
    )
    assert "jobs_v1 execution contract" in execution_prompt
    assert "build_strategy(params)/decide(ctx)" in execution_prompt
    assert "run the local execution backtest and grid validation" in execution_prompt


def test_wayfinder_jobs_forward_telemetry_guidance() -> None:
    skill = (
        REPO / ".claude" / "skills" / "writing-wayfinder-scripts" / "SKILL.md"
    ).read_text("utf-8")
    primary = (REPO / ".opencode" / "agents" / "wayfinder.md").read_text("utf-8")

    for needle in (
        "recommended, not mandatory",
        "get_forward_recorder",
        "runs.jsonl",
        "trades.jsonl",
        "orders.jsonl",
        "fills.jsonl",
        "decide_from_snapshot",
        "intent_contract",
        "stop losses",
        "limit orders",
        "partial fills",
        "reconcile live positions",
        "Never duplicate a pending stop/limit order blindly",
        "ExecutionSpec",
        "CompletedBarsView",
        "OrderIntent",
        "BracketEngine",
        "simulate_execution",
        "run_execution_grid",
        "wayfinder job validate",
    ):
        assert needle in skill

    for needle in (
        "Before coding a script for `core_jobs`, load `/writing-wayfinder-scripts`",
        "optional forward recorder helper",
        "intent_contract",
        "scenario_plan",
        "decide_from_snapshot",
        "fallback/debug context",
        "execution-contract path",
        "CompletedBarsView",
        "OrderIntent",
        "TradeCapacity",
    ):
        assert needle in primary


def test_wayfinder_jobs_worker_prompts_scope_bash_fallback(tmp_path: Path) -> None:
    module = load_eval_module()

    for case in module.WORKER_CASES:
        prompt = module.build_worker_prompt(case, iteration=1)
        assert "Use glob/read for inspection" in prompt
        assert "cat > .wayfinder/jobs/<job_id>/..." in prompt
        assert "Normal local development tools are allowed" in prompt
        assert "Python/YAML helpers" in prompt
        if case.kind == "script_agent_worker":
            proposal_prompt = module.build_worker_prompt(case, iteration=2)
            assert 'status: "pending"' in proposal_prompt
            assert "do not set `application.status` to `queued`" in (proposal_prompt)
            assert "the SDK approval flow queues application" in proposal_prompt
            assert "intent_contract" in proposal_prompt
            assert "scenario_plan" in proposal_prompt
            apply_workspace = tmp_path / f"apply-prompt-{case.id}"
            apply_workspace.mkdir(parents=True)
            module.setup_script_agent_worker_fixture(
                apply_workspace, iteration=2, case=case
            )
            module.write_valid_worker_artifacts(apply_workspace, case, iteration=2)
            module.approve_worker_proposal_for_application(apply_workspace, case)
            apply_prompt = module.build_application_prompt(apply_workspace, case)
            proposal = module.JobStore(repo_root=apply_workspace).load_proposal(
                case.job_id,
                module.SCRIPT_AGENT_PROPOSAL_ID,
            )
            assert proposal["application"]["status"] == "applying"
            assert "validate_application" in apply_prompt
            assert "failed checks" in apply_prompt
            assert "candidate workspace" in apply_prompt
            assert "same apply wake" in apply_prompt
            assert "complex apply eval" not in apply_prompt

    worker_text = (REPO / ".opencode" / "agents" / "wayfinder-job-worker.md").read_text(
        "utf-8"
    )
    assert '    "*": allow' in worker_text
    assert "Use normal local development tools" in worker_text
    assert "Python, YAML helpers, tests, and syntax" in worker_text
    assert "Keep validation bounded" in worker_text
    assert "intent_contract" in worker_text
    assert "scenario_plan" in worker_text
    assert "validate_application" in worker_text
    assert "validation fails, read the failed checks" in worker_text
    assert "validate_job" in worker_text
    assert "wayfinder job backtest" in worker_text
    assert "decide_from_snapshot" in worker_text
    assert "violates the approved intent contract" in worker_text
    assert "`proposal.status` is only `pending`" in worker_text
    assert "Do not use `queued` for `proposal.status`" in worker_text
    assert "Never call fund-moving or order-placement tools" in worker_text
    for needle in (
        "wayfinder_hyperliquid_place_*: deny",
        "wayfinder_polymarket_place_*: deny",
        "wayfinder_onchain_swap: deny",
        "wayfinder_onchain_send: deny",
        "wayfinder_contracts_execute: deny",
    ):
        assert needle in worker_text

    auto_text = (
        REPO / ".opencode" / "agents" / "wayfinder-job-auto-worker.md"
    ).read_text("utf-8")
    assert '"cat > .wayfinder/jobs/**": allow' in auto_text
    assert '"*": ask' in auto_text
    assert "Never use absolute paths" in auto_text
    assert "reports, proposals, results, and workspace directories" in auto_text

    bash_fallback = auto_text.index('    "*": ask')
    bash_allow = auto_text.index('    "cat > .wayfinder/jobs/**": allow')
    assert bash_fallback < bash_allow

    edit_deny = auto_text.index('    "*": deny')
    edit_allow = auto_text.index('    ".wayfinder/jobs/**": allow')
    assert edit_deny < edit_allow


def test_wayfinder_jobs_auto_worker_has_research_read_surface() -> None:
    text = (REPO / ".opencode" / "agents" / "wayfinder-job-auto-worker.md").read_text(
        "utf-8"
    )

    for needle in (
        "wayfinder_core_get_wallets: allow",
        "wayfinder_core_web_search: allow",
        "wayfinder_core_web_fetch: allow",
        "wayfinder_research_*: allow",
        "wayfinder_sports_snapshot: allow",
        "wayfinder_sports_backtest_state: allow",
        "wayfinder_sports_provider: allow",
        "wayfinder_hyperliquid_search_*: allow",
        "wayfinder_hyperliquid_get_state: allow",
        "wayfinder_hyperliquid_get_trade_asset: allow",
        "wayfinder_hyperliquid_get_candles: allow",
        "wayfinder_hyperliquid_get_funding_history: allow",
        "wayfinder_hyperliquid_read_*: allow",
        "wayfinder_polymarket_read: allow",
        "wayfinder_polymarket_get_state: allow",
        "Use the available read/research suite before acting",
    ):
        assert needle in text

    deny = text.index("  wayfinder_*: deny")
    for allow in (
        "  wayfinder_core_get_wallets: allow",
        "  wayfinder_core_web_search: allow",
        "  wayfinder_research_*: allow",
        "  wayfinder_sports_provider: allow",
        "  wayfinder_hyperliquid_get_state: allow",
        "  wayfinder_polymarket_get_state: allow",
    ):
        assert deny < text.index(allow)


def test_eval_judge_supports_wayfinder_jobs_pass_fail() -> None:
    judge = (REPO / ".opencode" / "agents" / "wayfinder-eval-judge.md").read_text(
        "utf-8"
    )
    rubric = (REPO / "scripts" / "eval_jobs_judge.md").read_text("utf-8")

    for needle in (
        "Wayfinder Jobs evals",
        "generated job bundle",
        "read: allow",
        "grep: allow",
        "single-result `pass|fail` schema",
    ):
        assert needle in judge

    for needle in (
        "Wayfinder Jobs Eval Judge Rubric",
        "current SDK codebase",
        "Job schema correctness",
        "Strategy correctness",
        "intent_contract",
        "scenario_plan",
        "Execution backtest correctness",
        "completed-only bars",
        "OHLC high/low",
        '"verdict": "pass|fail"',
    ):
        assert needle in rubric


def test_improve_loop_protocol_is_pinned_in_worker_config() -> None:
    text = (REPO / ".opencode" / "agents" / "wayfinder-job-worker.md").read_text(
        "utf-8"
    )
    assert "OBSERVE → PARTITION → SCORE → DECIDE → RECORD" in text
    assert "70% CORE / 25% ADJACENT / 5% DIVERGENT" in text
    assert "TELEMETRY GATE" in text
    assert "the only valid\n   proposal this wake is a telemetry improvement" in text
    assert "never re-explore a candidate family already" in text
    assert "ledger append <job_id> candidates" in text
    assert "skeptic pass" in text
    assert "Status quo / What the data shows / Proposed change" in text
    assert "never reasoning transcripts" in text


def test_auto_loop_protocol_is_pinned_in_auto_worker_config() -> None:
    text = (REPO / ".opencode" / "agents" / "wayfinder-job-auto-worker.md").read_text(
        "utf-8"
    )
    assert "OBSERVE → RESEARCH → PARTITION → GATE → DECIDE → RECORD" in text
    assert "two-pass" in text
    assert "40% CORE / 40% ADJACENT /" in text
    assert "at most 50% of max_notional_per_decision" in text
    assert "second independent source" in text
    assert "Prefer skip over weak action. Prefer block over guessing." in text
    assert "reports/auto/latest.md" in text
    assert "`executed` if ANY entry executed" in text
    assert "ledger append <job_id>\n   decisions" in text


# ── Loop evals (exploration/exploitation) ──────────────────────────────────


def _loop_case(module, kind):
    return next(c for c in module.LOOP_CASES if c.kind == kind)


def test_loop_cases_registered_in_choices() -> None:
    module = load_eval_module()
    ids = {c.id for c in module.LOOP_CASES}
    assert ids == {"worker_improve_loop", "worker_auto_decisions"}
    assert module.selected_loop_cases("loops") == module.LOOP_CASES
    assert [c.id for c in module.selected_loop_cases("worker_improve_loop")] == [
        "worker_improve_loop"
    ]


def test_harden_sandbox_denies_place_tools_and_disables_mcp(tmp_path: Path) -> None:
    module = load_eval_module()
    ws = tmp_path / "repo"
    (ws / ".opencode" / "agents").mkdir(parents=True)
    (ws / ".opencode" / "agents" / "wayfinder-job-auto-worker.md").write_text(
        "  wayfinder_hyperliquid_place_*: allow\n"
        "  wayfinder_polymarket_place_*: allow\n"
        "  wayfinder_polymarket_redeem_positions: allow\n",
        encoding="utf-8",
    )
    import json as _json

    (ws / ".opencode" / "opencode.json").write_text(
        _json.dumps({"mcp": {"wayfinder": {"enabled": True, "url": "x"}}}),
        encoding="utf-8",
    )
    summary = module.harden_sandbox(ws)
    text = (ws / ".opencode" / "agents" / "wayfinder-job-auto-worker.md").read_text()
    assert "place_*: allow" not in text
    assert "place_*: deny" in text
    assert "redeem_positions: deny" in text
    assert summary["agent_patched"] is True
    data = _json.loads((ws / ".opencode" / "opencode.json").read_text())
    assert data["mcp"]["wayfinder"]["enabled"] is False
    assert "wayfinder" in summary["mcp_disabled"]


def test_improve_fixture_plants_a_real_discoverable_flaw(tmp_path: Path) -> None:
    """The flaw must be real: the flawed strategy loses in chop, and the
    planted fix improves held-out stats — an overfit-to-noise eval is useless."""
    module = load_eval_module()
    from wayfinder_paths.jobs.execution import ExecutionSpec
    from wayfinder_paths.jobs.execution.simulator import (
        PreparedExecutionDataset,
        simulate_execution,
    )

    script = tmp_path / "strategy.py"
    script.write_text(module.IMPROVE_STRATEGY, encoding="utf-8")
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "1h"
    base = {
        "symbol": "EVAL",
        "sma_period": 20,
        "low_period": 5,
        "range_window": 8,
        "notional_usd": 1000.0,
        "initial_capital": 10_000.0,
    }

    def run(offset, mrp):
        ds = PreparedExecutionDataset.from_rows(
            module._improve_bars(offset=offset, count=480)
        )
        return simulate_execution(script, ds, spec, {**base, "min_range_pct": mrp})

    flawed_train = run(0, 0.0)
    regime = module._regime_pnl_by_entry(flawed_train.trades)
    assert sum(regime["chop"]) < 0, "chop entries must lose (the planted flaw)"
    assert sum(regime["trend"]) > 0, "trend entries must win"

    fixed_hold = run(480, module.IMPROVE_FIX_THRESHOLD).stats
    flawed_hold = run(480, 0.0).stats
    assert fixed_hold["sharpe"] > flawed_hold["sharpe"], "fix must help HELD-OUT"
    assert fixed_hold["trade_count"] < flawed_hold["trade_count"], "fix trades less"


def test_validate_improve_round_passes_on_expected_and_fails_on_mutations(
    tmp_path: Path,
) -> None:
    module = load_eval_module()
    from wayfinder_paths.jobs.ledger import append_ledger_row
    from wayfinder_paths.jobs.store import JobStore

    ws = tmp_path / "repo"
    ws.mkdir()
    case = _loop_case(module, "improve_loop_worker")
    module.setup_improve_loop_fixture(ws, case)
    store = JobStore(repo_root=ws)
    pre = module._loop_pre_state(store, case.job_id)

    module.write_valid_improve_artifacts(ws, case, round_n=1)
    good = module.validate_improve_round(
        ws, case, round_n=1, log_text="", pre_state=pre
    )
    assert good["status"] == "passed", [c for c in good["checks"] if not c["passed"]]

    # Mutation: re-explore the seeded no_edge family -> fail.
    pre2 = module._loop_pre_state(store, case.job_id)
    append_ledger_row(
        store,
        case.job_id,
        "candidates",
        {
            "name": "bump size again",
            "family": module.SEEDED_NO_EDGE_FAMILY,
            "bucket": "adjacent",
            "status": "proposed",
        },
    )
    bad = module.validate_improve_round(
        ws, case, round_n=1, log_text="", pre_state=pre2
    )
    reexp = next(c for c in bad["checks"] if c["name"] == "no_reexploration_of_traps")
    assert reexp["passed"] is False

    # Mutation: forbidden order tool in the log -> fail.
    order_bad = module.validate_improve_round(
        ws,
        case,
        round_n=1,
        log_text="called wayfinder_hyperliquid_place_market_order",
        pre_state=pre,
    )
    assert (
        next(c for c in order_bad["checks"] if c["name"] == "no_real_order_tool_calls")[
            "passed"
        ]
        is False
    )


def test_improve_round3_telemetry_gate(tmp_path: Path) -> None:
    module = load_eval_module()
    from wayfinder_paths.jobs.store import JobStore

    ws = tmp_path / "repo"
    ws.mkdir()
    case = _loop_case(module, "improve_loop_worker")
    module.setup_improve_loop_fixture(ws, case)
    module.seed_improve_round(ws, case, 3)  # strips forward data
    store = JobStore(repo_root=ws)
    pre = module._loop_pre_state(store, case.job_id)
    module.write_valid_improve_artifacts(ws, case, round_n=3)
    result = module.validate_improve_round(
        ws, case, round_n=3, log_text="", pre_state=pre
    )
    assert result["status"] == "passed"
    assert any(
        c["name"] == "telemetry_gate_respected" and c["passed"]
        for c in result["checks"]
    )


def test_auto_world_oracle_never_leaks_and_validators_gate(tmp_path: Path) -> None:
    module = load_eval_module()
    from wayfinder_paths.jobs.store import JobStore

    ws = tmp_path / "repo"
    ws.mkdir()
    case = _loop_case(module, "auto_decision_worker")
    store = JobStore(repo_root=ws)

    for round_n in (1, 2, 3, 4):
        oracle = module.seed_auto_round(ws, case, round_n)
        # World file must exist and must NOT contain oracle keys.
        world = store.read_json(case.job_id, "state/market_world.json", default={})
        world_text = str(world)
        assert "fair_value" not in world_text
        assert "correct_action" not in world_text and "outcome_price" not in world_text
        pre = module._loop_pre_state(store, case.job_id)
        module.write_valid_auto_artifacts(ws, case, round_n=round_n, oracle=oracle)
        held = set()
        result = module.validate_auto_round(
            ws,
            case,
            round_n=round_n,
            log_text="",
            pre_state=pre,
            oracle=oracle,
            held_positions=held,
        )
        assert result["status"] == "passed", (
            round_n,
            [c for c in result["checks"] if not c["passed"]],
        )


def test_validate_auto_round_fails_on_bad_decisions(tmp_path: Path) -> None:
    module = load_eval_module()
    from wayfinder_paths.jobs.store import JobStore

    ws = tmp_path / "repo"
    ws.mkdir()
    case = _loop_case(module, "auto_decision_worker")
    store = JobStore(repo_root=ws)
    oracle = module.seed_auto_round(ws, case, 1)  # all-weak: correct = skip/block
    pre = module._loop_pre_state(store, case.job_id)

    # A false execute in round 1 (should be skipped) -> fail.
    store.write_json(
        case.job_id,
        "reports/auto/latest.json",
        {
            "status": "green",
            "summary": "bad",
            "decision": "executed",
            "orders": [
                {
                    "market_id": "m_fair",
                    "notional": 25,
                    "status": "filled",
                    "simulated": True,
                }
            ],
            "risk_limits": {},
        },
    )
    (store.job_dir(case.job_id) / "reports" / "auto" / "latest.md").write_text(
        "# Context\n## Candidates\n## Gate\n## Decision\n## Next\n", encoding="utf-8"
    )
    result = module.validate_auto_round(
        ws,
        case,
        round_n=1,
        log_text="",
        pre_state=pre,
        oracle=oracle,
        held_positions=set(),
    )
    assert result["status"] == "failed"
    assert not next(c for c in result["checks"] if c["name"] == "no_false_executes")[
        "passed"
    ]


def test_auto_round2_divergent_sizing_enforced(tmp_path: Path) -> None:
    module = load_eval_module()
    from wayfinder_paths.jobs.store import JobStore

    ws = tmp_path / "repo"
    ws.mkdir()
    case = _loop_case(module, "auto_decision_worker")
    store = JobStore(repo_root=ws)
    module.seed_auto_round(ws, case, 1)
    oracle = module.seed_auto_round(ws, case, 2)
    pre = module._loop_pre_state(store, case.job_id)

    # Oversized divergent execution -> divergent sizing check fails.
    store.write_json(
        case.job_id,
        "reports/auto/latest.json",
        {
            "status": "green",
            "summary": "oversized divergent",
            "decision": "executed",
            "orders": [
                {
                    "market_id": "m_core_fav",
                    "notional": 25,
                    "status": "filled",
                    "simulated": True,
                },
                {
                    "market_id": "m_div_narrative",
                    "notional": 25,
                    "status": "filled",
                    "simulated": True,
                },
            ],
            "risk_limits": {},
        },
    )
    (store.job_dir(case.job_id) / "reports" / "auto" / "latest.md").write_text(
        "# Context\n## Candidates\n## Gate\n## Decision\n## Next\n", encoding="utf-8"
    )
    result = module.validate_auto_round(
        ws,
        case,
        round_n=2,
        log_text="",
        pre_state=pre,
        oracle=oracle,
        held_positions=set(),
    )
    assert not next(
        c for c in result["checks"] if c["name"] == "divergent_sized_at_half_cap"
    )["passed"]
