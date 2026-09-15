"""Freestyle jobs: the action contract, the paper runtime, the validation
ladder with its sandboxed dry run, and the compiler wrapper."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from wayfinder_paths.jobs.freestyle.contract import FreestyleSpec, normalize_action
from wayfinder_paths.jobs.freestyle.runtime import (
    LEDGER_PATH,
    STATE_PATH,
    run_freestyle_tick,
)
from wayfinder_paths.jobs.freestyle.validate import (
    static_checks,
    validate_freestyle_job,
)
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore

HORMUZ = """
from wayfinder_paths.jobs.freestyle import FreestyleSpec

SPEC = FreestyleSpec(venues=("polymarket", "hyperliquid"), max_notional_per_tick=500, max_loss_usd=20)

def tick(ctx):
    odds = ctx.quote("polymarket", "polymarket:hormuz-closure-2026:YES")
    ctx.state["last_odds"] = odds
    if odds > 0.4 and "BTC" not in ctx.positions:
        ctx.act({"venue": "hyperliquid", "kind": "market", "symbol": "BTC", "side": "long",
                 "notional": 100, "max_loss": 10})
    elif odds <= 0.4 and "BTC" in ctx.positions:
        ctx.act({"venue": "hyperliquid", "kind": "close", "symbol": "BTC"})
"""


def _job(
    tmp_path: Path, source: str = HORMUZ, **params
) -> tuple[JobStore, WayfinderJob]:
    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "hormuz-perp",
        script="workspace/src/hormuz_perp.py",
        interval_seconds=300,
        timeout_seconds=120,
        execution_contract="freestyle_v1",
        source={"kind": "freestyle", "origin": "inline"},
    )
    job.execution_params.update(params)
    root = store.init_layout(job)
    (root / "workspace" / "src" / "hormuz_perp.py").write_text(source, encoding="utf-8")
    store.save(job)
    return store, job


def test_normalize_action_maps_kinds_to_engine_intents() -> None:
    intent = normalize_action(
        {
            "venue": "hyperliquid",
            "kind": "market",
            "symbol": "BTC",
            "side": "long",
            "notional": 100,
            "max_loss": 5,
        }
    )
    assert intent.action == "OPEN" and intent.side == "long" and intent.notional == 100
    assert intent.metadata["max_loss"] == 5
    close = normalize_action(
        {"venue": "hyperliquid", "kind": "close", "symbol": "BTC"}, position_side="long"
    )
    assert close.action == "CLOSE" and close.reduce_only and close.side == "sell"
    buy = normalize_action(
        {
            "venue": "polymarket",
            "kind": "buy",
            "symbol": "polymarket:m:YES",
            "notional": 10,
        }
    )
    assert buy.side == "long"
    with pytest.raises(ValueError):
        normalize_action({"venue": "hyperliquid", "kind": "close", "symbol": "BTC"})
    with pytest.raises(ValueError):
        normalize_action({"venue": "hyperliquid", "kind": "market", "symbol": "BTC"})


def test_spec_from_mapping_and_defaults() -> None:
    spec = FreestyleSpec.from_any({"venues": ["hyperliquid"], "max_loss_usd": "5"})
    assert spec.venues == ("hyperliquid",) and spec.max_loss_usd == 5.0
    assert FreestyleSpec.from_any(None).quote_interval == "5m"


def test_dry_run_paper_ticks_fill_through_the_seam_and_leave_the_job_untouched(
    tmp_path: Path,
) -> None:
    store, job = _job(tmp_path)
    root = store.job_dir(job.id)
    marks = {
        "polymarket:polymarket:hormuz-closure-2026:YES": 0.6,
        "hyperliquid:BTC": 50_000.0,
    }
    payload = run_freestyle_tick(root, dry_run=True, ticks=2, marks=marks)
    assert payload["ok"], payload
    assert payload["fills"] == [] or payload["fills"][0]["symbol"] == "BTC"
    # the first tick opened BTC; the second held it (odds still above 0.4)
    assert "BTC" in payload["positions"]
    assert payload["positions"]["BTC"]["side"] == "long"
    assert payload["equity"] > 0
    # the dry run never touches the real ledger, state or forward dir
    assert not (root / LEDGER_PATH).exists()
    assert not (root / STATE_PATH).exists()
    # the store lays out empty forward files at creation; the dry run adds no rows
    real_orders = root / "results" / "forward" / "orders.jsonl"
    assert not real_orders.exists() or real_orders.stat().st_size == 0
    assert (
        root / "reports" / "validation" / "dryrun" / "forward" / "fills.jsonl"
    ).exists()


def test_paper_tick_persists_ledger_state_and_forward_rows(
    tmp_path: Path, monkeypatch
) -> None:
    store, job = _job(tmp_path)
    root = store.job_dir(job.id)
    from wayfinder_paths.jobs.freestyle import runtime as rt

    class _Gateway(rt.StubVenueGateway):
        pass

    monkeypatch.setattr(
        rt,
        "VenueGateway",
        lambda **kwargs: _Gateway(
            marks={
                "polymarket:polymarket:hormuz-closure-2026:YES": 0.7,
                "hyperliquid:BTC": 40_000.0,
            }
        ),
    )
    monkeypatch.setattr(rt, "fire_triggers", lambda *a, **k: None)
    monkeypatch.setattr(rt, "JobStore", lambda: store)
    monkeypatch.setenv("WAYFINDER_JOB_MODE", "paper")
    monkeypatch.delenv("WAYFINDER_DRY_RUN", raising=False)
    monkeypatch.delenv("WAYFINDER_JOB_REVISION", raising=False)
    monkeypatch.delenv("WAYFINDER_FORWARD_DIR", raising=False)
    payload = run_freestyle_tick(root)
    assert payload["ok"], payload
    ledger = json.loads((root / LEDGER_PATH).read_text())
    assert ledger["mode"] == "paper" and "BTC" in ledger["ledger"]["positions"]
    state = json.loads((root / STATE_PATH).read_text())
    assert state["last_odds"] == pytest.approx(0.7)
    rows = (root / "results" / "forward" / "fills.jsonl").read_text().splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["mode"] == "paper"
    runs = (root / "results" / "forward" / "runs.jsonl").read_text().splitlines()
    assert json.loads(runs[-1])["status"] == "ok"
    # second tick with odds below the threshold closes the position and books a trade
    monkeypatch.setattr(
        rt,
        "VenueGateway",
        lambda **kwargs: _Gateway(
            marks={
                "polymarket:polymarket:hormuz-closure-2026:YES": 0.2,
                "hyperliquid:BTC": 41_000.0,
            }
        ),
    )
    payload = run_freestyle_tick(root)
    assert payload["ok"] and "BTC" not in payload["positions"]
    trades = (root / "results" / "forward" / "trades.jsonl").read_text().splitlines()
    assert json.loads(trades[-1])["net_pnl"] > 0


def test_revision_drift_refuses_the_tick(tmp_path: Path, monkeypatch) -> None:
    store, job = _job(tmp_path)
    root = store.job_dir(job.id)
    monkeypatch.setenv("WAYFINDER_JOB_MODE", "paper")
    monkeypatch.setenv("WAYFINDER_JOB_REVISION", "deadbeef0000")
    monkeypatch.delenv("WAYFINDER_DRY_RUN", raising=False)
    from wayfinder_paths.jobs.freestyle import runtime as rt

    monkeypatch.setattr(rt, "fire_triggers", lambda *a, **k: None)
    monkeypatch.setattr(rt, "JobStore", lambda: store)
    payload = run_freestyle_tick(root)
    assert payload["ok"] is False and payload["refused"] is True
    assert "revision drift" in payload["error"]


def test_halted_job_refuses_openers_but_allows_exits(
    tmp_path: Path, monkeypatch
) -> None:
    store, job = _job(tmp_path)
    root = store.job_dir(job.id)
    from wayfinder_paths.jobs.freestyle import runtime as rt
    from wayfinder_paths.jobs.halt import request_halt

    request_halt(store, job.id, reason="owner stop")
    monkeypatch.setattr(
        rt,
        "VenueGateway",
        lambda **kwargs: rt.StubVenueGateway(
            marks={
                "polymarket:polymarket:hormuz-closure-2026:YES": 0.9,
                "hyperliquid:BTC": 40_000.0,
            }
        ),
    )
    monkeypatch.setattr(rt, "fire_triggers", lambda *a, **k: None)
    monkeypatch.setattr(rt, "JobStore", lambda: store)
    monkeypatch.setenv("WAYFINDER_JOB_MODE", "paper")
    monkeypatch.delenv("WAYFINDER_DRY_RUN", raising=False)
    monkeypatch.delenv("WAYFINDER_JOB_REVISION", raising=False)
    payload = run_freestyle_tick(root)
    assert payload["ok"] and payload["halted"]
    assert payload["actions"][0]["status"] == "refused"
    assert "halted" in payload["actions"][0]["reason"]


def test_static_checks_block_direct_venue_writes_and_async_ticks(
    tmp_path: Path,
) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import time\nfrom wayfinder_paths.mcp.tools.hyperliquid import hyperliquid_place_market_order\n"
        "async def tick(ctx):\n    time.sleep(1)\n    await hyperliquid_place_market_order(x=1)\n",
        encoding="utf-8",
    )
    by_name = {c["name"]: c for c in static_checks(bad)}
    assert by_name["no_direct_venue_writes"]["passed"] is False
    assert by_name["tick_is_sync"]["passed"] is False
    assert by_name["no_sleep_loops"]["passed"] is False
    good = tmp_path / "good.py"
    good.write_text(HORMUZ, encoding="utf-8")
    assert all(c["passed"] for c in static_checks(good))


def test_validate_freestyle_job_runs_the_sandboxed_dry_run(tmp_path: Path) -> None:
    store, job = _job(
        tmp_path,
        freestyle={
            "validation_marks": {
                "polymarket:polymarket:hormuz-closure-2026:YES": 0.55,
                "hyperliquid:BTC": 30_000,
            }
        },
    )
    report = validate_freestyle_job(job.id, store=store)
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["dry_run_ok"]["passed"], by_name["dry_run_ok"]
    assert report["status"] == "passed"
    assert report["kind"] == "freestyle_v1" and report["revision"]
    assert report["freestyle"]["spec"]["max_loss_usd"] == 20
    assert report["freestyle"]["dry_run"]["intents"][0]["symbol"] == "BTC"
    assert (store.job_dir(job.id) / "reports" / "validation" / "latest.json").exists()


def test_validate_freestyle_job_fails_a_crashing_script(tmp_path: Path) -> None:
    store, job = _job(
        tmp_path, source="def tick(ctx):\n    raise RuntimeError('boom')\n"
    )
    report = validate_freestyle_job(job.id, store=store)
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["dry_run_ok"]["passed"] is False
    assert "boom" in str(by_name["dry_run_ok"]["error"])
    assert report["status"] == "failed"


def test_compiler_writes_the_freestyle_wrapper(tmp_path: Path, monkeypatch) -> None:
    from wayfinder_paths.jobs import compiler as compiler_mod

    class _Bridge:
        def __init__(self, *, repo_root=None):
            self.calls = []

        def ensure_started(self):
            return {"ok": True}

        def add_or_update_script_job(self, **kwargs):
            self.calls.append(kwargs)
            return {"ok": True, "result": {"name": kwargs["name"]}}

        def delete(self, name):
            return {"ok": True}

    monkeypatch.setattr(compiler_mod, "RunnerBridge", _Bridge)
    store, job = _job(tmp_path)
    links = compiler_mod.JobCompiler(store=store).compile(job, start_daemon=False)
    wrapper = (
        Path(tmp_path) / links["jobs"][0]["response"]["result"]["name"]
        if False
        else None
    )
    script_wrapper = store.runs_jobs_dir / "hormuz_perp_script.py"
    text = script_wrapper.read_text(encoding="utf-8")
    assert "run_freestyle_tick as run_tick" in text
    assert "SystemExit(2" in text
    assert wrapper is None


FUNDING_SHORT = """
from wayfinder_paths.jobs.freestyle import FreestyleSpec

SPEC = FreestyleSpec(venues=("hyperliquid",), max_notional_per_tick=200, max_loss_usd=10)


def tick(ctx):
    rate = ctx.funding("hyperliquid", "BTC")
    ctx.state["last_funding"] = rate
    if rate > 0.0001 and "BTC" not in ctx.positions:
        ctx.act({"venue": "hyperliquid", "kind": "market", "symbol": "BTC",
                 "side": "short", "notional": 100, "max_loss": 10})
    elif rate < 0 and "BTC" in ctx.positions:
        ctx.act({"venue": "hyperliquid", "kind": "close", "symbol": "BTC"})
"""


def test_stub_gateway_funding_reads_the_mark_and_refuses_non_perp_venues() -> None:
    from wayfinder_paths.jobs.freestyle import runtime as rt

    gateway = rt.StubVenueGateway(marks={"funding:hyperliquid:BTC": 0.0002})
    assert gateway.funding("hyperliquid", "BTC") == pytest.approx(0.0002)
    assert gateway.funding("hyperliquid", "ETH") == 0.0
    with pytest.raises(LookupError, match="no funding rate"):
        gateway.funding("polymarket", "polymarket:x:YES")


def test_venue_gateway_funding_uses_the_feed_extension(monkeypatch) -> None:
    from wayfinder_paths.jobs.execution.venues import FundingSnapshot
    from wayfinder_paths.jobs.freestyle import runtime as rt

    class _Feed:
        async def get_funding(self, symbol: str, *, lookback_hours: int = 24):
            return FundingSnapshot(
                symbol=symbol, rate=0.00013, time_ms=1, history=((1, 0.00013),)
            )

    class _Adapter:
        feed = _Feed()

    monkeypatch.setattr(rt, "build_adapter", lambda venue, **kwargs: _Adapter())
    gateway = rt.VenueGateway(mode="paper", params={}, quote_interval="5m")
    assert gateway.funding("hyperliquid", "BTC") == pytest.approx(0.00013)
    with pytest.raises(LookupError, match="no funding rate"):
        gateway.funding("polymarket", "polymarket:x:YES")


def test_hyperliquid_feed_get_funding_returns_the_latest_settled_rate() -> None:
    from wayfinder_paths.jobs.execution.hyperliquid import HyperliquidMarketFeed

    class _Client:
        def __init__(self, rows: list[dict]) -> None:
            self.rows = rows

        async def get_funding_history(self, coin: str, start_ms: int, end_ms: int):
            assert coin == "BTC" and end_ms > start_ms
            return self.rows

    feed = HyperliquidMarketFeed(
        client=_Client(  # type: ignore[arg-type]
            [
                {"time": 2000, "fundingRate": "0.0002"},
                {"time": 1000, "fundingRate": "0.0001"},
                {"time": 3000, "fundingRate": None},
            ]
        )
    )
    snap = asyncio.run(feed.get_funding("BTC"))
    assert snap.rate == pytest.approx(0.0002) and snap.time_ms == 2000
    assert snap.history == ((1000, 0.0001), (2000, 0.0002))
    empty = HyperliquidMarketFeed(client=_Client([]))  # type: ignore[arg-type]
    with pytest.raises(LookupError, match="no funding rows"):
        asyncio.run(empty.get_funding("BTC"))


def test_dry_run_funding_trigger_opens_a_short_from_the_funding_mark(
    tmp_path: Path,
) -> None:
    store, job = _job(
        tmp_path,
        FUNDING_SHORT,
        freestyle={
            "validation_marks": {
                "hyperliquid:BTC": 60_000,
                "funding:hyperliquid:BTC": 0.0002,
            }
        },
    )
    report = validate_freestyle_job(job.id, store=store)
    assert report["status"] == "passed", [
        c for c in report["checks"] if not c["passed"]
    ]
    dry = report["freestyle"]["dry_run"]
    opens = [a for a in dry["actions"] if a["intent"]["action"] == "OPEN"]
    assert len(opens) == 1 and opens[0]["intent"]["side"] == "short"
    assert opens[0]["status"] == "filled"
    assert dry["funding"]["hyperliquid:BTC"] == pytest.approx(0.0002)
    assert dry["venues_used"] == ["hyperliquid"]


ETH_VALUE_WATCH = """
from wayfinder_paths.jobs.freestyle import FreestyleSpec

SPEC = FreestyleSpec(venues=("hyperliquid",), max_notional_per_tick=200, max_loss_usd=10)


def tick(ctx):
    eth_usd = ctx.token_value("ethereum-base")
    ctx.state["eth_usd"] = eth_usd
    if eth_usd < 2000 and "BTC" not in ctx.positions:
        ctx.act({"venue": "hyperliquid", "kind": "market", "symbol": "BTC",
                 "side": "long", "notional": 100, "max_loss": 10})
    elif eth_usd > 2200 and "BTC" in ctx.positions:
        ctx.act({"venue": "hyperliquid", "kind": "close", "symbol": "BTC"})
"""


def test_stub_gateway_token_price_reads_the_mark_and_defaults_to_one() -> None:
    from wayfinder_paths.jobs.freestyle import runtime as rt

    gateway = rt.StubVenueGateway(marks={"token:ethereum-base": 1950.0})
    assert gateway.token_price("ethereum-base") == pytest.approx(1950.0)
    assert gateway.token_price("usd-coin-polygon") == pytest.approx(1.0)


def test_venue_gateway_token_price_reads_the_token_client(monkeypatch) -> None:
    from wayfinder_paths.jobs.freestyle import runtime as rt

    seen: list[tuple[str, bool]] = []

    async def _details(token_id: str, *, market_data: bool = False, **kwargs):
        seen.append((token_id, market_data))
        return {"current_price": 2500.5} if token_id == "ethereum-base" else {}

    monkeypatch.setattr(rt.TOKEN_CLIENT, "get_token_details", _details)
    gateway = rt.VenueGateway(mode="paper", params={}, quote_interval="5m")
    assert gateway.token_price("ethereum-base") == pytest.approx(2500.5)
    assert seen == [("ethereum-base", True)]
    with pytest.raises(LookupError, match="no USD price"):
        gateway.token_price("nothing-here")


def test_dry_run_token_value_trigger_buys_from_the_token_mark(tmp_path: Path) -> None:
    store, job = _job(
        tmp_path,
        ETH_VALUE_WATCH,
        freestyle={
            "validation_marks": {
                "hyperliquid:BTC": 60_000,
                "token:ethereum-base": 1950,
            }
        },
    )
    report = validate_freestyle_job(job.id, store=store)
    assert report["status"] == "passed", [
        c for c in report["checks"] if not c["passed"]
    ]
    dry = report["freestyle"]["dry_run"]
    opens = [a for a in dry["actions"] if a["intent"]["action"] == "OPEN"]
    assert len(opens) == 1 and opens[0]["intent"]["side"] == "long"
    assert dry["token_values"]["ethereum-base"] == pytest.approx(1950.0)
    # a token read is not a venue: only the perp venue was used
    assert dry["venues_used"] == ["hyperliquid"]


YIELD_ROTATE = """
from wayfinder_paths.jobs.freestyle import FreestyleSpec

SPEC = FreestyleSpec(venues=("hyperliquid",), max_notional_per_tick=200, max_loss_usd=10)
FEED = "lend_supply_apr:aave-base:USDC"


def tick(ctx):
    rate = ctx.defi_yield(FEED)
    ctx.state["usdc_supply_apr"] = rate
    if rate > 0.05 and "BTC" not in ctx.positions:
        ctx.act({"venue": "hyperliquid", "kind": "market", "symbol": "BTC",
                 "side": "long", "notional": 100, "max_loss": 10})
    elif rate < 0.02 and "BTC" in ctx.positions:
        ctx.act({"venue": "hyperliquid", "kind": "close", "symbol": "BTC"})
"""
YIELD_FEED = "lend_supply_apr:aave-base:USDC"


def test_stub_gateway_defi_yield_reads_the_mark_and_defaults_to_zero() -> None:
    from wayfinder_paths.jobs.freestyle import runtime as rt

    gateway = rt.StubVenueGateway(marks={f"yield:{YIELD_FEED}": 0.08})
    assert gateway.defi_yield(YIELD_FEED) == pytest.approx(0.08)
    assert gateway.defi_yield(YIELD_FEED, "24h") == pytest.approx(0.08)
    assert gateway.defi_yield("yield_apy:sUSDe") == 0.0


def test_venue_gateway_defi_yield_uses_latest_snapshots_and_windows(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from wayfinder_paths.jobs.freestyle import runtime as rt

    class _Client:
        async def get_asset_basis(self, *, symbol):
            return {"asset_id": 1271, "symbol": symbol}

        async def screen_lending(
            self, *, asset_ids=None, venue=None, limit=100, **kwargs
        ):
            return {
                "data": [
                    {
                        "market_id": 911,
                        "asset_id": 1271,
                        "venue_name": "aave-base",
                        "chain_id": 8453,
                    }
                ]
            }

        async def get_market_lending_latest(self, *, market_id, asset_id):
            assert (market_id, asset_id) == (911, 1271)
            return SimpleNamespace(net_supply_apr_now=0.045, net_borrow_apr_now=0.065)

        async def get_asset_yield_latest(self, *, asset_id):
            return None

    monkeypatch.setattr(rt, "DELTA_LAB_CLIENT", _Client())

    async def _rows(feeds, *, days, since=None, client=None):
        name = "lend_supply_apr:aave-base:USDC"
        return (
            [
                {"timestamp": "t", "name": name, "value": v, "symbol": None}
                for v in (0.04, 0.05, 0.06)
            ],
            {"errors": {}},
        )

    monkeypatch.setattr(rt, "fetch_yield_rows", _rows)
    gateway = rt.VenueGateway(mode="paper", params={}, quote_interval="5m")
    assert gateway.defi_yield(YIELD_FEED) == pytest.approx(0.045)
    assert gateway.defi_yield("lend_borrow_apr:aave-base:USDC") == pytest.approx(0.065)
    assert gateway.defi_yield(YIELD_FEED, "24h") == pytest.approx(0.05)
    with pytest.raises(LookupError, match="no current yield"):
        gateway.defi_yield("yield_apy:sUSDe")
    with pytest.raises(LookupError, match="ctx.token_value"):
        gateway.defi_yield("token_price:ethereum-base")
    with pytest.raises(ValueError, match="window"):
        gateway.defi_yield(YIELD_FEED, "soon")


def test_dry_run_defi_yield_trigger_opens_from_the_yield_mark(tmp_path: Path) -> None:
    store, job = _job(
        tmp_path,
        YIELD_ROTATE,
        freestyle={
            "validation_marks": {"hyperliquid:BTC": 60_000, f"yield:{YIELD_FEED}": 0.08}
        },
    )
    report = validate_freestyle_job(job.id, store=store)
    assert report["status"] == "passed", [
        c for c in report["checks"] if not c["passed"]
    ]
    dry = report["freestyle"]["dry_run"]
    opens = [a for a in dry["actions"] if a["intent"]["action"] == "OPEN"]
    assert len(opens) == 1 and opens[0]["intent"]["side"] == "long"
    assert dry["yields"][YIELD_FEED] == pytest.approx(0.08)
    assert dry["venues_used"] == ["hyperliquid"]  # a yield read is not a venue


def test_freestyle_candidate_validation_runs_no_legacy_script_checks(
    tmp_path: Path,
) -> None:
    """A code_change proposal on a freestyle job validates the candidate with
    the freestyle static checks only: the legacy recorder/scenario checks
    (forward_recorder_imported, scenario_plan_present) would fail every
    freestyle candidate and block apply."""
    import shutil

    from wayfinder_paths.jobs.validation import (
        REQUIRED_INTENT_FIELDS,
        validate_candidate_application,
    )

    store, job = _job(tmp_path)
    job_dir = store.job_dir(job.id)
    candidate_dir = tmp_path / "candidate"
    shutil.copytree(job_dir, candidate_dir)
    script = candidate_dir / "workspace" / "src" / "hormuz_perp.py"
    script.write_text(
        script.read_text(encoding="utf-8").replace(
            '"notional": 200', '"notional": 100'
        ),
        encoding="utf-8",
    )
    proposal = {
        "kind": "code_change",
        "summary": "smaller BTC order",
        "intent_contract": {
            field: [f"{field} noted"] if field != "intent" else "smaller order"
            for field in REQUIRED_INTENT_FIELDS
        },
    }
    report = validate_candidate_application(
        repo_root=tmp_path,
        job_dir=job_dir,
        proposal=proposal,
        candidate_dir=candidate_dir,
        skip_behavior_checks=True,
    )
    names = {c["name"] for c in report["checks"]}
    assert "no_direct_venue_writes" in names
    assert not names & {
        "forward_recorder_imported",
        "forward_run_recorded",
        "scenario_plan_present",
    }
    failed = [c["name"] for c in report["checks"] if not c["passed"]]
    assert not [n for n in failed if not n.startswith("intent_contract")], failed


def test_freestyle_code_change_applies_at_the_promoted_revision(
    tmp_path: Path, monkeypatch
) -> None:
    """The owner's approve on a freestyle code change must leave the job
    consistent: the candidate is dry-run at its own revision, the promoted
    validation report carries that revision, the launch pin follows it and
    the next tick runs without a revision-drift refusal."""
    import shutil

    from wayfinder_paths.jobs.application import (
        claim_application,
        complete_application,
    )
    from wayfinder_paths.jobs.launch import (
        LAUNCH_STATE_PATH,
        evaluate_launch_checklist,
        launch_job,
    )
    from wayfinder_paths.jobs.proposals import propose_change
    from wayfinder_paths.jobs.validation import REQUIRED_INTENT_FIELDS
    from wayfinder_paths.tests.test_jobs_launch import _patch

    _patch(monkeypatch)
    store, job = _job(tmp_path)
    validate_freestyle_job(job.id, store=store)
    launched = launch_job(job.id, store=store, script_mode="paper")
    assert launched["launched"], launched
    root = store.job_dir(job.id)

    # Approve is possible at all: a freestyle proposal has no scenarios to replay.
    edited = tmp_path / "edited"
    shutil.copytree(root / "workspace", edited / "workspace")
    script = edited / "workspace" / "src" / "hormuz_perp.py"
    script.write_text(
        script.read_text(encoding="utf-8").replace(
            '"notional": 200', '"notional": 100'
        ),
        encoding="utf-8",
    )
    proposal = propose_change(
        store,
        job.id,
        kind="code_change",
        summary="smaller BTC order",
        intent_contract={
            field: [f"{field} noted"] if field != "intent" else "smaller order"
            for field in REQUIRED_INTENT_FIELDS
        },
        candidate_source=edited,
        memo="## Why\nHalve the order while the odds feed is thin.\n",
        allow_auto_apply=False,
    )
    pid = proposal["proposal_id"]
    assert proposal["change_summary"].startswith("## Why")
    report = proposal["candidate_report"]
    assert report["mode"] == "validation_only"
    assert report["validation_summary"]["status"] == "passed", report
    candidate_validation = json.loads(
        (
            root
            / "applications"
            / pid
            / "candidate"
            / "reports"
            / "validation"
            / "latest.json"
        ).read_text(encoding="utf-8")
    )
    assert candidate_validation["kind"] == "freestyle_v1"
    assert candidate_validation["revision"] == report["revision"]

    store.approve_proposal(job.id, pid)
    claim_application(store, job.id, pid)
    result = complete_application(store, job.id, pid, status="applied")
    promoted = result["promoted_revision"]
    assert result["proposal"]["application"]["status"] == "applied", result
    assert promoted == report["revision"]

    validation = store.read_json(job.id, "reports/validation/latest.json")
    assert validation["revision"] == promoted and validation["kind"] == "freestyle_v1"
    launch_state = store.read_json(job.id, LAUNCH_STATE_PATH)
    assert launch_state["revision"] == promoted
    assert launch_state["relaunched_by"] == f"apply:{pid}"
    assert launch_state["by"] == "owner"
    checklist = evaluate_launch_checklist(job.id, store=store, target="paper")
    assert checklist["ok"], checklist["reasons"]
    journal_types = [
        json.loads(line)["type"]
        for line in (root / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "launch_repinned" in journal_types
    launches = (root / "versions" / "launches.jsonl").read_text().splitlines()
    assert json.loads(launches[-1])["repin"] is True

    from wayfinder_paths.jobs.freestyle import runtime as rt

    monkeypatch.setattr(
        rt,
        "VenueGateway",
        lambda **kwargs: rt.StubVenueGateway(
            marks={
                "polymarket:polymarket:hormuz-closure-2026:YES": 0.7,
                "hyperliquid:BTC": 40_000.0,
            }
        ),
    )
    monkeypatch.setattr(rt, "fire_triggers", lambda *a, **k: None)
    monkeypatch.setattr(rt, "JobStore", lambda: store)
    monkeypatch.setenv("WAYFINDER_JOB_MODE", "paper")
    monkeypatch.setenv("WAYFINDER_JOB_REVISION", promoted)
    monkeypatch.delenv("WAYFINDER_DRY_RUN", raising=False)
    monkeypatch.delenv("WAYFINDER_FORWARD_DIR", raising=False)
    payload = run_freestyle_tick(root)
    assert payload["ok"], payload
    assert payload["actions"][0]["intent"]["notional"] == 100
    journal_types = [
        json.loads(line)["type"]
        for line in (root / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "revision_drift" not in journal_types


def test_last_tick_file_feeds_the_freestyle_snapshot_block(
    tmp_path: Path, monkeypatch
) -> None:
    """A real tick leaves a one-row record; the snapshot's `freestyle` block
    reads it (never ticks.jsonl) together with the validation dry run."""
    from wayfinder_paths.jobs.health import FREESTYLE_LAST_TICK_PATH
    from wayfinder_paths.jobs.sync import snapshot_job
    from wayfinder_paths.tests.test_jobs_launch import _patch

    _patch(monkeypatch)
    store, job = _job(tmp_path)
    root = store.job_dir(job.id)
    validate_freestyle_job(job.id, store=store)
    before = snapshot_job(job.id, store=store)
    block = before["freestyle"]
    assert block["contract"] == "freestyle_v1" and block["last_tick"] is None
    assert block["dry_run"]["ticks"] == 3
    assert block["dry_run"]["no_claim"].startswith("no backtest exists")
    assert block["spec"]["venues"]
    assert before["path"] is None

    from wayfinder_paths.jobs.freestyle import runtime as rt

    monkeypatch.setattr(
        rt,
        "VenueGateway",
        lambda **kwargs: rt.StubVenueGateway(
            marks={
                "polymarket:polymarket:hormuz-closure-2026:YES": 0.7,
                "hyperliquid:BTC": 40_000.0,
            }
        ),
    )
    monkeypatch.setattr(rt, "fire_triggers", lambda *a, **k: None)
    monkeypatch.setattr(rt, "JobStore", lambda: store)
    monkeypatch.setenv("WAYFINDER_JOB_MODE", "paper")
    monkeypatch.delenv("WAYFINDER_DRY_RUN", raising=False)
    monkeypatch.delenv("WAYFINDER_JOB_REVISION", raising=False)
    monkeypatch.delenv("WAYFINDER_FORWARD_DIR", raising=False)
    payload = run_freestyle_tick(root)
    assert payload["ok"] and payload["ts"]
    record = json.loads((root / FREESTYLE_LAST_TICK_PATH).read_text())
    assert record["status"] == "ok" and record["mode"] == "paper"
    assert record["reads"]["marks"]["hyperliquid:BTC"] == pytest.approx(40_000.0)
    assert record["actions"][0]["status"] == "filled"
    assert record["actions"][0]["symbol"] == "BTC"

    after = snapshot_job(job.id, store=store)
    last = after["freestyle"]["last_tick"]
    assert last["reads"]["marks"]["polymarket:polymarket:hormuz-closure-2026:YES"] == (
        pytest.approx(0.7)
    )
    assert after["freestyle"]["counts"]["ticks"] == 1
    assert after["heartbeat"]["last_tick"]["status"] == "ok"
    assert after["heartbeat"]["last_tick"]["ts"] == record["ts"]
    # A jobs_v1 job never carries the block.
    plain = WayfinderJob.new("plain", script="strategy.py", interval_seconds=60)
    store.create_job(plain)
    assert snapshot_job(plain.id, store=store)["freestyle"] is None


def test_backtest_monitors_do_not_apply_to_freestyle_jobs(tmp_path: Path) -> None:
    """The replication and counterfactual monitors need a backtest book; on a
    freestyle job they must say "not applicable" instead of journaling a
    *_failed entry on every wake (seen on the dev box after the first apply)."""
    from wayfinder_paths.jobs.counterfactual import counterfactual_job
    from wayfinder_paths.jobs.replication import replication_job

    store, job = _job(tmp_path)
    replication = replication_job(job.id, store=store)
    counterfactual = counterfactual_job(job.id, store=store)
    assert replication["available"] is False
    assert "not applicable" in replication["reason"]
    assert counterfactual["available"] is False
    assert "not applicable" in counterfactual["reason"]
    from wayfinder_paths.jobs.derived_features import refresh_derived_features_if_stale

    refresh = refresh_derived_features_if_stale(job.id, store=store)
    assert refresh["refreshed"] is False
    assert "not applicable" in refresh["reason"]
    journal = store.read_jsonl(job.id, "journal.jsonl", limit=100)
    assert not [
        row
        for row in journal
        if str(row.get("type", "")).endswith("_failed")
        or row.get("type") == "data_feed_degraded"
    ]


ONCHAIN_SCRIPT = """
from wayfinder_paths.jobs.freestyle import FreestyleSpec

SPEC = FreestyleSpec(venues=("onchain",), max_notional_per_tick=250, max_loss_usd=50)
TOKEN = "ethereum-robinhood"


def tick(ctx):
    price = ctx.quote("onchain", TOKEN)
    if price < 2000 and TOKEN not in ctx.positions:
        ctx.act({"venue": "onchain", "kind": "buy", "symbol": TOKEN, "notional": 200})
    elif price > 2500 and TOKEN in ctx.positions:
        ctx.act({"venue": "onchain", "kind": "sell", "symbol": TOKEN, "reason": "target"})
"""


def _paper_env(monkeypatch, store) -> None:
    from wayfinder_paths.jobs.freestyle import runtime as rt

    monkeypatch.setattr(rt, "fire_triggers", lambda *a, **k: None)
    monkeypatch.setattr(rt, "JobStore", lambda: store)
    monkeypatch.setenv("WAYFINDER_JOB_MODE", "paper")
    monkeypatch.delenv("WAYFINDER_DRY_RUN", raising=False)
    monkeypatch.delenv("WAYFINDER_JOB_REVISION", raising=False)
    monkeypatch.delenv("WAYFINDER_FORWARD_DIR", raising=False)


def test_onchain_spot_buys_below_and_sells_above_in_paper(
    tmp_path: Path, monkeypatch
) -> None:
    """The owner's ask: buy ETH on Robinhood chain under 2000, sell above 2500.
    In paper the swap fills at the token's USD price with the venue's fee and
    slippage; the inventory is a long-only position in the same ledger."""
    from wayfinder_paths.jobs.freestyle import runtime as rt

    store, job = _job(tmp_path, source=ONCHAIN_SCRIPT)
    root = store.job_dir(job.id)
    _paper_env(monkeypatch, store)
    monkeypatch.setattr(
        rt,
        "VenueGateway",
        lambda **kwargs: rt.StubVenueGateway(
            marks={"onchain:ethereum-robinhood": 1950.0}
        ),
    )
    payload = run_freestyle_tick(root)
    assert payload["ok"], payload
    ledger = json.loads((root / LEDGER_PATH).read_text())
    held = ledger["ledger"]["positions"]["ethereum-robinhood"]
    assert held["side"] == "long" and held["metadata"]["venue"] == "onchain"
    # 200 USD sized at the 1950 mark; the fill itself carries 50 bps slippage.
    assert held["size"] == pytest.approx(200 / 1950, rel=1e-6)
    assert held["avg_price"] == pytest.approx(1950 * 1.005, rel=1e-6)
    fill = json.loads(
        (root / "results" / "forward" / "fills.jsonl").read_text().splitlines()[-1]
    )
    assert fill["venue"] == "onchain" and fill["status"] == "filled"

    monkeypatch.setattr(
        rt,
        "VenueGateway",
        lambda **kwargs: rt.StubVenueGateway(
            marks={"onchain:ethereum-robinhood": 2600.0}
        ),
    )
    payload = run_freestyle_tick(root)
    assert payload["ok"] and "ethereum-robinhood" not in payload["positions"]
    trade = json.loads(
        (root / "results" / "forward" / "trades.jsonl").read_text().splitlines()[-1]
    )
    assert trade["venue"] == "onchain" and trade["net_pnl"] > 0
    assert trade["exit_reason"] == "target"


def test_onchain_refuses_shorts_and_limit_orders() -> None:
    with pytest.raises(ValueError, match="cannot short"):
        normalize_action(
            {
                "venue": "onchain",
                "kind": "market",
                "symbol": "ethereum-robinhood",
                "side": "short",
                "notional": 100,
            }
        )
    with pytest.raises(ValueError, match="limit orders"):
        normalize_action(
            {
                "venue": "onchain",
                "kind": "limit",
                "symbol": "ethereum-robinhood",
                "notional": 100,
                "limit_price": 1900,
            }
        )
    buy = normalize_action(
        {
            "venue": "onchain",
            "kind": "buy",
            "symbol": "ethereum-robinhood",
            "notional": 100,
        }
    )
    assert buy.action == "OPEN" and buy.side == "long"


def test_onchain_dry_run_quotes_the_token_stub_when_no_venue_mark() -> None:
    from wayfinder_paths.jobs.freestyle.runtime import StubVenueGateway

    gateway = StubVenueGateway(marks={"token:ethereum-robinhood": 1900.0})
    assert gateway.quote("onchain", "ethereum-robinhood") == pytest.approx(1900.0)
    assert StubVenueGateway(marks={"onchain:ethereum-robinhood": 2100.0}).quote(
        "onchain", "ethereum-robinhood"
    ) == pytest.approx(2100.0)


def test_onchain_adapter_paper_and_live_construction() -> None:
    from wayfinder_paths.jobs.execution.paper import PaperBroker
    from wayfinder_paths.jobs.execution.venues import build_adapter

    paper = build_adapter("onchain", mode="paper", params={"fee_bps": 30.0})
    assert isinstance(paper.broker, PaperBroker)
    assert paper.capabilities.market_kind == "spot"
    assert paper.capabilities.supports_shorts is False
    with pytest.raises(ValueError, match="wallet_label"):
        build_adapter("onchain", mode="live", params={})


def test_onchain_live_broker_quotes_then_swaps_on_the_job_wallet() -> None:
    from wayfinder_paths.jobs.execution.onchain import OnchainSwapBroker, human_amount
    from wayfinder_paths.jobs.execution.primitives import OrderIntent

    calls: dict[str, Any] = {}

    async def quote(**kwargs):
        calls["quote"] = kwargs
        return {
            "ok": True,
            "result": {
                "quote": {
                    "best_quote": {
                        "input_amount_usd": 200.0,
                        "output_amount": "100000000000000000",
                        "output_amount_usd": 199.4,
                        "fee_estimate": 0.6,
                    }
                },
                "suggested_swap_request": {**kwargs, "recipient": None},
            },
        }

    async def swap(**kwargs):
        calls["swap"] = kwargs
        return {
            "ok": True,
            "result": {
                "status": "confirmed",
                "effects": {"swap": {"txn_hash": "0xabc"}},
            },
        }

    async def details(symbol):
        return {"decimals": 18, "chain": {"code": "robinhood", "id": 4663}}

    broker = OnchainSwapBroker(
        wallet_label="job-wallet", quote=quote, swap=swap, token_details=details
    )
    buy = OrderIntent(
        action="OPEN",
        venue="onchain",
        symbol="ethereum-robinhood",
        side="long",
        notional=200.0,
        client_order_id="fs-1",
    )
    fill = asyncio.run(
        broker.place(buy, timestamp="2026-09-15T00:00:00+00:00", price=2000.0)
    )
    assert calls["quote"] == {
        "wallet_label": "job-wallet",
        "from_token": "usd-coin-robinhood",
        "to_token": "ethereum-robinhood",
        "amount": "200.0",
        "slippage_bps": 50,
    }
    assert calls["swap"]["from_token"] == "usd-coin-robinhood"
    assert fill.status == "filled" and fill.order_id == "0xabc"
    assert fill.filled_size == pytest.approx(0.1)  # raw 1e17 wei -> 0.1 ETH
    assert fill.avg_price == pytest.approx(2000.0)  # 200 USD / 0.1 ETH
    assert fill.fee == pytest.approx(0.6)

    sell = OrderIntent(
        action="CLOSE",
        venue="onchain",
        symbol="ethereum-robinhood",
        side="sell",
        size=0.1,
        reduce_only=True,
        client_order_id="fs-2",
    )
    fill = asyncio.run(
        broker.place(sell, timestamp="2026-09-15T01:00:00+00:00", price=2600.0)
    )
    assert calls["quote"]["from_token"] == "ethereum-robinhood"
    assert calls["quote"]["to_token"] == "usd-coin-robinhood"
    assert calls["quote"]["amount"] == "0.1"
    assert fill.status == "filled" and fill.filled_size == pytest.approx(0.1)
    assert fill.avg_price == pytest.approx(1994.0)  # output USD / tokens sold

    async def failed_swap(**kwargs):
        return {"ok": True, "result": {"status": "failed", "error": "reverted"}}

    broker = OnchainSwapBroker(
        wallet_label="job-wallet", quote=quote, swap=failed_swap, token_details=details
    )
    fill = asyncio.run(
        broker.place(buy, timestamp="2026-09-15T02:00:00+00:00", price=2000.0)
    )
    assert fill.status == "rejected" and "reverted" in str(fill.error)

    async def no_quote(**kwargs):
        return {
            "ok": False,
            "error": {"code": "quote_error", "message": "No quotes available"},
        }

    broker = OnchainSwapBroker(
        wallet_label="job-wallet", quote=no_quote, swap=swap, token_details=details
    )
    fill = asyncio.run(
        broker.place(buy, timestamp="2026-09-15T03:00:00+00:00", price=2000.0)
    )
    assert fill.status == "rejected" and "No quotes available" in str(fill.error)

    short = OrderIntent(
        action="OPEN",
        venue="onchain",
        symbol="ethereum-robinhood",
        side="short",
        notional=200.0,
    )
    fill = asyncio.run(
        broker.place(short, timestamp="2026-09-15T04:00:00+00:00", price=2000.0)
    )
    assert fill.status == "rejected" and "cannot short" in str(fill.error)
    assert human_amount("0.25", 18) == pytest.approx(0.25)
    assert human_amount("250000000000000000", 18) == pytest.approx(0.25)
