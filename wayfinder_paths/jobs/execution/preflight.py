from __future__ import annotations

import asyncio
import json
import math
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.ccxt_feed import (
    default_quote,
    fetch_ccxt_dataset_rows,
    fetch_ccxt_funding_rows,
)
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
    quote: str | None = None,
    feed: Any | None = None,
    incremental: bool = True,
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

    # Incremental refresh: with 120d windows a full refetch re-downloads
    # ~100k+ bars to add a few hours of tail. When the existing dataset has
    # compatible provenance (same source/exchange/symbols/interval) and
    # reaches back far enough for the requested window, fetch only the
    # missing tail (+2-bar overlap) and merge; anything else falls back to a
    # full fetch.
    previous_rows: list[dict[str, Any]] = []
    fetch_days: float = float(days)
    if incremental:
        previous_rows, fetch_days = _incremental_plan(
            root,
            source=source,
            exchange=exchange,
            symbols=symbols,
            interval=str(bar_interval),
            days=days,
            bar_seconds=bar_seconds,
        )

    if source == "ccxt":
        if feed is not None:
            lookback_bars = max(2, int(fetch_days * 86_400 / bar_seconds))
            view = asyncio.run(
                feed.get_completed_bars(
                    symbols, str(bar_interval), lookback_bars=lookback_bars
                )
            )
            rows = view.to_rows()
            source_metadata = {
                "exchange": exchange,
                "market_type": market_type,
                "quote": getattr(feed, "quote", None)
                or quote
                or default_quote(exchange),
                "symbol_map": dict(feed.symbol_map),
                "label_convention": "close_time",
            }
        else:
            rows, source_metadata = asyncio.run(
                fetch_ccxt_dataset_rows(
                    symbols,
                    str(bar_interval),
                    days=max(1, math.ceil(fetch_days)),
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
        lookback_bars = max(2, int(fetch_days * 86_400 / bar_seconds))
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
        # Probe the long-history source's market list so the evidence gate
        # can distinguish "venue-capped but ccxt has years" (must refetch
        # via ccxt) from "these symbols do not exist on ccxt at all" (HIP-3
        # equity perps like xyz:MU) — the latter is legitimate proof of
        # unavailability that a raising ccxt fetch can never leave behind.
        missing = _ccxt_missing_markets(symbols, exchange=exchange, quote=quote)
        if missing is not None:
            metadata["ccxt_missing_markets"] = missing
    if not rows and not previous_rows:
        raise RuntimeError("no bars returned while building live dataset")
    if previous_rows:
        rows = _merge_dataset_rows(previous_rows, rows, days=days)
        metadata["incremental"] = True
    # Requested-vs-received rides the persisted metadata: it is the PROOF the
    # evidence-window gate accepts for "more history does not exist" (a short
    # window is only excusable when the full target was requested and the
    # source could not supply it).
    stamps_all = sorted({str(row.get("timestamp")) for row in rows})
    if stamps_all:
        span_days = round(
            (pd.Timestamp(stamps_all[-1]) - pd.Timestamp(stamps_all[0])).total_seconds()
            / 86_400,
            1,
        )
        metadata["days_received"] = span_days
    path = root / "results" / "backtest" / "input_bars.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"bars": rows, "metadata": metadata}, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    result: dict[str, Any] = {
        "path": str(path),
        "bars": len(rows),
        "metadata": metadata,
    }
    # Requested-vs-received: venue feeds cap history silently — an agent asked
    # for 720 days, got 290, and analyzed away without noticing the shortfall.
    stamps = sorted({str(row.get("timestamp")) for row in rows})
    if stamps:
        first = pd.Timestamp(stamps[0])
        last = pd.Timestamp(stamps[-1])
        days_received = round((last - first).total_seconds() / 86_400, 1)
        result["first_ts"] = str(first)
        result["last_ts"] = str(last)
        result["days_requested"] = days
        result["days_received"] = days_received
        if days_received < 0.9 * float(days):
            result["warning"] = (
                f"received {days_received} days of {days} requested — the "
                "venue caps history. For longer windows use "
                "dataset_source='ccxt' (exchange='binance')."
            )
    # Derived research columns follow the dataset unconditionally — a fresh
    # dataset with frozen btc_trend/cross columns is the silent-staleness bug
    # (stale values merge cleanly into every scan frame, no error anywhere).
    # force=0 bypasses the hourly stamp gate; the helper never raises and
    # journals failures, so a broken exog feed cannot fail the dataset build.
    from wayfinder_paths.jobs.derived_features import (
        refresh_derived_features_if_stale,
    )

    # refresh_dataset=False: this call runs INSIDE the dataset build — the
    # helper's own dataset-staleness hook calling back into build_live_dataset
    # would recurse.
    result["derived_features"] = refresh_derived_features_if_stale(
        job_id, store=store, max_age_seconds=0, refresh_dataset=False
    )
    return result


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
        self.venue_positions = venue_positions
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
    final_positions = rejected_results[-1].get("positions") if rejected_results else {}
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
            if fill["status"] == "ambiguous":
                saw_ambiguous = True
                if fill["filled_size"]:
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
            side=record["side"],
            size=float(record["size"]),
            avg_price=float(record["avg_price"]),
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
    path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    return report


def _ccxt_missing_markets(
    symbols: list[str], *, exchange: str, quote: str | None
) -> list[str] | None:
    """Symbols with no swap OR spot market on the long-history exchange.
    None = probe failed (network etc.) — record nothing so the evidence gate
    stays conservative."""
    quote = quote or default_quote(exchange)
    try:
        import ccxt

        client = getattr(ccxt, exchange)()
        markets = client.load_markets()
        return [
            coin
            for coin in symbols
            if not any(
                (market := markets.get(pair)) and market.get("active")
                for pair in (f"{coin}/{quote}:{quote}", f"{coin}/{quote}")
            )
        ]
    except Exception:  # noqa: BLE001 — best-effort probe only
        return None


def _incremental_plan(
    root: Path,
    *,
    source: str,
    exchange: str,
    symbols: list[str],
    interval: str,
    days: int,
    bar_seconds: int,
) -> tuple[list[dict[str, Any]], float]:
    """(previous_rows, fetch_days): reuse the on-disk dataset when provenance
    matches and it reaches back far enough; otherwise ([], days) = full."""
    path = root / "results" / "backtest" / "input_bars.json"
    if not path.exists():
        return [], float(days)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return [], float(days)
    rows = doc.get("bars") if isinstance(doc, dict) else None
    meta = doc.get("metadata") if isinstance(doc, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(meta, dict):
        return [], float(days)
    if str(meta.get("source") or "") != source:
        return [], float(days)
    if source == "ccxt" and str(meta.get("exchange") or "") != exchange:
        return [], float(days)
    if str(meta.get("interval") or "") != interval:
        return [], float(days)
    if set(map(str, meta.get("symbols") or [])) != set(symbols):
        return [], float(days)
    stamps = sorted(str(row.get("timestamp")) for row in rows if row.get("timestamp"))
    if not stamps:
        return [], float(days)
    now = pd.Timestamp.now(tz="UTC")
    oldest = pd.Timestamp(stamps[0])
    newest = pd.Timestamp(stamps[-1])
    # The kept file must reach back far enough for the requested window —
    # incremental fetching can only extend the tail, never backfill. One
    # exception: when the previous fetch made the SAME days request and this
    # depth is all the source returned (venue history caps — HL serves only
    # ~5000 bars), a full refetch cannot reach further back either. Falling
    # back to full here made every refresh re-download the whole window
    # forever: hundreds of paginated candle calls per job per hour, 429
    # storms on the shared proxy throttle, and the stale-features
    # degraded/recovered flapping that followed.
    if oldest > now - pd.Timedelta(days=days) + pd.Timedelta(seconds=2 * bar_seconds):
        if int(float(meta.get("days") or 0)) != int(days):
            return [], float(days)
    gap_seconds = max((now - newest).total_seconds(), 0.0) + 2 * bar_seconds
    fetch_days = min(float(days), gap_seconds / 86_400)
    return list(rows), max(fetch_days, 2 * bar_seconds / 86_400)


def _merge_dataset_rows(
    previous_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
    *,
    days: int,
) -> list[dict[str, Any]]:
    """Union on (timestamp, symbol) — fresh rows win — trimmed to the
    trailing window anchored on the NEWEST bar (not the wall clock: a stale
    source or clock skew must never trim the dataset to empty)."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in previous_rows:
        merged[(str(row.get("timestamp")), str(row.get("symbol")))] = row
    for row in new_rows:
        merged[(str(row.get("timestamp")), str(row.get("symbol")))] = row
    newest = max(pd.Timestamp(str(row.get("timestamp"))) for row in merged.values())
    cutoff = newest - pd.Timedelta(days=days)
    kept = [
        row
        for row in merged.values()
        if pd.Timestamp(str(row.get("timestamp"))) >= cutoff
    ]
    kept.sort(key=lambda row: (str(row.get("timestamp")), str(row.get("symbol"))))
    return kept


def fetch_funding_features(
    job_id: str,
    *,
    days: int = 30,
    exchange: str = "binance",
    quote: str | None = None,
    store: JobStore | None = None,
    exchange_client: Any | None = None,
) -> dict[str, Any]:
    """Fetch historical funding rates for the job's symbols into the job's
    feature store — the first-class path for carry data.

    Appends canonical rows to state/features.jsonl (deduped on
    timestamp+name+symbol, so re-fetching extends rather than duplicates) and
    declares the "funding" feature in execution_spec.data_contract.features
    when missing, so both backtest and live as-of merge a `funding` column
    onto each symbol's bars. Declaring the feature restamps the workspace
    revision — consuming new data IS a strategy change and re-gates promotion.
    """
    store = store or JobStore()
    root = store.job_dir(job_id)
    job_data = _load_job_yaml(root)
    spec_data, spec_path = resolve_execution_spec(root, job_data)
    spec = ExecutionSpec.from_dict(spec_data)
    params = dict(job_data.get("execution_params") or {})
    symbols = [
        str(symbol)
        for symbol in (params.get("symbols") or spec.data_contract.get("symbols") or [])
    ]
    if not symbols:
        raise ValueError("no symbols configured for funding fetch")

    rows, metadata = asyncio.run(
        fetch_ccxt_funding_rows(
            symbols,
            days=days,
            exchange_id=exchange,
            quote=quote,
            exchange=exchange_client,
        )
    )

    features_path = root / "state" / "features.jsonl"
    features_path.parent.mkdir(parents=True, exist_ok=True)
    existing: set[tuple[str, str, str]] = set()
    if features_path.exists():
        for line in features_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                existing.add(
                    (
                        str(row.get("timestamp")),
                        str(row.get("name")),
                        str(row.get("symbol")),
                    )
                )
    written_at = pd.Timestamp.now(tz="UTC").isoformat()
    appended = 0
    with features_path.open("a", encoding="utf-8") as handle:
        for row in rows:
            key = (str(row["timestamp"]), str(row["name"]), str(row["symbol"]))
            if key in existing:
                continue
            existing.add(key)
            handle.write(json.dumps({**row, "written_at": written_at}) + "\n")
            appended += 1

    declared = False
    target = spec_path if spec_path is not None else root / "execution_spec.json"
    if rows and target.exists():
        spec_doc = json.loads(target.read_text(encoding="utf-8"))
        contract = spec_doc.setdefault("data_contract", {})
        features = contract.setdefault("features", [])
        if not any(
            isinstance(item, dict) and item.get("name") == "funding"
            for item in features
        ):
            features.append({"name": "funding"})
            target.write_text(json.dumps(spec_doc, indent=2) + "\n", encoding="utf-8")
            declared = True

    result: dict[str, Any] = {
        "rows_fetched": len(rows),
        "rows_appended": appended,
        "per_symbol": metadata.get("per_symbol", {}),
        "features_path": str(features_path),
        "feature_declared_now": declared,
        "metadata": metadata,
    }
    missing_symbols = sorted(
        symbol
        for symbol in symbols
        if int((metadata.get("per_symbol") or {}).get(symbol) or 0) == 0
    )
    result["missing_symbols"] = missing_symbols
    result["symbol_coverage_fraction"] = round(
        (len(symbols) - len(missing_symbols)) / len(symbols), 3
    )
    warnings: list[str] = []
    if missing_symbols:
        warnings.append("no funding history for symbols: " + ", ".join(missing_symbols))
    stamps = sorted(str(row.get("timestamp")) for row in rows)
    if stamps:
        first = pd.Timestamp(stamps[0])
        last = pd.Timestamp(stamps[-1])
        days_received = round((last - first).total_seconds() / 86_400, 1)
        result["first_ts"] = str(first)
        result["last_ts"] = str(last)
        result["days_requested"] = days
        result["days_received"] = days_received
        if days_received < 0.9 * float(days):
            warnings.append(
                f"funding history covers {days_received} days of {days} "
                "requested — bars outside this span carry NaN funding, which "
                "biases any funding-vs-price-signal comparison. Match the "
                "candle window or note the shortfall in your analysis."
            )
    if warnings:
        result["warning"] = " ".join(warnings)
    return result
