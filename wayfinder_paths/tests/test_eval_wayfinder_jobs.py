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


def test_wayfinder_jobs_worker_prompts_scope_bash_fallback() -> None:
    module = load_eval_module()

    for case in module.WORKER_CASES:
        prompt = module.build_worker_prompt(case, iteration=1)
        assert "Use glob/read for inspection" in prompt
        assert "cat > .wayfinder/jobs/<job_id>/..." in prompt
        assert "do not use absolute paths" in prompt

    for path in (
        REPO / ".opencode" / "agents" / "wayfinder-job-worker.md",
        REPO / ".opencode" / "agents" / "wayfinder-job-auto-worker.md",
    ):
        text = path.read_text("utf-8")
        assert '"cat > .wayfinder/jobs/**": allow' in text
        assert '"*": ask' in text
        assert "Never use absolute paths" in text
        assert "reports, proposals, results, and workspace directories" in text

        bash_fallback = text.index('    "*": ask')
        bash_allow = text.index('    "cat > .wayfinder/jobs/**": allow')
        assert bash_fallback < bash_allow

        edit_deny = text.index('    "*": deny')
        edit_allow = text.index('    ".wayfinder/jobs/**": allow')
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
        '"verdict": "pass|fail"',
    ):
        assert needle in rubric
