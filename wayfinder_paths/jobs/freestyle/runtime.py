"""The freestyle tick runtime: loads the author's ``tick(ctx)``, hands it a
context whose only trading seam is ``ctx.act``, and records everything
through the same forward recorder the jobs_v1 driver uses.

Paper and live differ only in the venue broker (``build_adapter`` returns the
venue's paper broker or its real one); quotes, the ledger, risk halts and
telemetry are shared. A dry run (validation) swaps the venue gateway for
stub marks so nothing touches the network.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import signal
import sys
import time
import traceback
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.execution.engine import EngineState
from wayfinder_paths.jobs.execution.job import _load_job_yaml
from wayfinder_paths.jobs.execution.paper import PaperBroker
from wayfinder_paths.jobs.execution.primitives import (
    CompletedBarsView,
    FillEvent,
    OrderIntent,
    PositionLedger,
)
from wayfinder_paths.jobs.execution.risk import check_risk_halt
from wayfinder_paths.jobs.execution.venues import build_adapter
from wayfinder_paths.jobs.forward import ForwardRecorder
from wayfinder_paths.jobs.freestyle.contract import (
    SUPPORTED_VENUES,
    ActionResult,
    FreestyleSpec,
    normalize_action,
)
from wayfinder_paths.jobs.gating import compute_workspace_revision
from wayfinder_paths.jobs.halt import read_halt, request_halt
from wayfinder_paths.jobs.models import WayfinderJob, utc_now_iso
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.triggers import fire_triggers
from wayfinder_paths.runner.monitor_state import atomic_write_json

LEDGER_PATH = "state/freestyle_ledger.json"
STATE_PATH = "state/freestyle_state.json"
DRY_RUN_RESULT_PATH = "reports/validation/dryrun/result.json"
JOB_RESULT_MARKER = "WAYFINDER_JOB_RESULT "
DEFAULT_INITIAL_CAPITAL = 1_000.0
# Per-venue paper fill assumptions (taker). Overridable through
# execution_params.freestyle.venue_params.<venue>.
DEFAULT_VENUE_PARAMS: dict[str, dict[str, float]] = {
    "hyperliquid": {"fee_bps": 4.5, "slippage_bps": 5.0},
    "hyperliquid_prediction": {"fee_bps": 0.0, "slippage_bps": 0.0},
    "polymarket": {"fee_bps": 0.0, "slippage_bps": 0.0},
}


class FreestyleRefusal(RuntimeError):
    """The runtime refused to run at all (identity drift, bad contract)."""


class TickTimeout(RuntimeError):
    pass


class VenueGateway:
    """Quotes, fills and market events per venue through the execution
    registry: the venue's paper broker in paper mode, its real broker live."""

    def __init__(
        self, *, mode: str, params: Mapping[str, Any], quote_interval: str
    ) -> None:
        self.mode = mode
        self.params = dict(params)
        self.quote_interval = quote_interval
        self._adapters: dict[str, Any] = {}

    def adapter(self, venue: str) -> Any:
        if venue not in self._adapters:
            venue_params = dict(DEFAULT_VENUE_PARAMS.get(venue) or {})
            venue_params.update(
                dict(
                    (
                        (self.params.get("freestyle") or {}).get("venue_params") or {}
                    ).get(venue)
                    or {}
                )
            )
            if self.mode == "live" and self.params.get("wallet_label"):
                venue_params["wallet_label"] = self.params["wallet_label"]
            self._adapters[venue] = build_adapter(
                venue, mode=self.mode, params=venue_params
            )
        return self._adapters[venue]

    def quote(self, venue: str, symbol: str) -> float:
        view = _run(
            self.adapter(venue).feed.get_completed_bars(
                [symbol], self.quote_interval, lookback_bars=2
            )
        )
        if symbol not in view.symbols:
            raise LookupError(f"{venue} returned no bars for {symbol}")
        return float(view.latest(symbol)["close"])

    def place(self, intent: OrderIntent, *, price: float, timestamp: str) -> FillEvent:
        return _run(
            self.adapter(intent.venue).broker.place(
                intent, timestamp=timestamp, price=price
            )
        )

    def resolutions(self, venue: str, symbols: list[str]) -> list[dict[str, Any]]:
        feed = self.adapter(venue).feed
        get_events = getattr(feed, "get_events", None)
        if get_events is None or not symbols:
            return []
        events = _run(get_events(symbols))
        return [
            {"symbol": event.symbol, "value": float(event.payload.get("value") or 0.0)}
            for event in events
            if event.kind == "resolution"
        ]


class StubVenueGateway:
    """Dry-run gateway: deterministic marks, paper fills, no network."""

    def __init__(
        self, *, marks: Mapping[str, float] | None = None, tick_index: int = 0
    ) -> None:
        self.marks = {str(k): float(v) for k, v in (marks or {}).items()}
        self.tick_index = tick_index
        self._brokers: dict[str, PaperBroker] = {}

    def quote(self, venue: str, symbol: str) -> float:
        # A settled market quotes at its resolution value from the second
        # tick on, so a script cannot keep buying a market that has resolved.
        resolved = self.marks.get(f"resolution:{venue}:{symbol}")
        if resolved is not None and self.tick_index >= 1:
            return float(resolved)
        base = self.marks.get(f"{venue}:{symbol}", self.marks.get(symbol))
        if base is None:
            base = 0.5 if venue in {"polymarket", "hyperliquid_prediction"} else 100.0
        # A gentle drift so a multi-tick dry run exercises mark-to-market.
        return base * (1.0 + 0.001 * self.tick_index)

    def place(self, intent: OrderIntent, *, price: float, timestamp: str) -> FillEvent:
        broker = self._brokers.get(intent.venue)
        if broker is None:
            defaults = DEFAULT_VENUE_PARAMS.get(intent.venue) or {}
            broker = PaperBroker(
                fee_bps=float(defaults.get("fee_bps") or 0.0),
                slippage_bps=float(defaults.get("slippage_bps") or 0.0),
            )
            self._brokers[intent.venue] = broker
        return _run(broker.place(intent, timestamp=timestamp, price=price))

    def resolutions(self, venue: str, symbols: list[str]) -> list[dict[str, Any]]:
        # A dry run settles a market when the marks carry
        # resolution:<venue>:<symbol> and at least one tick has passed, so a
        # script can be seen buying on tick one and settling on tick two.
        if self.tick_index < 1:
            return []
        resolved = []
        for symbol in symbols:
            value = self.marks.get(f"resolution:{venue}:{symbol}")
            if value is not None:
                resolved.append({"symbol": symbol, "value": float(value)})
        return resolved


class FreestyleContext:
    """What ``tick(ctx)`` sees. Reads are free; ``act`` is the only write."""

    def __init__(
        self,
        *,
        job: WayfinderJob,
        root: Path,
        mode: str,
        now: datetime,
        spec: FreestyleSpec,
        gateway: Any,
        ledger: PositionLedger,
        recorder: ForwardRecorder,
        store: JobStore | None,
        dry_run: bool,
        halted: bool,
    ) -> None:
        self.job = job
        self.root = root
        self.mode = mode
        self.now = now
        self.spec = spec
        self.dry_run = dry_run
        self.params: dict[str, Any] = dict(job.execution_params or {})
        self._gateway = gateway
        self._ledger = ledger
        self._recorder = recorder
        self._store = store
        self.halted = halted
        self.state: dict[str, Any] = _read_json(root / STATE_PATH) or {}
        self.marks: dict[str, float] = {}
        self.actions: list[dict[str, Any]] = []
        self.fills: list[dict[str, Any]] = []
        self.logs: list[str] = []
        self.notifications: list[dict[str, Any]] = []
        self.unpapered_actions: list[str] = []
        self.halt_reason: str | None = None
        self._tick_notional = 0.0
        self.venues_used: set[str] = set()

    # ---- reads -----------------------------------------------------------
    @property
    def positions(self) -> dict[str, dict[str, Any]]:
        return {
            symbol: record.to_dict()
            for symbol, record in self._ledger.positions.items()
        }

    @property
    def realized_pnl(self) -> float:
        return float(self._ledger.realized_pnl)

    def quote(self, venue: str, symbol: str) -> float:
        venue = str(venue).lower()
        price = float(self._gateway.quote(venue, symbol))
        self.marks[f"{venue}:{symbol}"] = price
        self.venues_used.add(venue)
        return price

    def log(self, message: str) -> None:
        self.logs.append(str(message)[:500])

    # ---- writes ----------------------------------------------------------
    def act(self, action: Mapping[str, Any]) -> ActionResult:
        try:
            symbol = str(action.get("symbol") or "")
            held = self._ledger.positions.get(symbol)
            intent = normalize_action(action, position_side=held.side if held else None)
        except (ValueError, TypeError) as exc:
            return self._refuse(dict(action), f"invalid action: {exc}")
        refusal = self._precheck(intent)
        if refusal:
            return self._refuse(intent.to_dict(), refusal)
        try:
            price = self.marks.get(f"{intent.venue}:{intent.symbol}")
            if price is None:
                price = self.quote(intent.venue, intent.symbol)
        except Exception as exc:  # noqa: BLE001 — a quote failure is a refusal, not a crash
            return self._refuse(intent.to_dict(), f"quote failed: {exc}")
        if intent.action == "CLOSE" and intent.size is None and intent.notional is None:
            held = self._ledger.positions.get(intent.symbol)
            intent.size = float(held.size) if held else None
        return self._execute(intent, price)

    def notify(self, title: str, body: str, *, delivery: str = "email") -> None:
        key = str(title)[:200]
        if any(item["title"] == key for item in self.notifications):
            return
        self.notifications.append(
            {"title": key, "body": str(body)[:20_000], "delivery": delivery}
        )

    def halt(self, reason: str, *, flatten: bool = False) -> None:
        self.halted = True
        self.halt_reason = str(reason)
        if self._store is not None and not self.dry_run:
            request_halt(
                self._store,
                self.job.id,
                reason=str(reason),
                flatten=flatten,
                source="freestyle_script",
            )

    def custom(self, label: str, coro: Any) -> Any:
        """Escape hatch for venue calls the runtime cannot paper. Skipped in
        paper and dry runs (reported as unpapered), executed live only when
        the job allows custom actions and SPEC acknowledges the risk."""
        label = str(label)
        allowed = bool((self.params.get("freestyle") or {}).get("allow_custom_actions"))
        if (
            self.mode != "live"
            or self.dry_run
            or not (allowed and self.spec.custom_risk_acknowledged)
        ):
            self.unpapered_actions.append(label)
            if hasattr(coro, "close"):
                coro.close()
            return None
        return _run(coro)

    # ---- internals -------------------------------------------------------
    def _precheck(self, intent: OrderIntent) -> str | None:
        if intent.venue not in SUPPORTED_VENUES:
            return f"venue {intent.venue!r} is not supported by the freestyle runtime"
        if self.spec.venues and intent.venue not in self.spec.venues:
            return f"venue {intent.venue!r} is outside SPEC.venues"
        if self.spec.symbols and intent.symbol not in self.spec.symbols:
            return f"symbol {intent.symbol!r} is outside SPEC.symbols"
        if self.halted and intent.action == "OPEN":
            return "job is halted: openers are refused, exits still flow"
        if intent.action == "OPEN":
            notional = intent.notional
            if notional is None and intent.size is not None:
                mark = self.marks.get(f"{intent.venue}:{intent.symbol}")
                notional = abs(intent.size) * mark if mark else None
            cap = self.spec.max_notional_per_tick
            if (
                cap is not None
                and notional is not None
                and self._tick_notional + notional > cap
            ):
                return f"max_notional_per_tick {cap} would be exceeded"
        return None

    def _execute(self, intent: OrderIntent, price: float) -> ActionResult:
        timestamp = self.now.isoformat()
        try:
            fill = self._gateway.place(intent, price=price, timestamp=timestamp)
        except Exception as exc:  # noqa: BLE001 — venue transport failures are recorded, never raised
            fill = FillEvent(
                status="rejected",
                venue=intent.venue,
                symbol=intent.symbol,
                side=intent.side,
                error=f"venue error: {exc}",
                timestamp=timestamp,
                client_order_id=intent.client_order_id,
            )
        realized_before = self._ledger.realized_pnl
        if fill.successful:
            self._ledger.apply_fill(fill)
            held = self._ledger.positions.get(intent.symbol)
            if held is not None:
                # The venue rides on the position so marks, settlement and
                # equity know where to look after a restart.
                held.metadata["venue"] = intent.venue
            if intent.action == "OPEN":
                self._tick_notional += abs(
                    float(fill.filled_size) * float(fill.avg_price or price)
                )
        order_row = {
            **intent.to_dict(),
            "ts": timestamp,
            "mode": self.mode,
            "status": fill.status,
            "requested_price": price,
        }
        self._recorder.record_order(order_row)
        fill_row = {
            **fill.to_dict(),
            "ts": timestamp,
            "mode": self.mode,
            "intent_action": intent.action,
            "intent_metadata": dict(intent.metadata),
        }
        self._recorder.record_fill(fill_row)
        if fill.successful and intent.action == "CLOSE":
            delta = float(self._ledger.realized_pnl - realized_before)
            self._recorder.record_trade_close(
                {
                    "ts": timestamp,
                    "symbol": intent.symbol,
                    "venue": intent.venue,
                    "side": intent.side,
                    "size": fill.filled_size,
                    "avg_price": fill.avg_price,
                    "fee": fill.fee,
                    "net_pnl": delta,
                    "pnl": delta,
                    "exit_reason": intent.metadata.get("exit_reason"),
                    "mode": self.mode,
                }
            )
        self.venues_used.add(intent.venue)
        result = ActionResult(
            status=fill.status if fill.status in {"filled", "resting"} else "rejected",
            reason=fill.error,
            intent=intent.to_dict(),
            fill=fill.to_dict(),
        )
        self.actions.append(result.to_dict())
        if fill.successful:
            self.fills.append(fill_row)
        return result

    def _refuse(self, intent: dict[str, Any], reason: str) -> ActionResult:
        result = ActionResult(status="refused", reason=reason, intent=intent)
        self.actions.append(result.to_dict())
        return result


def load_tick_module(entrypoint: Path) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"freestyle_{entrypoint.stem}_{abs(hash(str(entrypoint)))}", entrypoint
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {entrypoint}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_freestyle_tick(
    job_dir: str | Path | None = None,
    *,
    dry_run: bool = False,
    ticks: int = 1,
    marks: Mapping[str, float] | None = None,
    now: datetime | None = None,
    entrypoint: str | Path | None = None,
    extra_sys_path: list[str] | None = None,
) -> dict[str, Any]:
    """Sync entrypoint the compiler wrapper (and the validation dry run) calls.

    ``entrypoint`` lets the Path runner point at a freestyle-kind component
    inside an installed bundle; the caller vouches for the contract then.

    Returns the tick payload; ``ok`` False with ``refused`` True means the
    runtime declined to run (identity drift, wrong contract) and the wrapper
    exits 2 so the runner records the refusal distinctly from a script crash.
    """
    root = Path(job_dir or os.environ["WAYFINDER_JOB_DIR"])
    dry_run = dry_run or os.environ.get("WAYFINDER_DRY_RUN") == "1"
    mode = os.environ.get("WAYFINDER_JOB_MODE") or "paper"
    store: JobStore | None = None
    job: WayfinderJob | None = None
    payload: dict[str, Any]
    try:
        job = WayfinderJob.from_dict(_load_job_yaml(root))
        for extra in extra_sys_path or []:
            if extra not in sys.path:
                sys.path.insert(0, extra)
        allowed = {"freestyle_v1", "path_v1"} if entrypoint else {"freestyle_v1"}
        if job.execution_contract not in allowed:
            raise FreestyleRefusal(
                f"job {job.id} is on the {job.execution_contract} contract, not freestyle_v1"
            )
        divergence = None
        declared_mode = str(job.script_loop.mode or "paper")
        if mode == "live" and declared_mode != "live":
            divergence = {
                "kind": "mode_divergence",
                "runner_mode": "live",
                "declared_mode": declared_mode,
                "action": "downgraded_to_paper",
            }
            mode = "paper"
        if dry_run:
            mode = "paper"
        pinned = os.environ.get("WAYFINDER_JOB_REVISION") or ""
        current = compute_workspace_revision(root)
        if pinned and not dry_run and pinned != current:
            raise FreestyleRefusal(
                f"revision drift: launched at {pinned}, workspace is {current}; "
                "re-run validate and launch"
            )
        store = None if dry_run else JobStore()
        payload = _run_ticks(
            job,
            root,
            mode=mode,
            dry_run=dry_run,
            ticks=max(1, int(ticks)),
            marks=marks,
            now=now,
            store=store,
            revision=current,
            entrypoint=Path(entrypoint) if entrypoint else None,
        )
        if divergence is not None:
            payload.setdefault("guard_events", []).append(divergence)
    except FreestyleRefusal as exc:
        payload = {"ok": False, "refused": True, "error": str(exc)}
        if job is not None and not dry_run:
            try:
                JobStore().append_journal(
                    job.id,
                    {
                        "type": "revision_drift"
                        if "drift" in str(exc)
                        else "freestyle_refused",
                        "error": str(exc)[:300],
                    },
                )
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 — the tick must always report, never crash the wrapper
        payload = {
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc()[-2000:],
        }
    if store is not None and job is not None and not dry_run:
        events = _trigger_events(payload)
        if events:
            fire_triggers(store, job, events, source="scheduled_tick")
    summary = payload.get("summary") or payload.get("error") or "freestyle tick"
    severity = "info" if payload.get("ok") else "error"
    print(json.dumps(payload, default=str))
    print(
        JOB_RESULT_MARKER
        + json.dumps({"summary": str(summary)[:1000], "severity": severity})
    )
    return payload


def _run_ticks(
    job: WayfinderJob,
    root: Path,
    *,
    mode: str,
    dry_run: bool,
    ticks: int,
    marks: Mapping[str, float] | None,
    now: datetime | None,
    store: JobStore | None,
    revision: str,
    entrypoint: Path | None = None,
) -> dict[str, Any]:
    entrypoint = entrypoint or _entrypoint(root, job)
    module = load_tick_module(entrypoint)
    tick_fn = getattr(module, "tick", None)
    if not callable(tick_fn):
        raise FreestyleRefusal(f"{entrypoint.name} does not define tick(ctx)")
    if asyncio.iscoroutinefunction(tick_fn):
        raise FreestyleRefusal(
            "tick(ctx) must be a plain function; the runtime drives venues for you"
        )
    spec = FreestyleSpec.from_any(getattr(module, "SPEC", None))
    forward_dir = os.environ.get("WAYFINDER_FORWARD_DIR") or str(
        root / "results" / "forward"
    )
    if dry_run:
        forward_dir = str(root / "reports" / "validation" / "dryrun" / "forward")
    recorder = ForwardRecorder(
        job_id=job.id,
        job_dir=root,
        forward_dir=forward_dir,
        mode=mode,
        revision=revision or None,
    )
    ledger, ledger_doc = _load_ledger(root, mode=mode, dry_run=dry_run)
    timeout = int(job.script_loop.timeout_seconds or 300)
    clock = now or datetime.now(UTC)
    last: dict[str, Any] = {}
    accumulated: dict[str, list[Any]] = {
        "actions": [],
        "fills": [],
        "logs": [],
        "guard_events": [],
        "unpapered_actions": [],
        "notifications": [],
    }
    for index in range(ticks):
        tick_now = clock + timedelta(seconds=index) if dry_run else datetime.now(UTC)
        gateway: Any = (
            StubVenueGateway(marks=marks or _validation_marks(job), tick_index=index)
            if dry_run
            else VenueGateway(
                mode=mode,
                params=job.execution_params or {},
                quote_interval=spec.quote_interval,
            )
        )
        last = _one_tick(
            job,
            root,
            mode=mode,
            dry_run=dry_run,
            now=tick_now,
            spec=spec,
            gateway=gateway,
            ledger=ledger,
            recorder=recorder,
            store=store,
            tick_fn=tick_fn,
            timeout=timeout,
            revision=revision,
        )
        _save_ledger(
            root,
            ledger,
            mode=mode,
            marks=last.get("marks") or {},
            equity=last.get("equity"),
            dry_run=dry_run,
        )
        for key, bucket in accumulated.items():
            bucket.extend(last.get(key) or [])
        if not last.get("ok"):
            break
    if dry_run:
        # The dry-run record is the whole run, not the last tick: a script
        # that opens on tick one and holds afterwards still shows its intent.
        last = {**last, **{key: list(bucket) for key, bucket in accumulated.items()}}
        last["dry_run"] = {
            "ticks": ticks,
            "spec": spec.to_dict(),
            "entrypoint": str(entrypoint),
        }
        atomic_write_json(root / DRY_RUN_RESULT_PATH, last)
    return last


def _one_tick(
    job: WayfinderJob,
    root: Path,
    *,
    mode: str,
    dry_run: bool,
    now: datetime,
    spec: FreestyleSpec,
    gateway: Any,
    ledger: PositionLedger,
    recorder: ForwardRecorder,
    store: JobStore | None,
    tick_fn: Any,
    timeout: int,
    revision: str,
) -> dict[str, Any]:
    started = time.monotonic()
    halt = None if dry_run else read_halt(root)
    guard_events: list[dict[str, Any]] = []
    if halt:
        guard_events.append({"kind": "manual_halt", "reason": halt.get("reason")})
    ctx = FreestyleContext(
        job=job,
        root=root,
        mode=mode,
        now=now,
        spec=spec,
        gateway=gateway,
        ledger=ledger,
        recorder=recorder,
        store=store,
        dry_run=dry_run,
        halted=bool(halt),
    )
    # Mark every held position first: the risk check and the equity line need
    # a price per symbol before the author's code runs.
    for symbol, record in list(ledger.positions.items()):
        venue = str(record.metadata.get("venue") or "hyperliquid")
        try:
            ctx.quote(venue, symbol)
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"mark failed for {symbol}: {exc}")
    _settle_resolutions(ctx, ledger, gateway)
    if not dry_run and not halt:
        reason = _risk_check(root, ledger, ctx, now, job)
        if reason:
            guard_events.append({"kind": "risk_halt", "reason": reason})
            ctx.halted = True
            if store is not None:
                request_halt(store, job.id, reason=reason, source="risk_limits")
    error: str | None = None
    timed_out = False
    handler = None
    if timeout > 5 and hasattr(signal, "SIGALRM"):

        def _on_alarm(signum: int, frame: Any) -> None:
            raise TickTimeout(f"tick exceeded {timeout - 5}s")

        handler = signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(max(1, timeout - 5))
    try:
        tick_fn(ctx)
    except TickTimeout as exc:
        error = str(exc)
        timed_out = True
    except Exception as exc:  # noqa: BLE001 — author errors are reported, not raised
        error = f"{type(exc).__name__}: {exc}"
        ctx.log(traceback.format_exc()[-1500:])
    finally:
        if handler is not None:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, handler)
    if ctx.halt_reason:
        guard_events.append(
            {
                "kind": "manual_halt",
                "reason": ctx.halt_reason,
                "source": "freestyle_script",
            }
        )
    if not dry_run:
        atomic_write_json(root / STATE_PATH, ctx.state)
    equity, unrealized = _equity(job, ledger, ctx.marks)
    ok = error is None
    ts = now.isoformat()
    recorder.record_tick(
        {
            "ts": ts,
            "bar_ts": ts,
            "mode": mode,
            "revision": revision,
            "ledger": ledger.snapshot(),
            "intents": [a.get("intent") for a in ctx.actions if a.get("intent")],
            "fills": list(ctx.fills),
            "guard_events": guard_events,
            "marks": dict(ctx.marks),
            "equity": equity,
            "unrealized_pnl": unrealized,
            "actions": list(ctx.actions),
            "dry_run": dry_run,
        }
    )
    status = "failed" if not ok else ("halted" if ctx.halted else "ok")
    filled = sum(1 for a in ctx.actions if a.get("status") == "filled")
    summary = (
        f"{len(ctx.actions)} action(s), {filled} filled, equity {equity:.2f}"
        if ok
        else f"tick failed: {error}"
    )
    recorder.record_run(
        status=status,
        decision={"action": "tick", "reason": summary},
        metrics={
            "equity": equity,
            "realized_pnl": float(ledger.realized_pnl),
            "unrealized_pnl": unrealized,
            "actions": len(ctx.actions),
            "fills": filled,
            "elapsed_s": round(time.monotonic() - started, 3),
        },
        mode=mode,
        ts=ts,
        error=error,
        dry_run=dry_run,
    )
    _deliver_notifications(ctx, dry_run)
    return {
        "ok": ok,
        "status": status,
        "error": error,
        "timed_out": timed_out,
        "mode": mode,
        "summary": summary,
        "actions": list(ctx.actions),
        "fills": list(ctx.fills),
        "guard_events": guard_events,
        "marks": dict(ctx.marks),
        "equity": equity,
        "unrealized_pnl": unrealized,
        "realized_pnl": float(ledger.realized_pnl),
        "positions": ctx.positions,
        "logs": list(ctx.logs),
        "notifications": list(ctx.notifications),
        "unpapered_actions": list(ctx.unpapered_actions),
        "venues_used": sorted(ctx.venues_used),
        "spec": spec.to_dict(),
        "halted": ctx.halted,
    }


def _settle_resolutions(
    ctx: FreestyleContext, ledger: PositionLedger, gateway: Any
) -> None:
    by_venue: dict[str, list[str]] = {}
    for symbol, record in ledger.positions.items():
        venue = str(record.metadata.get("venue") or "")
        if venue in {"polymarket", "hyperliquid_prediction"}:
            by_venue.setdefault(venue, []).append(symbol)
    for venue, symbols in by_venue.items():
        try:
            resolved = gateway.resolutions(venue, symbols)
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"resolution check failed on {venue}: {exc}")
            continue
        for item in resolved:
            symbol = item["symbol"]
            ctx.marks[f"{venue}:{symbol}"] = float(item["value"])
            ctx.act(
                {
                    "venue": venue,
                    "kind": "close",
                    "symbol": symbol,
                    "reason": "resolution",
                }
            )


def _risk_check(
    root: Path,
    ledger: PositionLedger,
    ctx: FreestyleContext,
    now: datetime,
    job: WayfinderJob,
) -> str | None:
    rows: list[Mapping[str, Any]] = []
    for key, mark in ctx.marks.items():
        _, _, symbol = key.partition(":")
        rows.append(
            {
                "timestamp": now.isoformat(),
                "symbol": symbol,
                "open": mark,
                "high": mark,
                "low": mark,
                "close": mark,
            }
        )
    view = CompletedBarsView.from_rows(rows)
    params = dict(job.execution_params or {})
    params.setdefault("initial_capital", DEFAULT_INITIAL_CAPITAL)
    state = EngineState(ledger=ledger, mode=ctx.mode)
    try:
        reason, _snapshot = check_risk_halt(
            root, state=state, view=view, params=params, now=pd.Timestamp(now)
        )
    except Exception as exc:  # noqa: BLE001 — a broken risk file must not silently pass
        return f"risk check failed: {exc}"
    return reason


def _equity(
    job: WayfinderJob, ledger: PositionLedger, marks: Mapping[str, float]
) -> tuple[float, float]:
    initial = float(
        (job.execution_params or {}).get("initial_capital") or DEFAULT_INITIAL_CAPITAL
    )
    unrealized = 0.0
    for symbol, record in ledger.positions.items():
        venue = str(record.metadata.get("venue") or "hyperliquid")
        mark = marks.get(f"{venue}:{symbol}")
        if mark is None:
            continue
        direction = 1 if record.side == "long" else -1
        unrealized += (
            direction * (float(mark) - float(record.avg_price)) * float(record.size)
        )
    return initial + float(ledger.realized_pnl) + unrealized, unrealized


def _load_ledger(
    root: Path, *, mode: str, dry_run: bool
) -> tuple[PositionLedger, dict[str, Any]]:
    if dry_run:
        return PositionLedger(), {}
    doc = _read_json(root / LEDGER_PATH) or {}
    if doc and str(doc.get("mode")) != mode:
        # A mode flip starts a fresh book; the old one is archived, never merged.
        atomic_write_json(root / f"state/freestyle_ledger.{doc.get('mode')}.json", doc)
        return PositionLedger(), {}
    return PositionLedger.restore(doc.get("ledger")), doc


def _save_ledger(
    root: Path,
    ledger: PositionLedger,
    *,
    mode: str,
    marks: Mapping[str, float],
    equity: Any,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    atomic_write_json(
        root / LEDGER_PATH,
        {
            "mode": mode,
            "ledger": ledger.snapshot(),
            "marks": dict(marks),
            "equity": equity,
            "updated_at": utc_now_iso(),
        },
    )


def _entrypoint(root: Path, job: WayfinderJob) -> Path:
    raw = str(job.script_loop.entrypoint or "").strip()
    if not raw:
        raise FreestyleRefusal("script_loop.entrypoint is not set")
    path = Path(raw)
    if path.is_absolute():
        return path
    if path.parts and path.parts[0] == "workspace":
        return root / path
    if ".wayfinder" in path.parts and "workspace" in path.parts:
        index = path.parts.index("workspace")
        return root.joinpath(*path.parts[index:])
    return root / "workspace" / "src" / path.name


def _validation_marks(job: WayfinderJob) -> dict[str, float]:
    raw = ((job.execution_params or {}).get("freestyle") or {}).get(
        "validation_marks"
    ) or {}
    return {str(k): float(v) for k, v in raw.items()}


def _deliver_notifications(ctx: FreestyleContext, dry_run: bool) -> None:
    if dry_run or not ctx.notifications:
        return
    try:
        from wayfinder_paths.core.clients.NotifyClient import NOTIFY_CLIENT

        for item in ctx.notifications:
            _run(
                NOTIFY_CLIENT.notify(
                    title=item["title"], message=item["body"], delivery=item["delivery"]
                )
            )
    except Exception as exc:  # noqa: BLE001 — a notification failure never fails a tick
        ctx.log(f"notification failed: {exc}")


def _trigger_events(payload: Mapping[str, Any]) -> list[str]:
    events: list[str] = []
    if payload.get("ok") is not True:
        events.append("script_failure")
    kinds = {str(event.get("kind")) for event in payload.get("guard_events") or []}
    if kinds & {"risk_halt", "manual_halt"}:
        events.append("risk_halt")
    if "mode_divergence" in kinds:
        events.append("reconcile_mismatch")
    return events


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run freestyle ticks (validation dry runs)."
    )
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ticks", type=int, default=1)
    parser.add_argument(
        "--entrypoint", default=None, help="tick module (Path components)"
    )
    parser.add_argument(
        "--sys-path", action="append", default=[], help="extra import roots"
    )
    parser.add_argument(
        "--marks", default=None, help="JSON mapping venue:symbol -> price"
    )
    args = parser.parse_args(argv)
    marks = json.loads(args.marks) if args.marks else None
    payload = run_freestyle_tick(
        args.job_dir,
        dry_run=args.dry_run,
        ticks=args.ticks,
        marks=marks,
        entrypoint=args.entrypoint,
        extra_sys_path=list(args.sys_path),
    )
    if payload.get("ok"):
        return 0
    return 2 if payload.get("refused") else 1


if __name__ == "__main__":
    raise SystemExit(main())
