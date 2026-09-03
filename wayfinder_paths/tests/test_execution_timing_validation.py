from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

from wayfinder_paths.jobs.execution import ExecutionSpec, OrderIntent
from wayfinder_paths.jobs.execution.simulator import (
    PreparedExecutionDataset,
    run_execution_grid,
    simulate_execution,
)
from wayfinder_paths.jobs.execution.validation import (
    validate_execution_job,
    validate_execution_trace,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


def _write_strategy(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """
from wayfinder_paths.jobs.execution import OrderIntent


class Strategy:
    def __init__(self, params):
        self.params = params

    def decide(self, ctx):
        latest = ctx.view.latest("SNX")
        threshold = float(self.params.get("threshold", 10.4))
        if not ctx.ledger.positions and float(latest["close"]) > threshold:
            return [
                OrderIntent(
                    action="OPEN",
                    venue="hyperliquid",
                    symbol="SNX",
                    side="long",
                    size=1,
                    bracket={"stop_loss": 9.0, "take_profit": 12.0},
                )
            ]
        return []


def build_strategy(params):
    return Strategy(params)
""".lstrip(),
        encoding="utf-8",
    )


def _bars(count: int = 8) -> list[dict[str, Any]]:
    rows = []
    for index in range(count):
        minute = index * 5
        price = 10.0 + index * 0.5
        rows.append(
            {
                "timestamp": f"2026-01-01T{minute // 60:02}:{minute % 60:02}:00Z",
                "symbol": "SNX",
                "open": price,
                "high": price + 0.6,
                "low": price - 0.3,
                "close": price + 0.5,
                "volume": 100,
            }
        )
    return rows


def _make_job(
    tmp_path: Path,
    *,
    interval_seconds: int | None = None,
    cron_expr: str | None = None,
    timezone: str = "UTC",
    timeout_seconds: int = 120,
    bar_interval: str | None = None,
    jobs_v1: bool = False,
) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "timing-demo",
        script=".wayfinder/jobs/timing-demo/workspace/src/strategy.py",
        interval_seconds=interval_seconds,
        cron_expr=cron_expr,
        timezone=timezone,
        timeout_seconds=timeout_seconds,
    )
    spec = ExecutionSpec()
    if bar_interval:
        spec.data_contract["bar_interval"] = bar_interval
    job.execution_spec = spec.to_dict()
    store.save(job)
    root = store.job_dir(job.id)
    _write_strategy(root / "workspace" / "src" / "strategy.py")
    if jobs_v1:
        job_yaml = root / "job.yaml"
        data = yaml.safe_load(job_yaml.read_text(encoding="utf-8"))
        data["execution_contract"] = "jobs_v1"
        job_yaml.write_text(yaml.safe_dump(data), encoding="utf-8")
    return store, job.id


def _check(report: dict[str, Any], name: str) -> dict[str, Any]:
    return next(check for check in report["checks"] if check["name"] == name)


def test_sparse_bounded_trace_uses_timestamps_for_lookahead_validation() -> None:
    trace = {
        "execution_spec": ExecutionSpec().to_dict(),
        "runs": [
            {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "visible_bar_count": 3,
                "visible_latest_timestamp": "2026-01-01T00:00:00+00:00",
            },
            {
                "timestamp": "2026-01-01T01:00:00+00:00",
                "visible_bar_count": 4,
                "visible_latest_timestamp": "2026-01-01T01:00:00+00:00",
            },
            {
                "timestamp": "2026-01-01T02:00:00+00:00",
                # A sparse row aged out of the bounded window.
                "visible_bar_count": 3,
                "visible_latest_timestamp": "2026-01-01T02:00:00+00:00",
            },
        ],
        "bracket_events": [],
        "fills": [],
        "guard_events": [],
    }
    result = validate_execution_trace(trace)
    assert result["data_valid"] is True
    assert result["execution_valid"] is True

    trace["runs"][1]["visible_latest_timestamp"] = "2026-01-01T03:00:00+00:00"
    invalid = validate_execution_trace(trace)
    assert invalid["data_valid"] is False
    assert invalid["execution_valid"] is False


def test_valid_interval_schedule_passes(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path, interval_seconds=300, bar_interval="5m")

    report = validate_execution_job(job_id, store=store)

    assert report["status"] == "passed"
    assert _check(report, "bar_interval_declared")["passed"] is True
    assert _check(report, "schedule_declared_valid")["passed"] is True
    assert _check(report, "schedule_matches_bar_interval")["passed"] is True
    assert _check(report, "timeout_vs_interval")["passed"] is True
    assert _check(report, "staleness_policy_valid")["passed"] is True


def test_interval_slower_than_bars_blocks(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path, interval_seconds=3600, bar_interval="5m")

    report = validate_execution_job(job_id, store=store)

    assert report["status"] == "failed"
    assert _check(report, "schedule_matches_bar_interval")["passed"] is False


def test_cron_period_slower_than_bars_blocks(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path, cron_expr="0 * * * *", bar_interval="5m")

    report = validate_execution_job(job_id, store=store)

    assert report["status"] == "failed"
    check = _check(report, "schedule_matches_bar_interval")
    assert check["passed"] is False
    assert check["schedule_period_seconds"] == 3600


def test_cron_faster_than_bars_passes(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path, cron_expr="*/5 * * * *", bar_interval="1h")

    report = validate_execution_job(job_id, store=store)

    assert _check(report, "schedule_matches_bar_interval")["passed"] is True


def test_invalid_cron_expression_blocks(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path, cron_expr="not a cron", bar_interval="5m")

    report = validate_execution_job(job_id, store=store)

    assert report["status"] == "failed"
    assert _check(report, "schedule_declared_valid")["passed"] is False


def test_invalid_timezone_blocks(tmp_path: Path) -> None:
    store, job_id = _make_job(
        tmp_path, cron_expr="*/5 * * * *", timezone="Mars/Olympus", bar_interval="5m"
    )

    report = validate_execution_job(job_id, store=store)

    assert report["status"] == "failed"
    assert _check(report, "schedule_declared_valid")["passed"] is False


def test_missing_schedule_blocks(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path, bar_interval="5m")

    report = validate_execution_job(job_id, store=store)

    assert report["status"] == "failed"
    assert _check(report, "schedule_declared_valid")["passed"] is False


def test_timeout_exceeding_interval_blocks(tmp_path: Path) -> None:
    store, job_id = _make_job(
        tmp_path, interval_seconds=300, timeout_seconds=300, bar_interval="5m"
    )

    report = validate_execution_job(job_id, store=store)

    assert report["status"] == "failed"
    assert _check(report, "timeout_vs_interval")["passed"] is False


def test_bar_interval_optional_for_legacy_jobs(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path, interval_seconds=300)

    report = validate_execution_job(job_id, store=store)

    assert report["status"] == "passed"
    assert _check(report, "bar_interval_declared")["passed"] is True


def test_bar_interval_required_for_jobs_v1(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path, interval_seconds=300, jobs_v1=True)

    report = validate_execution_job(job_id, store=store)

    assert report["status"] == "failed"
    check = _check(report, "bar_interval_declared")
    assert check["passed"] is False
    assert check["blocking"] is True


def test_stats_include_risk_metrics(tmp_path: Path) -> None:
    script = tmp_path / "strategy.py"
    _write_strategy(script)
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "5m"

    result = simulate_execution(
        script,
        PreparedExecutionDataset.from_rows(_bars()),
        spec,
        {"threshold": 10.4, "initial_capital": 1000},
    )

    stats = result.stats
    assert {
        "net_return",
        "ending_equity",
        "trade_count",
        "sharpe",
        "max_drawdown_pct",
        "win_rate",
        "profit_factor",
        "avg_trade_pnl",
        "exposure_pct",
    } <= set(stats)
    assert stats["trade_count"] >= 2
    assert stats["max_drawdown_pct"] <= 0
    assert 0 <= stats["win_rate"] <= 1
    assert stats["profit_factor"] is None or stats["profit_factor"] >= 0
    assert stats["avg_trade_pnl"] is not None
    assert stats["exposure_pct"] > 0
    assert stats["sharpe"] is not None


def test_grid_ranks_by_sharpe_and_rejects_unknown_keys(tmp_path: Path) -> None:
    script = tmp_path / "strategy.py"
    _write_strategy(script)
    dataset = PreparedExecutionDataset.from_rows(_bars())

    result = run_execution_grid(
        script,
        dataset,
        ExecutionSpec(),
        {"threshold": [10.4, 100.0]},
        rank_by="sharpe",
    )

    assert result.rank_by == "sharpe"
    assert len(result.runs) == 2
    assert all("sharpe" in row["stats"] for row in result.runs)

    with pytest.raises(ValueError, match="rank_by"):
        run_execution_grid(
            script,
            dataset,
            ExecutionSpec(),
            {"threshold": [10.4]},
            rank_by="bogus_metric",
        )


def test_entrypoint_inside_workspace_passes(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path, interval_seconds=300, bar_interval="5m")
    report = validate_execution_job(job_id, store=store)
    check = _check(report, "entrypoint_inside_workspace")
    assert check["passed"] is True
    assert check["blocking"] is True


def test_entrypoint_outside_workspace_blocks_jobs_v1(tmp_path: Path) -> None:
    """A job-root strategy can never be versioned or proposed — validation
    must fail with the named check (this is how live jobs got stuck with
    active_revision null)."""
    store, job_id = _make_job(tmp_path, interval_seconds=300, bar_interval="5m")
    root = store.job_dir(job_id)
    rogue = root / "strategy.py"
    rogue.write_text(
        (root / "workspace" / "src" / "strategy.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    job_yaml = root / "job.yaml"
    data = yaml.safe_load(job_yaml.read_text(encoding="utf-8"))
    data["script_loop"]["entrypoint"] = str(rogue)
    job_yaml.write_text(yaml.safe_dump(data), encoding="utf-8")

    report = validate_execution_job(job_id, store=store)

    check = _check(report, "entrypoint_inside_workspace")
    assert check["passed"] is False
    assert check["expected_dir"].endswith("workspace/src")
    assert "workspace/src" in check["hint"]
    assert report["status"] == "failed"


def _close_stop_report(tmp_path: Path, body: str) -> dict[str, Any]:
    store, job_id = _make_job(tmp_path, interval_seconds=300, bar_interval="5m")
    script = store.job_dir(job_id) / "workspace" / "src" / "strategy.py"
    script.write_text(body, encoding="utf-8")
    return validate_execution_job(job_id, store=store)


def test_close_stop_check_ignores_comments(tmp_path: Path) -> None:
    report = _close_stop_report(
        tmp_path,
        "# time stop: close if held > N days; 0 to disable\n"
        "def decide(ctx):\n    return []\n\n"
        "def build_strategy(params):\n    return None\n",
    )
    assert _check(report, "no_close_only_stop_tp")["passed"] is True


def test_close_stop_check_ignores_docstrings(tmp_path: Path) -> None:
    report = _close_stop_report(
        tmp_path,
        '"""Stop logic note: we close via brackets, not manually."""\n'
        "def decide(ctx):\n    return []\n\n"
        "def build_strategy(params):\n    return None\n",
    )
    assert _check(report, "no_close_only_stop_tp")["passed"] is True


def test_close_stop_check_still_fires_in_code(tmp_path: Path) -> None:
    report = _close_stop_report(
        tmp_path,
        "def decide(ctx):\n"
        "    stop_hit = ctx.view.latest('SNX')['close'] < 9\n"
        "    if stop_hit:\n"
        "        return [{'action': 'CLOSE'}]\n"
        "    return []\n\n"
        "def build_strategy(params):\n    return None\n",
    )
    assert _check(report, "no_close_only_stop_tp")["passed"] is False


def test_close_stop_check_allows_bracket_delegation(tmp_path: Path) -> None:
    """An intent bracket ({"bracket": {"stop_loss": ...}}) delegates stop
    evaluation to the engine's ohlc_rules path — pricing the level off a close
    is correct there, not a close-only stop (the xyz-scalp-lab false positive
    that made the worker propose a cosmetic line split)."""
    report = _close_stop_report(
        tmp_path,
        "def decide(ctx):\n"
        "    current_close = 10.0\n"
        "    return [{\n"
        "        'action': 'OPEN',\n"
        "        'bracket': {'stop_loss': current_close * 0.98},\n"
        "    }]\n\n"
        "def build_strategy(params):\n    return None\n",
    )
    assert _check(report, "no_close_only_stop_tp")["passed"] is True


def test_bracket_escape_hatch_must_be_code_not_comment(tmp_path: Path) -> None:
    report = _close_stop_report(
        tmp_path,
        "# BracketEngine handles this... eventually\n"
        "def decide(ctx):\n"
        "    stop_hit = ctx.view.latest('SNX')['close'] < 9\n"
        "    if stop_hit:\n"
        "        return [{'action': 'CLOSE'}]\n"
        "    return []\n\n"
        "def build_strategy(params):\n    return None\n",
    )
    assert _check(report, "no_close_only_stop_tp")["passed"] is False


def test_boot_relative_warmup_counter_is_flagged(tmp_path: Path) -> None:
    report = _close_stop_report(
        tmp_path,
        "def decide(ctx):\n"
        "    bar_count = ctx.strategy_state.get('bar_count', 0) + 1\n"
        "    ctx.strategy_state['bar_count'] = bar_count\n"
        "    if bar_count < 28:\n"
        "        return []\n"
        "    return []\n\n"
        "def build_strategy(params):\n    return None\n",
    )
    check = _check(report, "no_boot_relative_warmup")
    assert check["passed"] is False
    assert check["blocking"] is False
    assert "every_n_bars" in check["hint"]


def test_data_derived_warmup_passes_counter_check(tmp_path: Path) -> None:
    report = _close_stop_report(
        tmp_path,
        "def decide(ctx):\n"
        "    if ctx.bar_index < 28 or not ctx.every_n_bars(2):\n"
        "        return []\n"
        "    return []\n\n"
        "def build_strategy(params):\n    return None\n",
    )
    assert _check(report, "no_boot_relative_warmup")["passed"] is True


def test_live_mode_blocks_without_wallet_label() -> None:
    from wayfinder_paths.jobs.execution.validation import _live_wallet_checks

    live_no_wallet = {
        "execution_contract": "jobs_v1",
        "script_loop": {"mode": "live"},
        "execution_params": {"venue": "hyperliquid"},
    }
    checks = _live_wallet_checks(live_no_wallet)
    assert checks[0]["name"] == "wallet_label_declared"
    assert checks[0]["passed"] is False
    assert checks[0]["blocking"] is True
    assert "execution_params.wallet_label" in checks[0]["hint"]

    live_with_wallet = {
        "execution_contract": "jobs_v1",
        "script_loop": {"mode": "live"},
        "execution_params": {"wallet_label": "funding-carry-basket"},
    }
    assert _live_wallet_checks(live_with_wallet)[0]["passed"] is True

    # Paper mode and legacy jobs are exempt — the check exists for live only.
    paper = {"execution_contract": "jobs_v1", "script_loop": {"mode": "paper"}}
    assert _live_wallet_checks(paper) == []
    legacy = {"execution_contract": "legacy", "script_loop": {"mode": "live"}}
    assert _live_wallet_checks(legacy) == []


def _build_windowed_strategy(params: dict[str, Any]) -> Any:
    """Reads only the trailing 10 closes — window-invariant by construction."""

    def decide(ctx: Any) -> list[OrderIntent]:
        closes = ctx.view.symbol_frame("SNX")["close"].astype(float)
        if len(closes) < 10:
            return []
        last = float(closes.iloc[-1])
        if last > float(closes.iloc[-10:].mean()):
            return [
                OrderIntent(
                    action="OPEN",
                    venue="hyperliquid",
                    symbol="SNX",
                    side="long",
                    size=1.0,
                    bracket={"stop_loss": last * 0.95, "take_profit": last * 1.05},
                )
            ]
        return []

    return types.SimpleNamespace(decide=decide)


def _build_full_frame_strategy(params: dict[str, Any]) -> Any:
    """Sizes off the mean of EVERYTHING handed — the parity trap: its
    decisions depend on how deep the harness's view happens to be."""

    def decide(ctx: Any) -> list[OrderIntent]:
        closes = ctx.view.symbol_frame("SNX")["close"].astype(float)
        return [
            OrderIntent(
                action="OPEN",
                venue="hyperliquid",
                symbol="SNX",
                side="long",
                notional=float(closes.mean()),
            )
        ]

    return types.SimpleNamespace(decide=decide)


def _build_parameterized_strategy(params: dict[str, Any]) -> Any:
    size = float(params.get("order_size") or 1.0)

    def decide(ctx: Any) -> list[OrderIntent]:
        return [
            OrderIntent(
                action="OPEN",
                venue="hyperliquid",
                symbol="SNX",
                side="long",
                size=size,
            )
        ]

    return types.SimpleNamespace(decide=decide)


_PROBE_SPEC = {
    "market_kind": "perp",
    "data_contract": {"bar_interval": "5m", "symbols": ["SNX"]},
}


def test_window_invariance_probe_passes_window_respecting_strategy() -> None:
    from wayfinder_paths.jobs.execution.primitives import CompletedBarsView
    from wayfinder_paths.jobs.execution.validation import window_invariance_probe

    bars = CompletedBarsView.from_rows(_bars(140))
    result = window_invariance_probe(
        _build_windowed_strategy, bars, _PROBE_SPEC, {"warmup_bars": 30}
    )
    assert result["status"] == "passed"
    assert result["window"] == 30
    assert result["bars_probed"] >= 2

    # Undeclared windows have nothing to prove — the probe skips.
    skipped = window_invariance_probe(_build_windowed_strategy, bars, _PROBE_SPEC, {})
    assert skipped["status"] == "skipped"


def test_window_invariance_probe_reds_full_frame_recompute() -> None:
    from wayfinder_paths.jobs.execution.primitives import CompletedBarsView
    from wayfinder_paths.jobs.execution.validation import window_invariance_probe

    bars = CompletedBarsView.from_rows(_bars(140))
    result = window_invariance_probe(
        _build_full_frame_strategy, bars, _PROBE_SPEC, {"warmup_bars": 30}
    )
    assert result["status"] == "failed"
    assert result["window"] == 30
    assert result["bar"]  # the differing bar is named
    assert result["base_intents"] != result["wide_intents"]


def test_parameter_behavior_probe_distinguishes_material_and_noop_knobs() -> None:
    from wayfinder_paths.jobs.execution.primitives import CompletedBarsView
    from wayfinder_paths.jobs.execution.validation import parameter_behavior_probe

    bars = CompletedBarsView.from_rows(_bars(140))
    changed = parameter_behavior_probe(
        _build_parameterized_strategy,
        bars,
        _PROBE_SPEC,
        {"warmup_bars": 30, "order_size": 1.0},
        [{"order_size": 1.0}, {"order_size": 2.0}],
    )
    unchanged = parameter_behavior_probe(
        _build_parameterized_strategy,
        bars,
        _PROBE_SPEC,
        {"warmup_bars": 30, "order_size": 1.0},
        [{"unused_knob": 0.0}, {"unused_knob": 1.0}],
    )

    assert changed["status"] == "changed"
    assert changed["changed_params"] == {"order_size": 2.0}
    assert changed["ticks_evaluated"] < 8 * 3
    assert unchanged["status"] == "unchanged"
    assert unchanged["bars_probed"] == 8
    assert unchanged["ticks_evaluated"] == 8 * 3


def test_window_invariance_check_reds_validation_with_hint(tmp_path: Path) -> None:
    """The validate_execution_job check: a declared-window job whose decide()
    reads beyond the window goes RED (blocking) with the differing bar and the
    bounded-window hint; a window-respecting job passes; undeclared jobs are
    exempt."""
    from wayfinder_paths.jobs.execution.validation import (
        BOUNDED_WINDOW_HINT,
        _window_invariance_checks,
    )

    root = tmp_path / "job"
    bars_path = root / "results" / "backtest" / "input_bars.json"
    bars_path.parent.mkdir(parents=True, exist_ok=True)
    bars_path.write_text(json.dumps({"bars": _bars(140)}), encoding="utf-8")
    script = root / "workspace" / "src" / "strategy.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "def decide(ctx):\n"
        "    closes = ctx.view.symbol_frame('SNX')['close'].astype(float)\n"
        "    return [{'action': 'OPEN', 'venue': 'hyperliquid', 'symbol': 'SNX',\n"
        "             'side': 'long', 'notional': float(closes.mean())}]\n",
        encoding="utf-8",
    )
    spec = ExecutionSpec.coerce(_PROBE_SPEC)
    job_data = {"execution_params": {"warmup_bars": 30, "symbols": ["SNX"]}}

    checks = _window_invariance_checks(root, script, job_data, spec)
    assert len(checks) == 1
    check = checks[0]
    assert check["name"] == "window_invariance"
    assert check["passed"] is False
    assert check.get("blocking") is not False  # RED, not advisory
    assert check["details"]["bar"] in check["error"]
    assert BOUNDED_WINDOW_HINT in check["error"]

    script.write_text(
        "def decide(ctx):\n"
        "    closes = ctx.view.symbol_frame('SNX')['close'].astype(float)\n"
        "    if len(closes) < 10:\n"
        "        return []\n"
        "    if float(closes.iloc[-1]) > float(closes.iloc[-10:].mean()):\n"
        "        return [{'action': 'OPEN', 'venue': 'hyperliquid',\n"
        "                 'symbol': 'SNX', 'side': 'long', 'size': 1.0}]\n"
        "    return []\n",
        encoding="utf-8",
    )
    passing = _window_invariance_checks(root, script, job_data, spec)
    assert len(passing) == 1 and passing[0]["passed"] is True

    # No declared window ⇒ no check (the simulator warns instead).
    assert _window_invariance_checks(root, script, {"execution_params": {}}, spec) == []


def test_probe_tolerates_seed_noise_but_names_a_real_level_shift() -> None:
    from wayfinder_paths.jobs.execution.validation import (
        _probe_values_match,
        probe_mismatches,
    )

    def intent(stop: float, size: float = 1.2249) -> dict:
        return {
            "action": "OPEN",
            "symbol": "SOL",
            "side": "buy",
            "size": size,
            "bracket": {"stop_loss": stop},
        }

    # A Wilder-ATR stop seeded 64 bars earlier: 2.6 bps apart, same trade.
    assert _probe_values_match([intent(92.5769)], [intent(92.5525)])
    assert probe_mismatches([intent(92.5769)], [intent(92.5525)]) == []
    # A level that really depends on history beyond the window.
    rows = probe_mismatches([intent(92.5769)], [intent(91.60)])
    assert not _probe_values_match([intent(92.5769)], [intent(91.60)])
    assert rows[0]["path"] == "[0].bracket.stop_loss" and rows[0]["rel"] > 0.01
    # A different number of intents is a mismatch in kind, not in degree.
    assert probe_mismatches([intent(1.0)], [])[0]["kind"] == "count"


def test_bounded_index_clock_store_is_flagged_and_blocks(tmp_path: Path) -> None:
    report = _close_stop_report(
        tmp_path,
        "def decide(ctx):\n"
        "    state = ctx.strategy_state\n"
        "    if ctx.bar_index < 20:\n"
        "        return []\n"
        "    state['arm'] = {'bar': int(ctx.bar_index), 'px': 1.0}\n"
        "    state.setdefault('first_bar', ctx.bar_index)\n"
        "    return []\n\n"
        "def build_strategy(params):\n    return None\n",
    )
    check = _check(report, "no_bounded_index_clock")
    assert check["passed"] is False
    assert check["blocking"] is True
    assert len(check["details"]) == 2
    assert "bar_ordinal" in check["hint"] and "bars_since" in check["hint"]


def test_bounded_index_clock_arithmetic_and_compare_are_flagged(
    tmp_path: Path,
) -> None:
    report = _close_stop_report(
        tmp_path,
        "def decide(ctx):\n"
        "    state = ctx.strategy_state\n"
        "    arm_bar = state.get('arm_bar')\n"
        "    age = int(ctx.bar_index) - arm_bar\n"
        "    if int(ctx.bar_index) - state['last_bar'] < 12:\n"
        "        return []\n"
        "    if int(state.get('cool', 0)) > ctx.bar_index:\n"
        "        return []\n"
        "    return []\n\n"
        "def build_strategy(params):\n    return None\n",
    )
    check = _check(report, "no_bounded_index_clock")
    assert check["passed"] is False
    assert len(check["details"]) == 3
    assert any("int(ctx.bar_index) - arm_bar" in hit for hit in check["details"])


def test_bounded_index_warmup_gate_passes_clock_check(tmp_path: Path) -> None:
    report = _close_stop_report(
        tmp_path,
        "# cooldown measured from ctx.bar_index would be wrong; we use ordinals\n"
        "def decide(ctx):\n"
        "    if ctx.bar_index < self_warmup(ctx):\n"
        "        return []\n"
        "    if ctx.bar_index < int(ctx.params.get('momentum_bars', 12)) + 4:\n"
        "        return []\n"
        "    last = ctx.view.timestamps[ctx.bar_index - 1]\n"
        "    ctx.strategy_state['arm_ordinal'] = ctx.bar_ordinal\n"
        "    age = ctx.bars_since(ctx.strategy_state.get('arm_ordinal'))\n"
        "    return []\n\n"
        "def self_warmup(ctx):\n    return int(ctx.params.get('warmup_bars', 20))\n\n"
        "def build_strategy(params):\n    return None\n",
    )
    check = _check(report, "no_bounded_index_clock")
    assert check["passed"] is True and check["hint"] is None


def _open_intent(size: float = 1.0) -> OrderIntent:
    return OrderIntent(
        action="OPEN", venue="hyperliquid", symbol="SNX", side="long", size=size
    )


def _build_armed_by_bar_index(params: dict[str, Any]) -> Any:
    """The stuck clock: arms once on the bounded index and never ages."""

    def decide(ctx: Any) -> list[OrderIntent]:
        if ctx.bar_index < 20:
            return []
        arm = ctx.strategy_state.setdefault("arm", {"bar": int(ctx.bar_index)})
        if int(ctx.bar_index) - int(arm["bar"]) >= 3:
            return [_open_intent()]
        return []

    return types.SimpleNamespace(decide=decide)


def _build_armed_by_ordinal(params: dict[str, Any]) -> Any:
    """The same machine on the global bar ordinal: fires every three bars."""

    def decide(ctx: Any) -> list[OrderIntent]:
        if ctx.bar_index < 20:
            return []
        stamp = ctx.strategy_state.setdefault("arm", ctx.bar_ordinal)
        if ctx.bars_since(stamp) >= 3:
            ctx.strategy_state["arm"] = ctx.bar_ordinal
            return [_open_intent()]
        return []

    return types.SimpleNamespace(decide=decide)


def _build_silent(params: dict[str, Any]) -> Any:
    def decide(ctx: Any) -> list[OrderIntent]:
        return []

    return types.SimpleNamespace(decide=decide)


def test_sequence_preview_reports_armed_state_machine() -> None:
    from wayfinder_paths.jobs.execution.simulator import PreparedExecutionDataset
    from wayfinder_paths.jobs.execution.validation import sequence_preview

    dataset = PreparedExecutionDataset.from_rows(_bars(140), {})
    stuck = sequence_preview(
        _build_armed_by_bar_index, dataset, _PROBE_SPEC, {"warmup_bars": 20}, bars=60
    )
    assert stuck["status"] == "armed_no_entry"
    assert stuck["entries"] == 0 and stuck["intents_total"] == 0
    assert stuck["bars_replayed"] == 61  # 80 replayed bars minus the 19 warmup
    assert stuck["state_keys"]["arm"]["changes"] == 1
    assert stuck["frozen_after"] == stuck["state_keys"]["arm"]["first_set_bar"]

    alive = sequence_preview(
        _build_armed_by_ordinal, dataset, _PROBE_SPEC, {"warmup_bars": 20}, bars=60
    )
    assert alive["status"] == "entries"
    assert alive["entries"] >= 15 and alive["by_action"]["OPEN"] == alive["entries"]
    assert alive["state_keys"]["arm"]["changes"] > 1
    assert alive["first_entry_bar"] is not None


def test_sequence_preview_is_silent_without_writes_and_skips_short_data() -> None:
    from wayfinder_paths.jobs.execution.simulator import PreparedExecutionDataset
    from wayfinder_paths.jobs.execution.validation import sequence_preview

    dataset = PreparedExecutionDataset.from_rows(_bars(140), {})
    silent = sequence_preview(
        _build_silent, dataset, _PROBE_SPEC, {"warmup_bars": 20}, bars=60
    )
    assert silent["status"] == "silent" and silent["state_keys"] == {}
    assert sequence_preview(_build_silent, dataset, _PROBE_SPEC, {})["status"] == (
        "skipped"
    )
    assert (
        sequence_preview(_build_silent, dataset, _PROBE_SPEC, {"warmup_bars": 200})[
            "status"
        ]
        == "skipped"
    )
