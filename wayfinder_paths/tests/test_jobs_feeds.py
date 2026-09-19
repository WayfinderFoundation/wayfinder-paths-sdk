"""Job-level feature feeds: fetch, declare (pinned, cadence, smoothing),
refresh incrementally, reconcile revisions, and reach decide() with
backtest/live parity — against in-file fakes, never the network."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from wayfinder_paths.jobs import feeds
from wayfinder_paths.jobs.execution import ExecutionSpec
from wayfinder_paths.jobs.execution.driver import tick_job
from wayfinder_paths.jobs.execution.features import (
    load_feature_rows,
    parse_feature_specs,
)
from wayfinder_paths.jobs.execution.job import _load_dataset
from wayfinder_paths.jobs.execution.paper import PaperBroker
from wayfinder_paths.jobs.execution.primitives import CompletedBarsView
from wayfinder_paths.jobs.execution.reconcile import reconcile_job
from wayfinder_paths.jobs.execution.simulator import simulate_execution
from wayfinder_paths.jobs.execution.validation import _feature_checks
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.tests.test_execution_feature_feeds import (
    FakeDeltaLab,
    FakeTokenClient,
    _hourly_candles,
    _yield_frame,
)
from wayfinder_paths.tests.test_jobs_live_driver import (
    PERP_CAPS,
    FakeAdapter,
    _bars,
    _now,
)
from wayfinder_paths.tests.test_jobs_live_driver import (
    _make_job as _make_driver_job,
)

WETH_BASE = "base_0x4200000000000000000000000000000000000006"
TOKEN_NAME = f"token_price:{WETH_BASE}"
LEND_NAME = "lend_supply_apr:aave-base:USDC"


def _make_job(
    tmp_path: Path, *, days: float = 3.0, embedded: bool = True
) -> tuple[JobStore, str]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "feed-demo",
        script=".wayfinder/jobs/feed-demo/workspace/src/strategy.py",
        interval_seconds=300,
    )
    spec = ExecutionSpec()
    spec.data_contract["bar_interval"] = "5m"
    if embedded:
        job.execution_spec = spec.to_dict()
    store.save(job)
    root = store.job_dir(job.id)
    if not embedded:
        (root / "execution_spec.json").write_text(
            json.dumps(spec.to_dict()), encoding="utf-8"
        )
    bars_path = root / "results" / "backtest" / "input_bars.json"
    bars_path.parent.mkdir(parents=True, exist_ok=True)
    bars_path.write_text(
        json.dumps({"bars": _bars(6), "metadata": {"days": days}}), encoding="utf-8"
    )
    return store, job.id


def _lending_client() -> FakeDeltaLab:
    client = FakeDeltaLab(_yield_frame())
    client.lending_rows = [
        {
            "market_id": 911,
            "asset_id": 1271,
            "venue_name": "aave-base",
            "chain_id": 8453,
            "market_external_id": "0xaave",
            "market_label": "Aave Base",
        }
    ]
    return client


def _declared(store: JobStore, job_id: str) -> list[dict[str, Any]]:
    return list(
        store.load(job_id).execution_spec["data_contract"].get("features") or []
    )


def test_fetch_token_features_writes_rows_declares_a_pinned_feed_and_restamps(
    tmp_path: Path,
) -> None:
    store, job_id = _make_job(tmp_path)
    root = store.job_dir(job_id)
    before = compute_workspace_revision(root)
    client = FakeTokenClient(_hourly_candles(80, now_ms=int(time.time() * 1000)))
    # the <chain>_<address> id resolves locally: no resolver network call
    result = feeds.fetch_token_features(
        job_id, token_ids=[WETH_BASE], interval="1h", store=store, client=client
    )
    assert result["feature_declared_now"] == [TOKEN_NAME]
    assert result["interval"] == "1h"
    assert (
        result["rows_appended"] == result["rows_fetched"] > 0
        and result["missing"] == []
    )
    assert result["days_requested"] == 3.0  # the dataset's own span
    declared = _declared(store, job_id)
    assert declared[0]["name"] == TOKEN_NAME
    assert declared[0]["feed"]["chain_id"] == 8453 and declared[0]["feed"][
        "address"
    ].startswith("0x4200")
    assert declared[0]["cadence"] == result["interval"] and declared[0][
        "smoothing"
    ] == {"method": "none"}
    assert (
        declared[0]["max_age_seconds"] == 3 * 3600
        if result["interval"] == "1h"
        else True
    )
    assert compute_workspace_revision(root) != before
    frame = load_feature_rows(
        [root],
        parse_feature_specs(ExecutionSpec.from_dict(store.load(job_id).execution_spec)),
    )[TOKEN_NAME]
    assert len(frame) == result["rows_appended"] and frame["symbol"].isna().all()
    # a second fetch extends nothing and declares nothing
    again = feeds.fetch_token_features(
        job_id, token_ids=[WETH_BASE], interval="1h", store=store, client=client
    )
    assert again["rows_appended"] == 0 and again["feature_declared_now"] == []


def test_fetch_token_features_declares_into_the_spec_file_when_not_embedded(
    tmp_path: Path,
) -> None:
    store, job_id = _make_job(tmp_path, embedded=False)
    root = store.job_dir(job_id)
    client = FakeTokenClient(_hourly_candles(30, now_ms=int(time.time() * 1000)))
    result = feeds.fetch_token_features(
        job_id, token_ids=[WETH_BASE], interval="1h", store=store, client=client
    )
    assert result["feature_declared_now"] == [TOKEN_NAME]
    doc = json.loads((root / "execution_spec.json").read_text(encoding="utf-8"))
    assert doc["data_contract"]["features"][0]["feed"]["kind"] == "token_price"
    assert not store.load(job_id).execution_spec


def test_token_interval_follows_the_bars_and_days_can_be_overridden(
    tmp_path: Path,
) -> None:
    store, job_id = _make_job(tmp_path, days=10.0)
    client = FakeTokenClient(_hourly_candles(30, now_ms=int(time.time() * 1000)))
    result = feeds.fetch_token_features(
        job_id, token_ids=[WETH_BASE], interval="1h", days=1, store=store, client=client
    )
    assert result["days_requested"] == 1.0 and result["rows_appended"] == 24
    with pytest.raises(ValueError, match="interval must be one of"):
        feeds.fetch_token_features(
            job_id, token_ids=[WETH_BASE], interval="7m", store=store, client=client
        )


def test_fetch_yield_features_end_to_end_and_idempotent(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path, days=400.0)
    client = _lending_client()
    result = feeds.fetch_yield_features(
        job_id, feeds=[LEND_NAME], store=store, client=client
    )
    assert (
        result["feature_declared_now"] == [LEND_NAME] and result["rows_appended"] == 3
    )
    assert result["days_requested"] == feeds.YIELD_RETENTION_DAYS
    assert "retained for about 211 days" in result["warning"]
    declared = _declared(store, job_id)[0]
    assert declared["feed"] == {
        "kind": "lend_supply_apr",
        "venue": "aave-base",
        "symbol": "USDC",
        "market_id": 911,
        "asset_id": 1271,
        "chain_id": 8453,
    }
    assert declared["cadence"] == "1h" and declared["smoothing"] == {
        "method": "mean",
        "window": "24h",
    }
    assert (
        declared["max_age_seconds"] == 3 * 3600
        and declared["stale_policy"] == "decide_anyway"
    )
    again = feeds.fetch_yield_features(
        job_id, feeds=[LEND_NAME], store=store, client=client
    )
    assert again["rows_appended"] == 0 and again["feature_declared_now"] == []
    # the pinned ids mean the second run never re-resolved
    assert not any(call[0] == "screen" for call in client.calls)


def test_yield_smoothing_override_and_token_kind_rejected(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path)
    client = _lending_client()
    result = feeds.fetch_yield_features(
        job_id, feeds=[LEND_NAME], smoothing="none", store=store, client=client
    )
    assert result["smoothing"] == {"method": "none"} and _declared(store, job_id)[0][
        "smoothing"
    ] == {"method": "none"}
    with pytest.raises(ValueError, match="fetch_token_features"):
        feeds.fetch_yield_features(
            job_id, feeds=[TOKEN_NAME], store=store, client=client
        )
    with pytest.raises(ValueError, match="needs a window"):
        feeds.parse_smoothing("mean")


def test_append_backfills_older_history_without_duplicates(tmp_path: Path) -> None:
    root = tmp_path
    recent = [
        {
            "timestamp": f"2026-01-01T0{h}:00:00+00:00",
            "name": "x",
            "value": float(h),
            "symbol": None,
        }
        for h in range(2, 5)
    ]
    assert feeds.append_feature_rows(root, recent)["rows_appended"] == 3
    wider = [
        {
            "timestamp": f"2026-01-01T0{h}:00:00+00:00",
            "name": "x",
            "value": float(h),
            "symbol": None,
        }
        for h in range(0, 6)
    ]
    written = feeds.append_feature_rows(root, wider)
    assert written["rows_appended"] == 3 and written["revised_rows"] == 0
    frame = load_feature_rows(
        [root],
        parse_feature_specs(
            ExecutionSpec.from_dict({"data_contract": {"features": [{"name": "x"}]}})
        ),
    )["x"]
    assert list(frame["value"]) == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]


def _recent_frame(hours: int = 5, *, end_offset_hours: int = 1) -> pd.DataFrame:
    end = pd.Timestamp.now(tz="UTC").floor("h") - pd.Timedelta(hours=end_offset_hours)
    stamps = pd.date_range(end=end, periods=hours, freq="h")
    return pd.DataFrame(
        {
            "supply_apr": [0.04 + 0.005 * i for i in range(hours)],
            "borrow_apr": [0.07] * hours,
        },
        index=pd.DatetimeIndex(stamps, name="ts"),
    )


def test_refresh_is_incremental_reconciles_revisions_and_noops_without_feeds(
    tmp_path: Path,
) -> None:
    store, job_id = _make_job(tmp_path)
    assert feeds.refresh_declared_feeds(job_id, store=store)["feeds"] == 0
    client = _lending_client()
    client.frame = _recent_frame()
    feeds.fetch_yield_features(job_id, feeds=[LEND_NAME], store=store, client=client)
    assert client.calls[-1][-1] == 3  # the dataset span
    # the source restates the newest value and publishes the next hour
    frame = _recent_frame(6, end_offset_hours=0)
    restated = frame.index[-2]
    frame.loc[restated, "supply_apr"] = frame.loc[restated, "supply_apr"] + 0.005
    client.frame = frame
    result = feeds.refresh_declared_feeds(job_id, store=store, delta_client=client)
    assert (
        result["feeds"] == 1
        and result["rows_appended"] == 1
        and result["revised_rows"] == 1
    )
    assert result["largest_revision"] == pytest.approx(0.005)
    assert client.calls[-1][-1] == 2  # newest stored minus a two-period warmup
    journal = [
        json.loads(line)
        for line in (store.job_dir(job_id) / "journal.jsonl").read_text().splitlines()
    ]
    revised = [row for row in journal if row.get("type") == "feed_revised"]
    assert revised and revised[0]["feeds"] == [LEND_NAME]
    root = store.job_dir(job_id)
    loaded = load_feature_rows(
        [root],
        parse_feature_specs(ExecutionSpec.from_dict(store.load(job_id).execution_spec)),
    )[LEND_NAME]
    assert loaded.set_index("timestamp")["value"][restated] == pytest.approx(
        frame.loc[restated, "supply_apr"]
    )
    unchanged = feeds.refresh_declared_feeds(job_id, store=store, delta_client=client)
    assert unchanged["rows_appended"] == 0 and unchanged["revised_rows"] == 0


def test_refresh_appends_healthy_feeds_then_raises_for_the_failed_one(
    tmp_path: Path,
) -> None:
    store, job_id = _make_job(tmp_path)
    client = _lending_client()
    feeds.fetch_yield_features(job_id, feeds=[LEND_NAME], store=store, client=client)
    token_client = FakeTokenClient(_hourly_candles(30, now_ms=int(time.time() * 1000)))
    feeds.fetch_token_features(
        job_id, token_ids=[WETH_BASE], interval="1h", store=store, client=token_client
    )
    client.fail_first = 5  # every retry of the yield feed fails
    token_client.candles = _hourly_candles(40, now_ms=int(time.time() * 1000))
    with pytest.raises(
        RuntimeError, match="feed refresh failed: lend_supply_apr:aave-base:USDC"
    ):
        feeds.refresh_declared_feeds(
            job_id, store=store, token_client=token_client, delta_client=client
        )
    root = store.job_dir(job_id)
    frame = load_feature_rows(
        [root],
        parse_feature_specs(ExecutionSpec.from_dict(store.load(job_id).execution_spec)),
    )[TOKEN_NAME]
    assert len(frame) > 24  # the token feed still advanced


def test_declared_feed_validation_rejects_an_unknown_kind(tmp_path: Path) -> None:
    store, job_id = _make_job(tmp_path)
    job = store.load(job_id)
    job.execution_spec["data_contract"]["features"] = [
        {"name": "x", "feed": {"kind": "weather"}}
    ]
    store.save(job)
    checks = _feature_checks(
        store.job_dir(job_id), ExecutionSpec.from_dict(job.execution_spec)
    )
    assert (
        checks[0]["name"] == "declared_features_valid" and checks[0]["passed"] is False
    )


TOKEN_STRATEGY = """
def decide(ctx):
    try:
        eth = float(ctx.view.feature("token_price:base_0x4200000000000000000000000000000000000006"))
    except ValueError:
        return []
    if "SNX" not in ctx.ledger.positions and eth < 2000:
        return [{"action": "OPEN", "venue": "hyperliquid", "symbol": "SNX",
                 "side": "buy", "size": 1}]
    if "SNX" in ctx.ledger.positions and eth > 2100:
        return [{"action": "CLOSE", "venue": "hyperliquid", "symbol": "SNX",
                 "side": "sell", "size": 1, "reduce_only": True}]
    return []
""".lstrip()


def test_backtest_and_driver_agree_on_a_token_price_feature(tmp_path: Path) -> None:
    """The parity anchor for colon-named feeds: rows appended by the writer
    reach decide() identically through the backtest loader and the driver."""
    store, job, root = _make_driver_job(tmp_path)
    (root / "workspace" / "src" / "strategy.py").write_text(
        TOKEN_STRATEGY, encoding="utf-8"
    )
    spec = ExecutionSpec.from_dict(job.execution_spec)
    spec.data_contract["features"] = [
        {
            "name": TOKEN_NAME,
            "feed": {
                "kind": "token_price",
                "token_id": WETH_BASE,
                "chain_id": 8453,
                "address": "0x4200000000000000000000000000000000000006",
                "interval": "5m",
            },
            "cadence": "5m",
            "smoothing": {"method": "none"},
        }
    ]
    job.execution_spec = spec.to_dict()
    store.save(job)
    rows = [
        {
            "timestamp": "2026-01-01T00:05:00+00:00",
            "name": TOKEN_NAME,
            "value": 1950.0,
            "symbol": None,
        },
        {
            "timestamp": "2026-01-01T00:15:00+00:00",
            "name": TOKEN_NAME,
            "value": 2150.0,
            "symbol": None,
        },
    ]
    assert feeds.append_feature_rows(root, rows)["rows_appended"] == 2
    bars = _bars(6)
    (root / "results" / "backtest").mkdir(parents=True, exist_ok=True)
    (root / "results" / "backtest" / "input_bars.json").write_text(
        json.dumps(bars), encoding="utf-8"
    )
    dataset = _load_dataset(root, spec, job.to_dict())
    backtest = simulate_execution(
        root / "workspace" / "src" / "strategy.py", dataset, spec, job.execution_params
    )
    fills = [f for f in backtest.trace["fills"] if f["status"] == "filled"]
    assert len(fills) == 2, "token-price strategy must open and close"

    async def _drive():
        broker = PaperBroker(capabilities=PERP_CAPS)
        out = []
        for count in range(1, len(bars) + 1):
            view = CompletedBarsView.from_rows(bars[:count])
            result = await tick_job(
                job,
                root,
                "paper",
                store=store,
                adapters={"hyperliquid": FakeAdapter(view, broker)},
                now=_now(view),
            )
            out.extend(result["fills"])
        return out

    driver_fills = asyncio.run(_drive())

    def key(rows):
        return [
            (r["symbol"], r["side"], r["filled_size"], r["avg_price"], r["timestamp"])
            for r in rows
            if r["status"] == "filled"
        ]

    assert key(driver_fills) == key(backtest.trace["fills"])
    report = reconcile_job(job.id, store=store)
    assert report["intent_match_rate"] == 1.0 and report["data_drift_ticks"] == 0


# ---- surfaces ----------------------------------------------------------------


def test_mcp_actions_dispatch_to_the_feed_ops(monkeypatch) -> None:
    from wayfinder_paths.mcp.tools import jobs as jobs_tools

    seen: list[tuple[str, dict[str, Any]]] = []

    async def fake_run(op: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        seen.append((op, kwargs))
        return {"ok": True, "result": {"op": op}}

    monkeypatch.setattr(jobs_tools, "_run_job_op", fake_run)
    monkeypatch.setattr(jobs_tools, "JobStore", lambda: object())
    result = asyncio.run(
        jobs_tools.core_jobs(
            action="fetch_token_features",
            job_id="j",
            token_ids=["ethereum-base"],
            bar_interval="1h",
        )
    )
    assert result["ok"] and seen[-1] == (
        "fetch_token_features",
        {"job_id": "j", "token_ids": ["ethereum-base"], "interval": "1h", "days": None},
    )
    result = asyncio.run(
        jobs_tools.core_jobs(
            action="fetch_yield_features",
            job_id="j",
            feeds=[LEND_NAME],
            days=90,
            smoothing="none",
        )
    )
    assert seen[-1] == (
        "fetch_yield_features",
        {"job_id": "j", "feeds": [LEND_NAME], "days": 90, "smoothing": "none"},
    )
    missing = asyncio.run(
        jobs_tools.core_jobs(action="fetch_yield_features", job_id="j")
    )
    assert missing["ok"] is False and "needs feeds" in json.dumps(missing)


def test_cli_verbs_round_trip(monkeypatch) -> None:
    from click.testing import CliRunner

    from wayfinder_paths.jobs import cli as cli_module

    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_token(job_id, **kwargs):
        calls.append(
            (
                "token",
                {"job_id": job_id, **{k: v for k, v in kwargs.items() if k != "store"}},
            )
        )
        return {"rows_appended": 3}

    def fake_yield(job_id, **kwargs):
        calls.append(
            (
                "yield",
                {"job_id": job_id, **{k: v for k, v in kwargs.items() if k != "store"}},
            )
        )
        return {"rows_appended": 5}

    monkeypatch.setattr(feeds, "fetch_token_features", fake_token)
    monkeypatch.setattr(feeds, "fetch_yield_features", fake_yield)
    monkeypatch.setattr(cli_module, "JobStore", lambda: object())
    runner = CliRunner()
    out = runner.invoke(
        cli_module.job_cli,
        [
            "fetch-token-features",
            "demo",
            "--token-id",
            "ethereum-base",
            "--interval",
            "1h",
            "--days",
            "7",
        ],
    )
    assert out.exit_code == 0, out.output
    assert json.loads(out.output)["result"]["rows_appended"] == 3
    assert calls[-1] == (
        "token",
        {
            "job_id": "demo",
            "token_ids": ["ethereum-base"],
            "interval": "1h",
            "days": 7.0,
        },
    )
    out = runner.invoke(
        cli_module.job_cli,
        ["fetch-yield-features", "demo", "--feed", LEND_NAME, "--smoothing", "ewm:12h"],
    )
    assert out.exit_code == 0, out.output
    assert calls[-1] == (
        "yield",
        {"job_id": "demo", "feeds": [LEND_NAME], "days": None, "smoothing": "ewm:12h"},
    )


def test_wake_substrate_lists_declared_feeds(tmp_path: Path) -> None:
    from wayfinder_paths.jobs.worker import _research_substrate_block

    store, job_id = _make_job(tmp_path)
    root = store.job_dir(job_id)
    assert "declared_feeds" not in _research_substrate_block(root)
    client = _lending_client()
    feeds.fetch_yield_features(job_id, feeds=[LEND_NAME], store=store, client=client)
    block = _research_substrate_block(root)
    entry = block["declared_feeds"][0]
    assert entry["name"] == LEND_NAME and entry["cadence"] == "1h"
    assert (
        entry["smoothing"] == {"method": "mean", "window": "24h"}
        and entry["available"] is True
    )
    assert "fetch_yield_features" in block["_basis"] and "EVERY wake" in block["_basis"]
