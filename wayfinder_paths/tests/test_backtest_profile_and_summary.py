"""Backtest telemetry (`profile`), the bounded compute-window default, and the
compact stdout summary — the diagnosability + token fixes for the create-a-
strategy loop."""

from __future__ import annotations

import datetime
import json
import types

from wayfinder_paths.jobs.execution import ExecutionContext, OrderIntent
from wayfinder_paths.jobs.execution.job import summarize_backtest_payload
from wayfinder_paths.jobs.execution.simulator import (
    DEFAULT_WARMUP_BARS,
    PreparedExecutionDataset,
    simulate_execution,
)

SPEC = {
    "market_kind": "perp",
    "data_contract": {"bar_interval": "1h", "symbols": ["IMX"]},
}

SPEC_PAIR = {
    "market_kind": "perp",
    "data_contract": {"bar_interval": "1h", "symbols": ["ETH", "SOL"]},
}


def _dataset(n: int) -> PreparedExecutionDataset:
    rows = []
    price = 1.0
    for i in range(n):
        price = max(0.01, price * (1.0 + 0.02 * ((i % 11) - 5) / 100.0))
        secs = 1_700_000_000 + i * 3600
        ts = datetime.datetime.fromtimestamp(secs, datetime.UTC).isoformat()
        rows.append(
            {
                "timestamp": ts,
                "symbol": "IMX",
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 1000.0,
            }
        )
    return PreparedExecutionDataset.from_rows(rows)


def _build_strategy(params=None):
    """Recomputes over the whole handed frame every bar (the pathology) but
    only DECIDES on the last 20 closes — so decisions are identical for any
    compute window >= 21."""

    def decide(ctx: ExecutionContext):
        frame = ctx.view.symbol_frame("IMX")
        if len(frame) < 22:
            return []
        df = frame.copy()  # full-frame copy each bar — the churn source
        close = df["close"].astype(float)
        df["ema"] = close.ewm(span=9, adjust=False).mean()
        last = float(close.iloc[-1])
        low_n = float(close.iloc[-21:-1].min())
        pos = ctx.ledger.positions.get("IMX")
        if pos is not None:
            if last > low_n * 1.03:
                return [
                    OrderIntent(
                        action="close",
                        venue="hyperliquid",
                        symbol="IMX",
                        side="buy",
                        size=pos.size,
                        reduce_only=True,
                    )
                ]
            return []
        if last < low_n:
            return [
                OrderIntent(
                    action="open",
                    venue="hyperliquid",
                    symbol="IMX",
                    side="short",
                    notional=100.0,
                    reduce_only=False,
                    bracket={
                        "stop_loss": last * 1.07,
                        "take_profit": last * 0.9,
                        "policy": "conservative",
                    },
                )
            ]
        return []

    ns = types.SimpleNamespace()
    ns.decide = decide
    return ns


def test_profile_reports_window_and_timing() -> None:
    res = simulate_execution(_build_strategy, _dataset(300), SPEC, {})
    profile = res.profile
    assert profile["compute_window"] == DEFAULT_WARMUP_BARS
    assert profile["compute_window_source"] == "default"
    assert profile["bars_total"] == 300
    assert profile["bars_timed"] > 0
    assert set(profile["tick_ms"]) >= {"mean", "p50", "p95", "max", "last"}
    assert "tick_time_growing" in profile


def test_warmup_window_never_changes_stats() -> None:
    """The bounded-window default must produce byte-identical results to full
    history for a strategy that only looks at the last N bars — the safety
    property that lets us window by default."""
    ds_size = 400
    full = simulate_execution(
        _build_strategy, _dataset(ds_size), SPEC, {"full_history": True}
    )
    default = simulate_execution(_build_strategy, _dataset(ds_size), SPEC, {})
    tight = simulate_execution(
        _build_strategy, _dataset(ds_size), SPEC, {"warmup_bars": 30}
    )
    assert full.profile["compute_window"] == "full_history"
    assert tight.profile["compute_window_source"] == "warmup_bars"
    for key in ("net_return", "sharpe", "trade_count", "win_rate"):
        assert full.stats[key] == default.stats[key] == tight.stats[key]


def test_lookback_bars_still_windows_for_back_compat() -> None:
    res = simulate_execution(
        _build_strategy, _dataset(120), SPEC, {"lookback_bars": 40}
    )
    assert res.profile["compute_window"] == 40
    assert res.profile["compute_window_source"] == "lookback_bars"


def test_summarize_backtest_payload_drops_heavy_arrays() -> None:
    res = simulate_execution(_build_strategy, _dataset(120), SPEC, {})
    payload = {
        "type": "single",
        "result": res.to_dict(),
        "artifacts": {"latest": "/j/results/backtest/latest.json"},
        "validation": {"status": "passed"},
    }
    summary = summarize_backtest_payload(payload)
    # Decision-grade fields kept.
    assert summary["result"]["stats"] == res.stats
    assert summary["result"]["profile"] == res.profile
    assert summary["artifacts"]["latest"].endswith("latest.json")
    # Multi-MB per-bar arrays dropped.
    for heavy in ("equity_curve", "trades", "positions", "trace", "visualization"):
        assert heavy not in summary["result"]
    # And the summary is dramatically smaller.
    assert (
        len(json.dumps(summary, default=str))
        < len(json.dumps(payload, default=str)) // 10
    )


def test_grid_summary_flags_in_sample_only() -> None:
    """A grid with no walk-forward is in-sample only — the summary must say so
    (and strip the heavy per-run arrays), so the agent validates OOS instead of
    trusting a curve-fit ranking."""
    grid_payload = {
        "type": "grid",
        "result": {
            "grid_id": "g1",
            "rank_by": "net_return",
            "runs": [{}, {}],
            "invalid": [],
            "ranked": [
                {"params": {"x": 1}, "net_return": 0.4, "equity_curve": [1, 2, 3]}
            ],
        },
    }
    summary = summarize_backtest_payload(grid_payload)
    assert "note" in summary and "out-of-sample" in summary["note"]
    assert "equity_curve" not in summary["result"]["ranked"][0]  # heavy key stripped
    # Once validated out-of-sample, the overfit note goes away.
    validated = summarize_backtest_payload(
        {**grid_payload, "walk_forward": {"summary": {"decay_ratio": 0.9}}}
    )
    assert "note" not in validated


def test_effective_workers_never_oversubscribes(monkeypatch) -> None:
    from wayfinder_paths.jobs.execution import simulator as sim

    monkeypatch.setattr(sim, "available_cpu_count", lambda: 2)
    assert sim._effective_workers(8, "process") == 2  # clamped to cores
    assert sim._effective_workers(1, "process") == 1
    assert sim._effective_workers(0, "process") == 2  # 0 => all cores
    assert sim._effective_workers(8, "serial") == 1  # serial is always 1


def test_quick_bars_truncates_to_last_n() -> None:
    """--quick backtests only the last N bars — cheap for parameter sweeps."""
    full = simulate_execution(_build_strategy, _dataset(300), SPEC, {})
    ds = _dataset(300)
    # Emulate the job-level `quick_bars` truncation (last 60 bars).
    from wayfinder_paths.jobs.execution.simulator import PreparedExecutionDataset

    ts = ds.bars.timestamps
    quick = PreparedExecutionDataset(ds.bars.window(len(ts) - 1, 60), {})
    quick_res = simulate_execution(_build_strategy, quick, SPEC, {})
    assert quick_res.profile["bars_total"] == 60
    assert full.profile["bars_total"] == 300


def test_diagnose_backtest_agrees_with_stats(tmp_path) -> None:
    """The diagnose headline is the run's own stats verbatim, and the buckets
    come from the same realized PnL — so it never disagrees with the backtest
    (unlike hand-rolled recomputation)."""
    from wayfinder_paths.jobs.backtest_artifacts import diagnose_backtest
    from wayfinder_paths.jobs.store import JobStore

    store = JobStore(repo_root=tmp_path)
    job_id = "diag-job"
    bt_dir = store.job_dir(job_id) / "results" / "backtest"
    bt_dir.mkdir(parents=True, exist_ok=True)
    latest = {
        "run_id": "r1",
        "stats": {
            "net_return": 0.05,
            "trade_count": 3,
            "win_rate": 0.667,
            "sharpe": 1.2,
        },
        "trades": [
            {
                "timestamp": "2026-01-01T08:00:00+00:00",
                "side": "buy",
                "reduce_only": True,
                "realized_pnl_delta": 2.0,
                "raw": {"metadata": {"exit_reason": "take_profit"}},
            },
            {
                "timestamp": "2026-01-01T09:00:00+00:00",
                "side": "buy",
                "reduce_only": True,
                "realized_pnl_delta": 1.0,
                "raw": {"metadata": {"exit_reason": "take_profit"}},
            },
            {
                "timestamp": "2026-01-01T10:00:00+00:00",
                "side": "buy",
                "reduce_only": True,
                "realized_pnl_delta": -1.5,
                "raw": {"metadata": {"exit_reason": "stop_loss"}},
            },
            # entry fill (no realized pnl) — excluded from trade buckets
            {
                "timestamp": "2026-01-01T07:00:00+00:00",
                "side": "sell",
                "realized_pnl_delta": 0.0,
            },
        ],
    }
    (bt_dir / "latest.json").write_text(json.dumps(latest))

    diag = diagnose_backtest(job_id, store=store)
    assert diag["available"] is True
    # Headline is the stats verbatim — no recomputation, no drift.
    assert diag["headline"]["net_return"] == 0.05
    assert diag["headline"]["win_rate"] == 0.667
    # Only the 3 closing fills are analyzed.
    assert diag["closed_trades_analyzed"] == 3
    tp = diag["by_exit_reason"]["take_profit"]
    assert tp["trades"] == 2 and tp["pnl"] == 3.0 and tp["win_rate"] == 1.0
    sl = diag["by_exit_reason"]["stop_loss"]
    assert sl["trades"] == 1 and sl["pnl"] == -1.5 and sl["win_rate"] == 0.0
    assert diag["worst_trades"][0]["pnl"] == -1.5
    assert diag["best_trades"][0]["pnl"] == 2.0
    # 3 trades → the top recommendation is the blocking "too few trades" one.
    assert diag["recommendations"][0]["severity"] == "blocking"
    assert diag["next_step"] == diag["recommendations"][0]["suggest"]


def _write_latest(store, job_id: str, stats: dict, trades: list) -> None:
    bt_dir = store.job_dir(job_id) / "results" / "backtest"
    bt_dir.mkdir(parents=True, exist_ok=True)
    (bt_dir / "latest.json").write_text(
        json.dumps({"run_id": "r1", "stats": stats, "trades": trades})
    )


def _closes(pnls_reasons_sides: list) -> list:
    """Build closing fills (reduce_only, non-zero pnl) from (pnl, reason, side)."""
    out = []
    for i, (pnl, reason, side) in enumerate(pnls_reasons_sides):
        out.append(
            {
                "timestamp": f"2026-01-01T{i % 24:02d}:00:00+00:00",
                "side": side,
                "reduce_only": True,
                "realized_pnl_delta": pnl,
                "raw": {"metadata": {"exit_reason": reason}},
            }
        )
    return out


def test_recommendations_flag_poor_payoff_high_winrate(tmp_path) -> None:
    """Win often but lose more per loss (PF < 1.1) → a HIGH 'poor payoff' rec
    that says tighten exits, not loosen entry — evidence quotes the real stats."""
    from wayfinder_paths.jobs.backtest_artifacts import diagnose_backtest
    from wayfinder_paths.jobs.store import JobStore

    store = JobStore(repo_root=tmp_path)
    # 24 trades, 70% winners but a poor profit factor.
    trades = _closes(
        [(1.0, "take_profit", "buy")] * 17 + [(-3.0, "stop_loss", "buy")] * 7
    )
    _write_latest(
        store,
        "payoff",
        {
            "net_return": -0.04,
            "trade_count": 24,
            "win_rate": 17 / 24,
            "profit_factor": 17.0 / 21.0,
            "avg_trade_pnl": (17 - 21) / 24,
            "sharpe": -0.3,
        },
        trades,
    )
    diag = diagnose_backtest("payoff", store=store)
    sevs = [r["severity"] for r in diag["recommendations"]]
    issues = " ".join(r["issue"] for r in diag["recommendations"]).lower()
    assert "high" in sevs
    assert "payoff" in issues or "losers" in issues
    # Stop-outs are the whole loss → a stop-bleed rec appears too.
    assert any("stop" in r["suggest"].lower() for r in diag["recommendations"])


def test_recommendations_validate_path_points_to_walk_forward(tmp_path) -> None:
    """A promising in-sample result yields the 'validate out-of-sample' rec
    naming job experiments / decay_ratio — the offer-to-deploy gate."""
    from wayfinder_paths.jobs.backtest_artifacts import diagnose_backtest
    from wayfinder_paths.jobs.store import JobStore

    store = JobStore(repo_root=tmp_path)
    trades = _closes(
        [(3.0, "take_profit", "short")] * 22 + [(-1.0, "stop_loss", "short")] * 8
    )
    _write_latest(
        store,
        "good",
        {
            "net_return": 0.31,
            "trade_count": 30,
            "win_rate": 22 / 30,
            "profit_factor": 66.0 / 8.0,
            "avg_trade_pnl": (66 - 8) / 30,
            "sharpe": 2.1,
        },
        trades,
    )
    diag = diagnose_backtest("good", store=store)
    validate = [r for r in diag["recommendations"] if r["severity"] == "validate"]
    assert validate, diag["recommendations"]
    assert "experiments" in validate[0]["suggest"]
    assert "decay_ratio" in validate[0]["suggest"]
    # No blocking/high issue on a clean promising run.
    assert diag["recommendations"][0]["severity"] == "validate"


def test_walk_forward_default_is_bounded_rolling_not_anchored() -> None:
    """Walk-forward with only folds/test_bars must NOT raise and must use a
    bounded ROLLING train window (constant size across folds) — the fast path.
    Previously this raised, pushing callers to the ~4x-slower anchored/expanding
    window that made OOS validation blow past the interactive time budget."""
    from wayfinder_paths.jobs.execution.walk_forward import (
        DEFAULT_WF_TRAIN_MULTIPLE,
        run_walk_forward,
    )

    ds = _dataset(500)
    rep = run_walk_forward(
        _build_strategy, ds, SPEC, [{}], folds=3, test_bars=60, warmup_bars=60
    )
    ok = [f for f in rep["folds"] if f["status"] == "ok"]
    assert len(ok) == 3
    # Rolling: every fold trains on the same bounded window (4 x test_bars).
    assert {f["train"]["bars"] for f in ok} == {DEFAULT_WF_TRAIN_MULTIPLE * 60}

    # Anchored is opt-in and expands — later folds train on strictly more bars.
    anchored = run_walk_forward(
        _build_strategy,
        ds,
        SPEC,
        [{}],
        folds=3,
        test_bars=60,
        warmup_bars=60,
        anchored=True,
    )
    a_sizes = [f["train"]["bars"] for f in anchored["folds"] if f["status"] == "ok"]
    assert a_sizes[-1] > a_sizes[0]


def test_simulator_runs_from_async_only_via_thread() -> None:
    """The simulator refuses to run inside a live event loop, so the async MCP
    backtest tool MUST offload it to a thread. Guards that contract: inline
    raises, `to_thread` works. If the guard or the MCP wrapper regresses, the
    agent loses its only working backtest tool and falls back to fighting the
    CLI / raw Python (the failure this fix removes)."""
    import asyncio

    import pytest

    async def run():
        with pytest.raises(RuntimeError, match="event loop"):
            simulate_execution(_build_strategy, _dataset(120), SPEC, {})
        return await asyncio.to_thread(
            simulate_execution, _build_strategy, _dataset(120), SPEC, {}
        )

    res = asyncio.run(run())
    assert "net_return" in res.stats


def test_mcp_job_ops_run_in_isolated_subprocess() -> None:
    """Heavy job ops (backtest/experiments/fetch/promote) run in a CHILD
    process, not inside the MCP server: in-server runs contend with the event
    loop for the GIL (~28x slowdown observed) and a memory spike OOM-kills the
    whole server, silently dropping every wayfinder tool. This drives the real
    subprocess pipe end-to-end (echo op) and asserts failures come back as
    clean tool errors instead of a dead server."""
    import asyncio

    from wayfinder_paths.mcp.tools import jobs as mcp_jobs

    async def drive():
        echoed = await mcp_jobs._run_job_op("__echo__", {"a": 1, "b": [2, 3]})
        failed = await mcp_jobs._run_job_op("__nope__", {})
        return echoed, failed

    echoed, failed = asyncio.run(drive())
    assert echoed == {"ok": True, "result": {"a": 1, "b": [2, 3]}}
    assert failed["ok"] is False
    assert failed["error"]["code"] == "job_op_failed"
    # The child's traceback tail is surfaced so real errors stay debuggable.
    assert "unknown op" in str(failed["error"]["details"])


def test_op_runner_summarizes_experiments_backtest(monkeypatch) -> None:
    """The child summarizes the experiments backtest payload (heavy arrays
    stripped, in-sample note attached) so only ~2 KB crosses the pipe."""
    from wayfinder_paths.jobs.execution import op_runner

    def fake_experiment(job_id, grid, **kwargs):
        return {
            "experiment": {"id": "e1"},
            "backtest": {
                "type": "grid",
                "result": {
                    "grid_id": "g1",
                    "rank_by": "net_return",
                    "runs": [{}],
                    "invalid": [],
                    "ranked": [
                        {
                            "params": {"a": 1},
                            "net_return": 0.1,
                            "equity_curve": [1, 2, 3],
                        }
                    ],
                },
            },
        }

    import wayfinder_paths.jobs.execution.experiments as experiments_mod

    monkeypatch.setattr(experiments_mod, "run_experiment", fake_experiment)
    result = op_runner._run(
        "experiments", {"job_id": "j", "grid": {"a": [1, 2]}, "full": False}
    )
    backtest = result["backtest"]
    assert "equity_curve" not in backtest["result"]["ranked"][0]
    assert "note" in backtest


def test_run_experiment_threads_quick_bars_and_parallel(monkeypatch) -> None:
    """Experiments default to a CPU-clamped process pool and accept quick_bars
    — a serial full-history sweep of a heavy strategy is what blew the
    interactive budget live (8 combos x 4319 bars at ~30 bars/s ≈ 20 min)."""
    import wayfinder_paths.jobs.execution.experiments as experiments_mod

    captured: dict = {}

    def fake_backtest(job_id, **kwargs):
        captured.update(kwargs, job_id=job_id)
        return {"type": "grid", "result": {"grid_id": "g", "ranked": []}}

    monkeypatch.setattr(experiments_mod, "backtest_execution_job", fake_backtest)
    monkeypatch.setattr(
        experiments_mod, "record_experiment", lambda job_id, payload, store: {}
    )
    experiments_mod.run_experiment("j", {"a": [1, 2]}, quick_bars=2000)
    assert captured["quick_bars"] == 2000
    assert captured["workers"] == 0  # 0 = all cores, clamped downstream
    assert captured["parallel"] == "process"


def _pair_dataset(n: int) -> PreparedExecutionDataset:
    rows = []
    eth, sol = 1000.0, 100.0
    for i in range(n):
        eth = max(1.0, eth * (1.0 + 0.02 * ((i % 13) - 6) / 100.0))
        sol = max(0.1, sol * (1.0 + 0.02 * ((i % 7) - 3) / 100.0))
        secs = 1_700_000_000 + i * 3600
        ts = datetime.datetime.fromtimestamp(secs, datetime.UTC).isoformat()
        for sym, px in (("ETH", eth), ("SOL", sol)):
            rows.append(
                {
                    "timestamp": ts,
                    "symbol": sym,
                    "open": px,
                    "high": px * 1.01,
                    "low": px * 0.99,
                    "close": px,
                    "volume": 1000.0,
                }
            )
    return PreparedExecutionDataset.from_rows(rows)


def _zscore_intents(ctx: ExecutionContext, frame, z_last: float):
    pos = ctx.ledger.positions.get("IMX")
    last = float(frame["close"].iloc[-1])
    if pos is None and z_last <= -0.9:
        return [
            OrderIntent(
                action="open",
                venue="hyperliquid",
                symbol="IMX",
                side="long",
                notional=100.0,
                reduce_only=False,
                bracket={
                    "stop_loss": last * 0.93,
                    "take_profit": last * 1.1,
                    "policy": "conservative",
                },
            )
        ]
    if pos is not None and z_last >= 0.8:
        return [
            OrderIntent(
                action="close",
                venue="hyperliquid",
                symbol="IMX",
                side="sell",
                size=pos.size,
                reduce_only=True,
                metadata={"exit_reason": "z_exit"},
            )
        ]
    return []


def test_precompute_matches_inline_and_runs_once() -> None:
    """The `precompute` hook (one vectorized pass, columns merged onto the
    bars) must produce IDENTICAL results to computing the same indicator
    inside decide() every bar — it exists purely to kill the ~5ms-per-pandas-
    op-per-bar overhead that made replays crawl. Also: exactly one call per
    backtest."""
    calls = {"n": 0}

    def build_inline(params=None):
        def decide(ctx: ExecutionContext):
            frame = ctx.view.symbol_frame("IMX")
            if len(frame) < 22:
                return []
            close = frame["close"].astype(float)
            mean = close.rolling(20).mean()
            std = close.rolling(20).std().replace(0, 1e-9)
            z_last = float(((close - mean) / std).iloc[-1])
            return _zscore_intents(ctx, frame, z_last)

        ns = types.SimpleNamespace()
        ns.decide = decide
        ns.warmup_bars = 60
        return ns

    def build_precomputed(params=None):
        def precompute(frames):
            out = {}
            for sym, frame in frames.items():
                close = frame["close"].astype(float)
                mean = close.rolling(20).mean()
                std = close.rolling(20).std().replace(0, 1e-9)
                feats = frame[[]].copy()
                feats["z"] = (close - mean) / std
                out[sym] = feats
            calls["n"] += 1
            return out

        def decide(ctx: ExecutionContext):
            frame = ctx.view.symbol_frame("IMX")
            if len(frame) < 22:
                return []
            z_last = float(frame["z"].iloc[-1])
            return _zscore_intents(ctx, frame, z_last)

        ns = types.SimpleNamespace()
        ns.decide = decide
        ns.precompute = precompute
        ns.warmup_bars = 60
        return ns

    inline = simulate_execution(build_inline, _dataset(400), SPEC, {})
    fast = simulate_execution(build_precomputed, _dataset(400), SPEC, {})
    assert calls["n"] == 1  # one vectorized pass, not one per bar
    for key in ("net_return", "trade_count", "win_rate", "sharpe"):
        assert inline.stats[key] == fast.stats[key]
    assert inline.stats["trade_count"] > 0  # the comparison actually traded


def test_precompute_cross_symbol_pair_columns() -> None:
    """Cross-symbol features (the pair-trade case): precompute reads BOTH
    symbols' frames and attaches the spread column to the traded symbol —
    decide() then just reads it."""
    seen = {"cols": None}

    def build(params=None):
        def precompute(frames):
            eth = frames["ETH"]["close"].astype(float).to_numpy()
            sol = frames["SOL"]["close"].astype(float).to_numpy()
            n = min(len(eth), len(sol))
            import pandas as pd

            ratio = pd.Series(eth[:n] / sol[:n])
            z = (ratio - ratio.rolling(20).mean()) / ratio.rolling(20).std()
            feats = frames["ETH"][[]].iloc[:n].copy()
            feats["pair_z"] = z.to_numpy()
            return {"ETH": feats}

        def decide(ctx: ExecutionContext):
            frame = ctx.view.symbol_frame("ETH")
            seen["cols"] = list(frame.columns)
            if len(frame) < 25 or frame["pair_z"].iloc[-1] is None:
                return []
            return []

        ns = types.SimpleNamespace()
        ns.decide = decide
        ns.precompute = precompute
        ns.warmup_bars = 60
        return ns

    simulate_execution(build, _pair_dataset(120), SPEC_PAIR, {})
    assert "pair_z" in (seen["cols"] or [])


def test_precompute_row_mismatch_raises() -> None:
    from wayfinder_paths.jobs.execution.features import apply_precompute

    def bad_precompute(frames):
        frame = next(iter(frames.values()))
        return {next(iter(frames)): frame[[]].iloc[:-5].assign(x=1.0)}

    ns = types.SimpleNamespace(precompute=bad_precompute)
    import pytest

    with pytest.raises(ValueError, match="one per input bar"):
        apply_precompute(ns, _dataset(100).bars)


class _FakeFundingExchange:
    """Injectable ccxt fake: one funding page per market, then empty.
    Timestamps anchor to the requested `since` (hour-quantized so a re-fetch
    seconds later produces identical rows — exercising the dedupe path)."""

    def __init__(self):
        self.pages: dict = {}

    async def load_markets(self):
        return {
            "ETH/USDT:USDT": {"active": True, "symbol": "ETH/USDT:USDT"},
            "SOL/USDT:USDT": {"active": True, "symbol": "SOL/USDT:USDT"},
        }

    async def fetch_funding_rate_history(self, market, since=None, limit=500):
        page = self.pages.get(market, 0)
        self.pages[market] = page + 1
        if page > 0:
            return []
        base = (int(since) // 3_600_000) * 3_600_000 + 3_600_000
        return [
            {"timestamp": base + i * 3_600_000, "fundingRate": 0.0001 * (i + 1)}
            for i in range(3)
        ]

    async def close(self):
        return None


def test_fetch_funding_features_end_to_end(tmp_path) -> None:
    """Funding rates are FIRST-CLASS: one call pulls history into the job's
    feature store in the canonical row shape, dedupes on re-fetch, and
    declares the feature in the execution spec so bars carry a `funding`
    column in backtest and live. (The agent previously had to improvise this
    with scratch scripts and hand-copied JSONL.)"""
    import json as _json

    from wayfinder_paths.jobs.execution.preflight import fetch_funding_features
    from wayfinder_paths.jobs.store import JobStore

    store = JobStore(repo_root=tmp_path)
    root = store.job_dir("pair-job")
    root.mkdir(parents=True, exist_ok=True)
    (root / "job.yaml").write_text("id: pair-job\n")
    (root / "execution_spec.json").write_text(
        _json.dumps(
            {
                "market_kind": "perp",
                "data_contract": {"bar_interval": "1h", "symbols": ["ETH", "SOL"]},
            }
        )
    )

    result = fetch_funding_features(
        "pair-job", days=2, store=store, exchange_client=_FakeFundingExchange()
    )
    assert result["rows_fetched"] == 6  # 3 rows x 2 symbols
    assert result["rows_appended"] == 6
    assert result["per_symbol"] == {"ETH": 3, "SOL": 3}
    assert result["feature_declared_now"] is True

    lines = [
        _json.loads(line)
        for line in (root / "state" / "features.jsonl").read_text().splitlines()
    ]
    assert len(lines) == 6
    row = lines[0]
    assert row["name"] == "funding" and row["symbol"] == "ETH"
    assert isinstance(row["value"], float) and "written_at" in row

    spec = _json.loads((root / "execution_spec.json").read_text())
    assert {"name": "funding"} in spec["data_contract"]["features"]

    # Re-fetch: identical rows dedupe to zero appends; feature already declared.
    again = fetch_funding_features(
        "pair-job", days=2, store=store, exchange_client=_FakeFundingExchange()
    )
    assert again["rows_appended"] == 0
    assert again["feature_declared_now"] is False
