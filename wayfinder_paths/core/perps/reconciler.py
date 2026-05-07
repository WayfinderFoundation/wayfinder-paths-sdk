"""Reconciliation core — replay decide() against historical state snapshots and
diff against captured live intents + (optionally) live exchange fills.

This module is the engine. Two callers consume it:
  - `ActivePerpsStrategy.reconcile(...)` — first-class entrypoint (MCP / runner)
  - `scripts/active_perps_strategy_recon.py` — CLI wrapper for ad-hoc runs
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.core.backtesting.data import (
    align_dataframes,
    fetch_funding_rates,
    fetch_prices,
)
from wayfinder_paths.core.backtesting.ref import (
    BacktestRef,
    fingerprint_frames,
    hash_module_source,
    load_ref,
)
from wayfinder_paths.core.perps.context import TriggerContext, normalize_signal
from wayfinder_paths.core.perps.handlers.reconcile import ReconcileHandler
from wayfinder_paths.core.perps.state import StateStore


def _import_dotted(spec: str):
    if ":" in spec:
        module, attr = spec.split(":", 1)
    else:
        module, _, attr = spec.rpartition(".")
    return getattr(importlib.import_module(module), attr)


def _warn_hash_mismatch(ref: BacktestRef) -> list[str]:
    out: list[str] = []
    if ref.code.signal.module:
        try:
            cur = hash_module_source(ref.code.signal.module)
            if cur != ref.code.signal.source_sha256:
                out.append(
                    f"signal source_sha256 drift: {cur[:12]} vs ref {ref.code.signal.source_sha256[:12]}"
                )
        except ImportError as e:
            out.append(f"signal module not importable: {e}")
    if ref.code.decide is not None and ref.code.decide.module:
        try:
            cur = hash_module_source(ref.code.decide.module)
            if cur != ref.code.decide.source_sha256:
                out.append(
                    f"decide source_sha256 drift: {cur[:12]} vs ref {ref.code.decide.source_sha256[:12]}"
                )
        except ImportError as e:
            out.append(f"decide module not importable: {e}")
    return out


async def _fetch_window(symbols: list[str], start: str, end: str, interval: str):
    prices = await fetch_prices(symbols, start, end, interval)
    funding = None
    try:
        funding = await fetch_funding_rates(symbols, start, end)
        prices, funding = await align_dataframes(prices, funding, method="ffill")
    except (ValueError, KeyError):
        pass
    return prices, funding


def _diff_axes(
    intents: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    bars: pd.DatetimeIndex,
) -> dict[str, Any]:
    """Replay-intents vs live HL fills (the existing four-axis diff)."""
    by_bar_intents: dict[tuple, list[dict[str, Any]]] = {}
    for it in intents:
        key = (pd.Timestamp(it["placed_at_t"]).floor("h"), it["symbol"], it["side"])
        by_bar_intents.setdefault(key, []).append(it)

    by_bar_fills: dict[tuple, list[dict[str, Any]]] = {}
    for f in fills:
        try:
            t = pd.Timestamp(int(f["time"]) / 1000, unit="s", tz="UTC").floor("h")
        except (TypeError, ValueError, KeyError):
            continue
        key = (t, str(f.get("coin", "?")), "buy" if f.get("side") == "B" else "sell")
        by_bar_fills.setdefault(key, []).append(f)

    decision_misses, unexpected_fills, size_drifts, price_drifts, fill_completion = (
        [],
        [],
        [],
        [],
        [],
    )
    seen_intent = set(by_bar_intents.keys())
    seen_fills = set(by_bar_fills.keys())
    for key in seen_intent & seen_fills:
        ints = by_bar_intents[key]
        fls = by_bar_fills[key]
        intended = sum(float(i["size"]) for i in ints)
        filled = sum(abs(float(f.get("sz", 0.0))) for f in fls)
        if abs(filled - intended) / max(intended, 1e-9) > 0.01:
            size_drifts.append(
                {
                    "bar": str(key[0]),
                    "symbol": key[1],
                    "side": key[2],
                    "intended": intended,
                    "filled": filled,
                    "drift_pct": (filled - intended) / max(intended, 1e-9),
                }
            )
        for f in fls:
            try:
                price_drifts.append(
                    {
                        "bar": str(key[0]),
                        "symbol": key[1],
                        "side": key[2],
                        "fill_price": float(f.get("px", 0.0)),
                    }
                )
            except (TypeError, ValueError):
                continue
        fill_completion.append(
            {
                "bar": str(key[0]),
                "symbol": key[1],
                "side": key[2],
                "intents": len(ints),
                "fills": len(fls),
                "intended_size": intended,
                "filled_size": filled,
            }
        )
    for key in seen_intent - seen_fills:
        decision_misses.append(
            {
                "bar": str(key[0]),
                "symbol": key[1],
                "side": key[2],
                "intents": len(by_bar_intents[key]),
            }
        )
    for key in seen_fills - seen_intent:
        unexpected_fills.append(
            {
                "bar": str(key[0]),
                "symbol": key[1],
                "side": key[2],
                "fills": len(by_bar_fills[key]),
            }
        )
    bars_with_intents = {k[0] for k in seen_intent}
    expected_bars = set(bars)
    return {
        "trigger_timing": {
            "expected_bars": len(expected_bars),
            "bars_with_intents": len(bars_with_intents),
            "missing_trigger_bars": [
                str(b) for b in sorted(expected_bars - bars_with_intents)[:50]
            ],
            "unexpected_trigger_bars": [
                str(b) for b in sorted(bars_with_intents - expected_bars)[:50]
            ],
        },
        "decision_parity": {
            "missed_intents": decision_misses,
            "unexpected_fills": unexpected_fills,
        },
        "size_drift": size_drifts,
        "fill_price_drift": price_drifts,
        "fill_completion": fill_completion,
    }


def _strict_intent_diff(
    replay: list[dict[str, Any]],
    live: list[dict[str, Any]],
) -> dict[str, Any]:
    """Replay-intents (this run) vs recorded live-intents (from snapshots)."""

    def key(d: dict[str, Any]) -> tuple:
        bar = d.get("bar_t") or d.get("placed_at_t")
        return (
            pd.Timestamp(bar).floor("h") if bar else None,
            d.get("venue", "?"),
            d.get("symbol") or d.get("sym", "?"),
            d.get("side", "?"),
        )

    by_replay: dict[tuple, list[dict[str, Any]]] = {}
    for it in replay:
        by_replay.setdefault(key(it), []).append(it)
    by_live: dict[tuple, list[dict[str, Any]]] = {}
    for it in live:
        by_live.setdefault(key(it), []).append(it)

    matched, replay_only, live_only, size_drifts = [], [], [], []
    for k in by_replay.keys() & by_live.keys():
        r_sz = sum(float(x.get("size", 0.0)) for x in by_replay[k])
        l_sz = sum(float(x.get("size", 0.0)) for x in by_live[k])
        rel = abs(r_sz - l_sz) / max(abs(l_sz), 1e-9)
        bucket = {
            "bar": str(k[0]),
            "venue": k[1],
            "symbol": k[2],
            "side": k[3],
            "replay_size": r_sz,
            "live_size": l_sz,
            "rel_drift": rel,
        }
        matched.append(bucket)
        if rel > 0.01:
            size_drifts.append(bucket)
    for k in by_replay.keys() - by_live.keys():
        replay_only.append(
            {
                "bar": str(k[0]),
                "venue": k[1],
                "symbol": k[2],
                "side": k[3],
                "intents": len(by_replay[k]),
            }
        )
    for k in by_live.keys() - by_replay.keys():
        live_only.append(
            {
                "bar": str(k[0]),
                "venue": k[1],
                "symbol": k[2],
                "side": k[3],
                "intents": len(by_live[k]),
            }
        )
    return {
        "matched_buckets": matched,
        "replay_only": replay_only,
        "live_only": live_only,
        "size_drifts": size_drifts,
    }


async def _pull_live_fills(
    strategy_name: str, start: str, end: str
) -> list[dict[str, Any]]:
    from wayfinder_paths.adapters.hyperliquid_adapter.adapter import HyperliquidAdapter
    from wayfinder_paths.mcp.scripting import get_adapter

    adapter = await get_adapter(HyperliquidAdapter, strategy_name)
    ok, raw = await adapter.get_user_fills(adapter.wallet_address)
    if not ok or not isinstance(raw, list):
        return []
    start_ms = pd.Timestamp(start).timestamp() * 1000
    end_ms = pd.Timestamp(end).timestamp() * 1000
    return [f for f in raw if start_ms <= float(f.get("time", 0)) <= end_ms]


async def reconcile_strategy(
    *,
    strategy_dir: str | Path,
    strategy_name: str,
    start: str | None = None,
    end: str | None = None,
    no_fills: bool = False,
    write_report: bool = True,
) -> dict[str, Any]:
    """Replay decide() over the recorded live snapshots, diff against captured
    live intents + (optionally) HL fills, and return a structured report.

    If `start`/`end` are omitted, defaults to a 30-day window ending now.
    """
    sd = Path(strategy_dir)
    ref = load_ref(sd)

    if end is None:
        end = datetime.now(UTC).strftime("%Y-%m-%d")
    if start is None:
        start = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%d")

    warnings = _warn_hash_mismatch(ref)

    signal_fn = (
        _import_dotted(f"{ref.code.signal.module}:{ref.code.signal.entrypoint}")
        if ref.code.signal.module and ref.code.signal.entrypoint
        else None
    )
    decide_fn = (
        _import_dotted(f"{ref.code.decide.module}:{ref.code.decide.entrypoint}")
        if ref.code.decide and ref.code.decide.module
        else None
    )
    if signal_fn is None:
        return {
            "strategy": strategy_name,
            "ok": False,
            "error": "ref.code.signal not importable — cannot replay",
            "warnings": warnings,
        }
    if decide_fn is None:
        from wayfinder_paths.core.backtesting.perps import default_decide

        decide_fn = default_decide

    prices, funding = await _fetch_window(
        ref.data.symbols, start, end, ref.data.interval
    )
    cur_fp = (
        fingerprint_frames(prices)
        if funding is None
        else fingerprint_frames(prices, funding)
    )
    if ref.data.fingerprint and cur_fp != ref.data.fingerprint:
        warnings.append(
            f"data fingerprint drift: {cur_fp[:12]} vs ref {ref.data.fingerprint[:12]}"
        )

    venues_keys = ["perp"] + [f"hip3:{d}" for d in ref.venues.hip3]
    handlers = {
        k: ReconcileHandler(
            venue=k,
            prices=prices,
            funding=funding,
            strategy_name=strategy_name,
            slippage_bps=ref.execution_assumptions.slippage_bps,
            fee_bps=ref.execution_assumptions.fee_bps,
            min_order_usd=ref.execution_assumptions.min_order_usd,
        )
        for k in venues_keys
    }
    perp = handlers["perp"]
    hip3 = {k.removeprefix("hip3:"): h for k, h in handlers.items() if k != "perp"}

    state = StateStore(strategy_name, "reconcile")
    raw_sig = signal_fn(prices, funding, dict(ref.params))
    signal_frame = normalize_signal(
        raw_sig, fallback_index=prices.index, fallback_columns=ref.data.symbols
    )

    replay_intents: list[dict[str, Any]] = []
    recorded_live: list[dict[str, Any]] = []
    for i, t in enumerate(prices.index):
        for h in handlers.values():
            h.set_bar(i)
            h.load_snapshot_at(t.to_pydatetime())
            for live in h.recorded_live_intents:
                rec = dict(live)
                rec["bar_t"] = str(t)
                rec.setdefault("venue", h.venue)
                recorded_live.append(rec)
        ctx = TriggerContext(
            perp=perp,
            hip3=hip3,
            params=dict(ref.params),
            state=state,
            signal=signal_frame,
            t=t.to_pydatetime(),
        )
        await decide_fn(ctx)
        for h in handlers.values():
            for intent in h.drain_intents():
                rec = dict(intent)
                rec["placed_at_t"] = str(intent["placed_at_t"])
                rec["venue"] = h.venue
                replay_intents.append(rec)

    fills: list[dict[str, Any]] = []
    if not no_fills:
        try:
            fills = await _pull_live_fills(strategy_name, start, end)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"live fills fetch failed: {e}")

    diff = _diff_axes(replay_intents, fills, prices.index)
    strict_diff = _strict_intent_diff(replay_intents, recorded_live)

    payload = {
        "strategy": strategy_name,
        "ref_hash": ref.produced.ref_hash,
        "window": {"start": start, "end": end, "bars": len(prices.index)},
        "warnings": warnings,
        "intents": replay_intents,
        "recorded_live_intents": recorded_live,
        "live_fills": fills,
        "diff": diff,
        "strict_intent_diff": strict_diff,
    }

    if write_report:
        out_dir = sd / "reconciliation"
        out_dir.mkdir(parents=True, exist_ok=True)
        run_ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"{run_ts}.json"
        out_path.write_text(json.dumps(payload, indent=2, default=str))
        payload["report_path"] = str(out_path)

    return payload
