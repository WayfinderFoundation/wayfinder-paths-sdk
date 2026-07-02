from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.driver import tick_job
from wayfinder_paths.jobs.execution.job import _load_dataset, _load_job_yaml
from wayfinder_paths.jobs.execution.paper import PaperBroker
from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    ExecutionSpec,
    FillEvent,
    OrderIntent,
    PositionRecord,
    TradeCapacity,
    bar_interval_seconds,
)
from wayfinder_paths.jobs.execution.purity import PurityViolation
from wayfinder_paths.jobs.execution.validation import resolve_execution_spec
from wayfinder_paths.jobs.execution.venues import (
    MarketEvent,
    VenueCapabilities,
    VenueState,
    build_adapter,
)
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.models import WayfinderJob, utc_now_iso
from wayfinder_paths.jobs.store import JobStore

PREFLIGHT_CAPS = VenueCapabilities(
    market_kind="perp",
    supports_brackets=True,
    supports_shorts=True,
    supports_notional_sizing=True,
    supports_limit_orders=True,
)


def build_live_dataset(
    job_id: str,
    *,
    days: int = 14,
    store: JobStore | None = None,
    adapters: dict[str, Any] | None = None,
    source: str = "venues",
    exchange: str = "binance",
    market_type: str = "swap",
    quote: str = "USDT",
    feed: Any | None = None,
) -> dict[str, Any]:
    """Fetch real candles and persist them as the job's backtest dataset
    (input_bars.json).

    source="venues" (default) fetches through the same adapter feeds the live
    driver uses — backtest, preflight, and live share one data path.
    source="ccxt" fetches long-history OHLCV from a CCXT exchange (dataset
    building ONLY — the live driver's feed and broker stay on the job's
    venues); the metadata records exchange/market/symbol substitutions so the
    provenance is auditable."""
    store = store or JobStore()
    root = store.job_dir(job_id)
    job_data = _load_job_yaml(root)
    spec_data, _ = resolve_execution_spec(root, job_data)
    spec = ExecutionSpec.from_dict(spec_data)
    params = dict(job_data.get("execution_params") or {})
    bar_interval = spec.data_contract.get("bar_interval")
    bar_seconds = bar_interval_seconds(bar_interval)
    if not bar_seconds:
        raise ValueError("execution_spec.data_contract.bar_interval is required")
    symbols = [
        str(symbol)
        for symbol in (params.get("symbols") or spec.data_contract.get("symbols") or [])
    ]
    if not symbols:
        raise ValueError("no symbols configured for dataset fetch")

    import asyncio

    if source == "ccxt":
        from wayfinder_paths.jobs.execution.ccxt_feed import fetch_ccxt_dataset_rows

        if feed is not None:
            lookback_bars = max(2, int(days * 86_400 / bar_seconds))
            view = asyncio.run(
                feed.get_completed_bars(
                    symbols, str(bar_interval), lookback_bars=lookback_bars
                )
            )
            rows = view.to_rows()
            source_metadata = {
                "exchange": exchange,
                "market_type": market_type,
                "quote": quote,
                "symbol_map": dict(getattr(feed, "symbol_map", {})),
                "label_convention": "close_time",
            }
        else:
            rows, source_metadata = asyncio.run(
                fetch_ccxt_dataset_rows(
                    symbols,
                    str(bar_interval),
                    days=days,
                    exchange_id=exchange,
                    market_type=market_type,
                    quote=quote,
                )
            )
        metadata = {
            "source": "ccxt",
            **source_metadata,
            "venues": [],
            "symbols": symbols,
            "interval": str(bar_interval),
            "days": days,
            "fetched_at": utc_now_iso(),
        }
    else:
        if adapters is None:
            adapters = {
                venue: build_adapter(venue, mode="paper", spec=spec, params=params)
                for venue in (spec.venues or ["hyperliquid"])
            }
        lookback_bars = max(2, int(days * 86_400 / bar_seconds))
        rows = []

        async def _fetch() -> None:
            for adapter in adapters.values():
                view = await adapter.feed.get_completed_bars(
                    symbols, str(bar_interval), lookback_bars=lookback_bars
                )
                rows.extend(view.to_rows())

        asyncio.run(_fetch())
        metadata = {
            "source": "live_fetch",
            "venues": sorted(adapters),
            "symbols": symbols,
            "interval": str(bar_interval),
            "days": days,
            "fetched_at": utc_now_iso(),
        }
    if not rows:
        raise RuntimeError("no bars returned while building live dataset")
    path = root / "results" / "backtest" / "input_bars.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"bars": rows, "metadata": metadata}, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return {"path": str(path), "bars": len(rows), "metadata": metadata}


class ReplayFeed:
    """Serves the dataset truncated to an externally-set cursor (one bar per
    driver tick); freezing the cursor after `stale_after` simulates a dead
    feed."""

    def __init__(
        self, bars: list[dict[str, Any]], *, stale_after: int | None = None
    ) -> None:
        self._view = CompletedBarsView.from_rows(bars)
        self._timestamps = self._view.timestamps
        self.cursor = 0
        self.stale_after = stale_after

    def view_at(self, tick_index: int) -> CompletedBarsView:
        effective = tick_index
        if self.stale_after is not None:
            effective = min(tick_index, self.stale_after)
        effective = min(effective, len(self._timestamps) - 1)
        return self._view.through(effective)

    async def get_completed_bars(
        self,
        symbols: Sequence[str],
        interval: str,
        *,
        lookback_bars: int,
        as_of: Any = None,
    ) -> CompletedBarsView:
        return self.view_at(self.cursor)

    async def get_events(
        self, symbols: Sequence[str], *, since: Any = None
    ) -> list[MarketEvent]:
        return []


class ReplayBroker:
    """PaperBroker with injectable fill faults."""

    def __init__(
        self,
        *,
        reject_fills: bool = False,
        ambiguous_fill_at: int | None = None,
        venue_positions: dict[str, PositionRecord] | None = None,
    ) -> None:
        self.capabilities = PREFLIGHT_CAPS
        self._paper = PaperBroker(capabilities=PREFLIGHT_CAPS)
        self.reject_fills = reject_fills
        self.ambiguous_fill_at = ambiguous_fill_at
        self.venue_positions = venue_positions if venue_positions is not None else None
        self.place_calls = 0

    async def place(
        self,
        intent: OrderIntent,
        *,
        timestamp: str,
        price: float | None = None,
    ) -> FillEvent:
        self.place_calls += 1
        if self.reject_fills:
            return FillEvent(
                status="rejected",
                venue=intent.venue,
                symbol=intent.symbol,
                side=intent.side,
                error="preflight: injected rejection",
                client_order_id=intent.client_order_id,
                timestamp=timestamp,
            )
        if self.ambiguous_fill_at == self.place_calls:
            return FillEvent(
                status="ambiguous",
                venue=intent.venue,
                symbol=intent.symbol,
                side=intent.side,
                error="preflight: injected ambiguous response",
                client_order_id=intent.client_order_id,
                timestamp=timestamp,
            )
        return await self._paper.place(intent, timestamp=timestamp, price=price)

    async def fetch_state(self, symbols: Any = ()) -> VenueState:
        return VenueState(
            positions=dict(self.venue_positions or {}), source="preflight"
        )

    async def get_capacity(self, symbol: str, side: str) -> TradeCapacity:
        return TradeCapacity(safe=True, source="preflight")

    async def cancel(self, client_order_id: str) -> FillEvent:
        return FillEvent(status="rejected", venue="preflight", symbol="", side="")


class ReplayAdapter:
    name = "replay"
    capabilities = PREFLIGHT_CAPS

    def __init__(self, feed: ReplayFeed, broker: ReplayBroker) -> None:
        self.feed = feed
        self.broker = broker


def run_preflight(
    job_id: str,
    *,
    store: JobStore | None = None,
    candidate_dir: str | Path | None = None,
    max_ticks: int = 50,
) -> dict[str, Any]:
    """Drive the ACTUAL driver tick path (not the simulator) over replayed
    data, then over adversarial fault scenarios. This is the behavioral answer
    to 'will the system do what the plan meant' — the same code that will run
    live is exercised, with the runner's failure modes injected."""
    store = store or JobStore()
    root = Path(candidate_dir) if candidate_dir else store.job_dir(job_id)
    job_data = _load_job_yaml(root)
    checks: list[dict[str, Any]] = []
    revision = compute_workspace_revision(root)

    if str(job_data.get("execution_contract") or "legacy") != "jobs_v1":
        checks.append(
            {
                "name": "execution_contract_jobs_v1",
                "passed": False,
                "hint": "preflight requires the jobs_v1 driver contract",
            }
        )
        return _write_report(store, job_id, root, checks, revision, candidate_dir)
    checks.append({"name": "execution_contract_jobs_v1", "passed": True})

    spec_data, _ = resolve_execution_spec(root, job_data)
    spec = ExecutionSpec.from_dict(spec_data)
    # Sandbox ticks resolve the spec from job data, not from the real job dir,
    # so embed it explicitly (it may live in execution_spec.json).
    job_data = {**job_data, "execution_spec": spec.to_dict()}
    try:
        dataset = _load_dataset(root, spec, job_data)
    except FileNotFoundError as exc:
        if candidate_dir:
            # Candidates carry workspace + job.yaml only; reuse the active
            # job's dataset so preflight exercises the same bars.
            try:
                dataset = _load_dataset(store.job_dir(job_id), spec, job_data)
            except FileNotFoundError:
                dataset = None
        else:
            dataset = None
        if dataset is None:
            checks.append(
                {"name": "dataset_available", "passed": False, "error": str(exc)}
            )
            return _write_report(store, job_id, root, checks, revision, candidate_dir)
    checks.append({"name": "dataset_available", "passed": True})

    bars = dataset.bars.to_rows()
    tick_count = min(len(dataset.bars.timestamps), max_ticks)

    import asyncio

    outcome = asyncio.run(
        _run_scenarios(
            job_data=job_data,
            root=root,
            store=store,
            candidate_dir=candidate_dir,
            bars=bars,
            tick_count=tick_count,
        )
    )
    checks.extend(outcome)
    return _write_report(store, job_id, root, checks, revision, candidate_dir)


async def _run_scenarios(
    *,
    job_data: dict[str, Any],
    root: Path,
    store: JobStore,
    candidate_dir: str | Path | None,
    bars: list[dict[str, Any]],
    tick_count: int,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    job = WayfinderJob.from_dict(job_data)
    entrypoint = store.resolve_script_entrypoint(
        job.id, job_data, candidate_dir=candidate_dir
    )

    async def drive(
        sandbox: Path,
        *,
        mode: str = "paper",
        feed: ReplayFeed,
        broker: ReplayBroker,
        ticks: int,
        now_offset: pd.Timedelta | None = None,
        duplicate_at: int | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        adapter = ReplayAdapter(feed, broker)
        tick_index = 0
        while tick_index < ticks:
            feed.cursor = tick_index
            view = feed.view_at(tick_index)
            now = view.timestamps[-1] + (now_offset or pd.Timedelta(0))
            try:
                result = await tick_job(
                    job,
                    sandbox,
                    mode,
                    store=store,
                    adapters={"replay": adapter, "hyperliquid": adapter},
                    now=now,
                    entrypoint=entrypoint,
                )
            except PurityViolation as exc:
                result = {"ok": False, "error": f"purity: {exc}", "purity": False}
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            results.append(result)
            if duplicate_at is not None and tick_index == duplicate_at:
                duplicate_at = None
                continue  # rerun the same tick index -> same view, must skip
            tick_index += 1
        return results

    def sandbox_dir(name: str) -> Path:
        sandbox = root / "reports" / "preflight" / "sandbox" / name
        if sandbox.exists():
            shutil.rmtree(sandbox)
        sandbox.mkdir(parents=True, exist_ok=True)
        (sandbox / "job.yaml").write_text(
            json.dumps(job_data, default=str), encoding="utf-8"
        )
        return sandbox

    # --- happy path -------------------------------------------------------
    sandbox = sandbox_dir("happy")
    results = await drive(
        sandbox,
        feed=ReplayFeed(bars),
        broker=ReplayBroker(),
        ticks=tick_count,
    )
    completed = all(result.get("ok") for result in results)
    checks.append(
        {
            "name": "driver_ticks_complete",
            "passed": completed,
            "ticks": len(results),
            "errors": [r.get("error") for r in results if not r.get("ok")][:5],
        }
    )
    checks.append(
        {
            "name": "purity_ok",
            "passed": not any(result.get("purity") is False for result in results),
        }
    )
    bar_timestamps = [
        r["bar_timestamp"]
        for r in results
        if not r.get("skipped") and r.get("bar_timestamp")
    ]
    checks.append(
        {
            "name": "no_lookahead",
            "passed": bar_timestamps == sorted(bar_timestamps),
        }
    )
    fill_count = sum(len(result.get("fills") or []) for result in results)
    checks.append(
        {
            "name": "produced_trades",
            "passed": fill_count > 0,
            "blocking": False,
            "fill_count": fill_count,
        }
    )

    # --- stale feed: no opens against dead data ---------------------------
    sandbox = sandbox_dir("stale")
    broker = ReplayBroker()
    stale_results = await drive(
        sandbox,
        feed=ReplayFeed(bars, stale_after=0),
        broker=broker,
        ticks=min(3, tick_count),
        now_offset=pd.Timedelta(days=365),
    )
    checks.append(
        {
            "name": "stale_tick_no_open",
            "passed": all(
                result.get("skipped") or not result.get("intents")
                for result in stale_results
            )
            and broker.place_calls == 0,
        }
    )

    # --- rejected fills must not create positions -------------------------
    sandbox = sandbox_dir("rejected")
    rejected_results = await drive(
        sandbox,
        feed=ReplayFeed(bars),
        broker=ReplayBroker(reject_fills=True),
        ticks=tick_count,
    )
    final_positions = (
        rejected_results[-1].get("positions") if rejected_results else {}
    )
    checks.append(
        {
            "name": "rejected_fill_no_state_clear",
            "passed": not final_positions
            and all(result.get("ok") for result in rejected_results),
        }
    )

    # --- ambiguous fill must never read as success -------------------------
    sandbox = sandbox_dir("ambiguous")
    ambiguous_results = await drive(
        sandbox,
        feed=ReplayFeed(bars),
        broker=ReplayBroker(ambiguous_fill_at=1),
        ticks=tick_count,
    )
    ambiguous_ok = True
    saw_ambiguous = False
    for result in ambiguous_results:
        for fill in result.get("fills") or []:
            if fill.get("status") == "ambiguous":
                saw_ambiguous = True
                if fill.get("filled_size"):
                    ambiguous_ok = False
    checks.append(
        {
            "name": "ambiguous_fill_no_success_report",
            "passed": ambiguous_ok,
            "exercised": saw_ambiguous,
        }
    )

    # --- restart mid-position: adopt venue state, don't duplicate ---------
    sandbox = sandbox_dir("restart")
    seed_broker = ReplayBroker()
    seed_results = await drive(
        sandbox,
        feed=ReplayFeed(bars),
        broker=seed_broker,
        ticks=min(4, tick_count),
    )
    held = {}
    for result in seed_results:
        held = result.get("positions") or held
    venue_positions = {
        symbol: PositionRecord(
            symbol=symbol,
            side=str(record.get("side") or "long"),
            size=float(record.get("size") or 0.0),
            avg_price=float(record.get("avg_price") or 0.0),
        )
        for symbol, record in held.items()
    }
    (sandbox / "state" / "engine_state.json").unlink(missing_ok=True)
    restart_broker = ReplayBroker(venue_positions=venue_positions)
    restart_results = await drive(
        sandbox,
        mode="live",
        feed=ReplayFeed(bars),
        broker=restart_broker,
        ticks=min(5, tick_count),
    )
    recovered = any(
        set(result.get("positions") or {}) >= set(venue_positions)
        for result in restart_results
    )
    checks.append(
        {
            "name": "restart_recovers_position",
            "passed": recovered if venue_positions else True,
            "seeded_positions": sorted(venue_positions),
        }
    )

    # --- duplicate tick idempotency ----------------------------------------
    sandbox = sandbox_dir("duplicate")
    dup_broker = ReplayBroker()
    dup_results = await drive(
        sandbox,
        feed=ReplayFeed(bars),
        broker=dup_broker,
        ticks=min(3, tick_count),
        duplicate_at=1,
    )
    skips = [r for r in dup_results if r.get("skip_reason") == "no_new_bar"]
    checks.append(
        {
            "name": "duplicate_tick_idempotent",
            "passed": len(skips) >= 1,
        }
    )

    shutil.rmtree(root / "reports" / "preflight" / "sandbox", ignore_errors=True)
    return checks


def _write_report(
    store: JobStore,
    job_id: str,
    root: Path,
    checks: list[dict[str, Any]],
    revision: str,
    candidate_dir: str | Path | None,
) -> dict[str, Any]:
    failed_blocking = [
        check
        for check in checks
        if not check["passed"] and check.get("blocking") is not False
    ]
    report = {
        "status": "passed" if not failed_blocking else "failed",
        "checks": checks,
        "revision": revision,
        "generated_at": utc_now_iso(),
    }
    path = root / "reports" / "preflight" / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return report
