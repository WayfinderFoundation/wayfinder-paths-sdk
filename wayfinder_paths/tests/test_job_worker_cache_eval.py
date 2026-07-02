from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]


def load_job_worker_cache_eval():
    path = REPO / "scripts" / "eval_job_worker_cache.py"
    spec = importlib.util.spec_from_file_location("eval_job_worker_cache", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_job_worker_cache"] = module
    spec.loader.exec_module(module)
    return module


def test_job_worker_cache_eval_deterministic_contract(tmp_path: Path) -> None:
    module = load_job_worker_cache_eval()

    report = module.run_deterministic_eval(tmp_path / "out")

    assert report["status"] == "passed"
    checks = {check["name"]: check["passed"] for check in report["checks"]}
    assert checks == {
        "stable_marker_precedes_dynamic_marker": True,
        "volatile_job_timestamps_do_not_change_stable_hash": True,
        "dynamic_state_does_not_change_stable_hash": True,
        "dynamic_state_changes_dynamic_hash": True,
        "recent_journal_is_dynamic_only": True,
        "durable_memory_change_changes_stable_hash": True,
    }
    assert (tmp_path / "out" / "deterministic_report.json").exists()


def test_job_worker_cache_live_eval_uses_real_opencode_shape(
    tmp_path: Path, monkeypatch
) -> None:
    module = load_job_worker_cache_eval()
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):  # noqa: ANN001
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=f"{module.LIVE_SENTINEL}\n",
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "get_api_key", lambda: "wk_eval_live")
    monkeypatch.delenv("WAYFINDER_API_KEY", raising=False)

    report = module.run_live_eval(
        repo_root_path=tmp_path,
        output_dir=tmp_path / "out",
        model="wayfinder/deepseek-v4-pro",
        opencode_bin="/bin/opencode",
        timeout_seconds=5,
        db_path=tmp_path / "missing.db",
    )

    assert report["status"] == "passed"
    command = captured["args"][0]
    assert command[:6] == [
        "/bin/opencode",
        "run",
        "--agent",
        module.JOB_WORKER_AGENT_NAME,
        "-m",
        "wayfinder/deepseek-v4-pro",
    ]
    assert "--format" in command
    assert "--dangerously-skip-permissions" not in command
    env = captured["kwargs"]["env"]
    assert env["WAYFINDER_API_KEY"] == "wk_eval_live"
    assert os.environ.get("WAYFINDER_API_KEY") != "wk_eval_live"
    assert "wk_eval_live" not in (tmp_path / "out" / "live_report.json").read_text()
