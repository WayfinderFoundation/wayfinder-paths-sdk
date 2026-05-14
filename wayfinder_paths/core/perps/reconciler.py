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
    return getattr(importlib.reload(importlib.import_module(module)), attr)


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


# =====================================================================
# 6-axis drift taxonomy — refactored from the older _diff_axes /
# _strict_intent_diff blocks. Each axis answers a specific debug
# question about why real ≠ counterfactual.
# =====================================================================


def _hour(t: Any) -> pd.Timestamp:
    ts = pd.Timestamp(t)
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    return ts.floor("h")


def _cf_expected_intents(
    signal_frame: Any,
    prices: pd.DataFrame,
    deploy_ts: pd.Timestamp,
    deploy_nav: float,
    ref: BacktestRef,
) -> list[dict[str, Any]]:
    """Walk the counterfactual signal frame and emit per-bar 'should have'
    intents. These are what the strategy WOULD trade under perfect hourly
    execution — the right reference for position drift."""
    syms = list(signal_frame.targets.columns)
    win = signal_frame.targets.index[signal_frame.targets.index >= deploy_ts]
    if len(win) == 0:
        return []
    targets = signal_frame.targets.loc[win]
    win_prices = prices.loc[win]
    min_order_usd = ref.execution_assumptions.min_order_usd
    cur_size = dict.fromkeys(syms, 0.0)
    intents: list[dict[str, Any]] = []
    for i, ts in enumerate(win):
        for s in syms:
            mid = float(win_prices.iloc[i][s])
            if mid <= 0:
                continue
            target_w = float(targets.iloc[i][s])
            target_size = (target_w * deploy_nav) / mid
            delta = target_size - cur_size[s]
            if abs(delta) * mid < min_order_usd:
                continue
            intents.append(
                {
                    "bar_t": str(ts),
                    "placed_at_t": str(ts),
                    "symbol": s,
                    "side": "buy" if delta > 0 else "sell",
                    "size": abs(delta),
                    "notional": abs(delta) * mid,
                }
            )
            cur_size[s] = target_size
    return intents


def _drift_position(
    expected_intents: list[dict[str, Any]],
    fills: list[dict[str, Any]],
) -> dict[str, Any]:
    """Axis 1 — POSITION DIVERGENCE (bidirectional).

    sim_only: counterfactual said trade, live placed nothing (runner died,
              decide errored, missed update window).
    live_only: live traded, counterfactual had no intent (off-strategy
               ad-hoc, manual orders, or another strategy on same wallet).
    """
    by_intent: dict[tuple, list[dict[str, Any]]] = {}
    for it in expected_intents:
        key = (_hour(it["placed_at_t"]), it["symbol"], it["side"])
        by_intent.setdefault(key, []).append(it)
    by_fill: dict[tuple, list[dict[str, Any]]] = {}
    for f in fills:
        try:
            t = pd.Timestamp(int(f["time"]) / 1000, unit="s", tz="UTC").floor("h")
        except (TypeError, ValueError, KeyError):
            continue
        side = "buy" if f.get("side") == "B" else "sell"
        by_fill.setdefault((t, f.get("coin"), side), []).append(f)

    sim_only = [
        {
            "bar": str(k[0]),
            "symbol": k[1],
            "side": k[2],
            "intent_size": sum(float(x.get("size", 0)) for x in by_intent[k]),
            "intent_notional": sum(float(x.get("notional", 0)) for x in by_intent[k]),
            "intents": len(by_intent[k]),
        }
        for k in by_intent.keys() - by_fill.keys()
    ]
    live_only = [
        {
            "bar": str(k[0]),
            "symbol": k[1],
            "side": k[2],
            "fill_count": len(by_fill[k]),
            "fill_notional": sum(
                float(x.get("sz", 0)) * float(x.get("px", 0)) for x in by_fill[k]
            ),
        }
        for k in by_fill.keys() - by_intent.keys()
    ]
    return {
        "sim_only_intents": sim_only,
        "live_only_fills": live_only,
        "summary": {
            "sim_only_count": len(sim_only),
            "live_only_count": len(live_only),
            "sim_only_notional": sum(s["intent_notional"] for s in sim_only),
            "live_only_notional": sum(s["fill_notional"] for s in live_only),
        },
    }


def _drift_fill_rate(
    recorded_live_intents: list[dict[str, Any]],
    fills: list[dict[str, Any]],
) -> dict[str, Any]:
    """Axis 2 — FILL RATE DIVERGENCE.

    Match each recorded live intent (size requested) to its corresponding
    fills by (bar, symbol, side). Anything intended but not (fully) filled
    is fill-rate drift — orderbook too thin, IOC expired, exchange rejected.
    """
    if not recorded_live_intents:
        return {
            "ok": False,
            "reason": "no recorded_live_intents — recording wrapper inactive or no rebalances",
            "partial_fills": [],
            "no_fills": [],
            "summary": {"partial_count": 0, "no_fill_count": 0},
        }
    by_intent: dict[tuple, float] = {}
    for it in recorded_live_intents:
        key = (
            _hour(it.get("placed_at_t") or it.get("bar_t")),
            it.get("symbol"),
            it.get("side"),
        )
        by_intent[key] = by_intent.get(key, 0.0) + float(it.get("size", 0))

    by_fill: dict[tuple, float] = {}
    for f in fills:
        try:
            t = pd.Timestamp(int(f["time"]) / 1000, unit="s", tz="UTC").floor("h")
        except (TypeError, ValueError, KeyError):
            continue
        side = "buy" if f.get("side") == "B" else "sell"
        key = (t, f.get("coin"), side)
        by_fill[key] = by_fill.get(key, 0.0) + abs(float(f.get("sz", 0)))

    partial, no_fill = [], []
    for key, intended in by_intent.items():
        filled = by_fill.get(key, 0.0)
        if intended <= 0:
            continue
        fill_pct = filled / intended
        entry = {
            "bar": str(key[0]),
            "symbol": key[1],
            "side": key[2],
            "intended_size": intended,
            "filled_size": filled,
            "fill_pct": fill_pct,
        }
        if filled <= 1e-9:
            no_fill.append(entry)
        elif fill_pct < 0.99:
            partial.append(entry)
    return {
        "ok": True,
        "partial_fills": partial,
        "no_fills": no_fill,
        "summary": {
            "partial_count": len(partial),
            "no_fill_count": len(no_fill),
        },
    }


def _drift_signal(
    replay_signal: Any,
    strategy_name: str,
    bars: pd.DatetimeIndex,
) -> dict[str, Any]:
    """Axis 3 — SIGNAL DIVERGENCE.

    For each on-disk snapshot in [bars], compare its recorded signal_row to
    what replay's signal_fn produced at the same bar. Mismatches = the live
    runtime saw different inputs (different prices, different params, etc.).
    """
    weight_drifts = []
    bars_compared = 0
    max_abs_drift = 0.0
    for t in StateStore.list_snapshots(strategy_name):
        ts = _hour(t)
        if not (bars[0] <= ts <= bars[-1]):
            continue
        snap = StateStore.snapshot_at(strategy_name, t)
        live_row = snap.get("signal_row") or {}
        if not live_row:
            continue
        try:
            replay_row = replay_signal.targets.loc[ts]
        except (KeyError, AttributeError):
            continue
        bars_compared += 1
        for sym, live_w in live_row.items():
            if live_w is None:
                continue
            try:
                replay_w = float(replay_row.get(sym, 0.0) or 0.0)
            except (TypeError, ValueError):
                replay_w = 0.0
            diff = float(live_w) - replay_w
            if abs(diff) > max_abs_drift:
                max_abs_drift = abs(diff)
            if abs(diff) > 1e-6:
                weight_drifts.append(
                    {
                        "bar": str(ts),
                        "symbol": sym,
                        "live_weight": float(live_w),
                        "replay_weight": replay_w,
                        "abs_diff": abs(diff),
                    }
                )
    return {
        "weight_drifts": weight_drifts,
        "summary": {
            "bars_compared": bars_compared,
            "bars_with_drift": len({(d["bar"], d["symbol"]) for d in weight_drifts}),
            "max_abs_drift": max_abs_drift,
        },
    }


def _drift_slippage(
    fills: list[dict[str, Any]],
    prices: pd.DataFrame,
    ref: BacktestRef,
) -> dict[str, Any]:
    """Axis 4 — SLIPPAGE DIVERGENCE.

    Per fill: compare fill_price to bar mid (price drift in bps) and
    paid fee to expected fee from ref.execution_assumptions (fee drift).
    """
    expected_fee_bps = ref.execution_assumptions.fee_bps
    expected_slip_bps = ref.execution_assumptions.slippage_bps
    price_drifts, fee_drifts = [], []
    total_excess_fees = 0.0
    drift_bps_samples: list[float] = []
    for f in fills:
        try:
            t = pd.Timestamp(int(f["time"]) / 1000, unit="s", tz="UTC").floor("h")
            sym = f["coin"]
            fill_px = float(f["px"])
            sz = float(f["sz"])
            fee = float(f.get("fee") or 0.0) + float(f.get("builderFee") or 0.0)
        except (TypeError, ValueError, KeyError):
            continue
        if sym not in prices.columns or t not in prices.index:
            continue
        bar_mid = float(prices.at[t, sym])
        if bar_mid <= 0:
            continue
        side = "buy" if f.get("side") == "B" else "sell"
        # Drift bps signed: positive = paid worse than mid for that side
        sign = 1 if side == "buy" else -1
        drift_bps = sign * (fill_px - bar_mid) / bar_mid * 10_000
        drift_bps_samples.append(drift_bps)
        if abs(drift_bps) > expected_slip_bps:
            price_drifts.append(
                {
                    "bar": str(t),
                    "symbol": sym,
                    "side": side,
                    "fill_price": fill_px,
                    "bar_mid": bar_mid,
                    "drift_bps": drift_bps,
                    "expected_bps": expected_slip_bps,
                }
            )
        notional = sz * fill_px
        expected_fee = notional * expected_fee_bps / 10_000
        fee_excess = fee - expected_fee
        total_excess_fees += fee_excess
        if expected_fee > 0 and (fee / expected_fee) > 1.5:
            fee_drifts.append(
                {
                    "bar": str(t),
                    "symbol": sym,
                    "fee_paid": fee,
                    "fee_expected": expected_fee,
                    "ratio": fee / expected_fee,
                }
            )
    avg_drift = (
        sum(drift_bps_samples) / len(drift_bps_samples) if drift_bps_samples else 0.0
    )
    return {
        "price_drifts": price_drifts,
        "fee_drifts": fee_drifts,
        "summary": {
            "fills_examined": len(drift_bps_samples),
            "avg_price_drift_bps": avg_drift,
            "total_excess_fees": total_excess_fees,
            "expected_slippage_bps": expected_slip_bps,
            "expected_fee_bps": expected_fee_bps,
        },
    }


async def _pull_funding_payments(
    strategy_name: str, start: str, end: str
) -> list[dict[str, Any]]:
    """HL userFunding endpoint — funding paid/received per bar.

    Uses HL info API directly since the adapter doesn't expose a method
    for it. Returns entries shaped like:
        {coin, time, usdc (signed funding), szi, fundingRate, ...}
    """
    try:
        import httpx  # noqa: PLC0415

        from wayfinder_paths.adapters.hyperliquid_adapter.adapter import (
            HyperliquidAdapter,
        )
        from wayfinder_paths.mcp.scripting import get_adapter

        adapter = await get_adapter(HyperliquidAdapter, strategy_name)
        wallet = adapter.wallet_address
        start_ms = int(pd.Timestamp(start).timestamp() * 1000)
        end_ms = int(pd.Timestamp(end).timestamp() * 1000)
        body = {
            "type": "userFunding",
            "user": wallet,
            "startTime": start_ms,
            "endTime": end_ms,
        }
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post("https://api.hyperliquid.xyz/info", json=body)
            r.raise_for_status()
            raw = r.json()
        # The endpoint returns a list of {time, hash, delta: {coin, usdc, ...}}
        # Flatten to {coin, time, usdc, ...}
        out = []
        for entry in raw if isinstance(raw, list) else []:
            t = entry.get("time")
            d = entry.get("delta", {})
            if d.get("type") == "funding" or "fundingRate" in d:
                out.append(
                    {
                        "coin": d.get("coin"),
                        "time": t,
                        "usdc": d.get("usdc"),
                        "szi": d.get("szi"),
                        "fundingRate": d.get("fundingRate"),
                    }
                )
        return out
    except Exception:  # noqa: BLE001
        return []


def _drift_funding(
    funding_payments: list[dict[str, Any]],
    funding_rates: pd.DataFrame | None,
    counterfactual_positions: dict[str, Any] | None,
) -> dict[str, Any]:
    """Axis 5 — FUNDING DIVERGENCE.

    Sum live funding paid/received per symbol; compare to expected funding
    derived from window-mean funding rate × counterfactual position size × hours.
    Useful for perp strategies on volatile-funding assets (HIP-3, LSDs).
    """
    accruals = []
    total_real = 0.0
    by_sym: dict[str, float] = {}
    for r in funding_payments:
        sym = r.get("coin")
        # HL returns "usdc" as the funding amount (signed: + means received)
        amt = float(r.get("usdc") or 0.0)
        by_sym[sym] = by_sym.get(sym, 0.0) + amt
        total_real += amt

    total_expected = 0.0
    if funding_rates is not None and counterfactual_positions:
        for sym, p in counterfactual_positions.items():
            if sym not in funding_rates.columns:
                continue
            sz = p.get("size", 0)
            mean_rate = float(funding_rates[sym].mean())  # per hour, signed
            hours = len(funding_rates)
            # If long pays positive funding (longs pay shorts when funding > 0),
            # accrual = -sz × mean_rate × hours × notional / size  ≈  -sz × mean_rate × hours × mark
            mark = p.get("mark", 0)
            expected = -sz * mean_rate * hours * mark
            total_expected += expected
            actual = by_sym.get(sym, 0.0)
            accruals.append(
                {
                    "symbol": sym,
                    "live_funding_usd": actual,
                    "expected_funding_usd": expected,
                    "drift": actual - expected,
                }
            )
    return {
        "accruals": accruals,
        "summary": {
            "total_real_funding": total_real,
            "total_expected_funding": total_expected,
            "drift": total_real - total_expected,
        },
    }


def _drift_config(
    ref: BacktestRef,
    strategy_name: str,
    bars: pd.DatetimeIndex,
) -> dict[str, Any]:
    """Axis 6 — CONFIGURATION DRIFT.

    Hashes ref.params and the latest snapshot's params_hash. If they
    diverge, someone changed strategy params after the ref was minted.
    Also detects mid-window param transitions.
    """
    import hashlib  # noqa: PLC0415

    ref_hash = hashlib.sha256(
        json.dumps(dict(ref.params), sort_keys=True, default=str).encode()
    ).hexdigest()[:16]

    seen_hashes: dict[str, list[str]] = {}
    for t in StateStore.list_snapshots(strategy_name):
        ts = _hour(t)
        if not (bars[0] <= ts <= bars[-1]):
            continue
        snap = StateStore.snapshot_at(strategy_name, t)
        h = snap.get("params_hash")
        if not h:
            continue
        seen_hashes.setdefault(h, []).append(str(ts))

    transitions = []
    if len(seen_hashes) > 1:
        items = sorted(
            ((h, ts_list[0]) for h, ts_list in seen_hashes.items()),
            key=lambda x: x[1],
        )
        for prev, curr in zip(items, items[1:], strict=False):
            transitions.append({"from": prev[0], "to": curr[0], "at": curr[1]})

    drifted = bool(seen_hashes) and ref_hash not in seen_hashes
    return {
        "ref_params_hash": ref_hash,
        "live_params_hashes": list(seen_hashes.keys()),
        "drifted_from_ref": drifted,
        "transitions": transitions,
    }


def _compute_drift(
    *,
    cf_expected_intents: list[dict[str, Any]],
    recorded_live: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    funding_payments: list[dict[str, Any]],
    funding_rates: pd.DataFrame | None,
    replay_signal: Any,
    prices: pd.DataFrame,
    ref: BacktestRef,
    strategy_name: str,
    bars: pd.DatetimeIndex,
    counterfactual_positions: dict[str, Any] | None,
) -> dict[str, Any]:
    """Top-level drift block — 6 axes.

    Each axis answers a specific question about why real != counterfactual.
    """
    return {
        "position": _drift_position(cf_expected_intents, fills),
        "fill_rate": _drift_fill_rate(recorded_live, fills),
        "signal": _drift_signal(replay_signal, strategy_name, bars),
        "slippage": _drift_slippage(fills, prices, ref),
        "funding": _drift_funding(
            funding_payments, funding_rates, counterfactual_positions
        ),
        "config": _drift_config(ref, strategy_name, bars),
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
    # Bare date strings parse as midnight; bump so the final day is inclusive.
    end_ts = pd.Timestamp(end)
    if end_ts.normalize() == end_ts:
        end_ts = end_ts + pd.Timedelta(days=1)
    end_ms = end_ts.timestamp() * 1000
    return [f for f in raw if start_ms <= float(f.get("time", 0)) <= end_ms]


async def _pull_current_state(strategy_name: str) -> dict[str, Any] | None:
    """Fetch HL clearinghouseState for the strategy's wallet (for current
    real positions / unrealized PnL). Returns None on failure."""
    try:
        from wayfinder_paths.adapters.hyperliquid_adapter.adapter import (
            HyperliquidAdapter,
        )
        from wayfinder_paths.mcp.scripting import get_adapter

        adapter = await get_adapter(HyperliquidAdapter, strategy_name)
        ok, state = await adapter.get_user_state(adapter.wallet_address)
        return state if ok else None
    except Exception:  # noqa: BLE001 — counterfactual is best-effort
        return None


def _deploy_anchor(
    strategy_name: str, prices_index: pd.DatetimeIndex
) -> tuple[pd.Timestamp, float] | None:
    """Find deploy bar + starting NAV from earliest snapshot with a nav field."""
    snaps = StateStore.list_snapshots(strategy_name)
    for t in snaps:
        snap = StateStore.snapshot_at(strategy_name, t)
        nav = snap.get("nav")
        if nav is None:
            continue
        ts = pd.Timestamp(t).floor("h")
        if ts.tz is None:
            ts = ts.tz_localize("UTC")
        if prices_index.tz is None:
            ts = ts.tz_localize(None)
        if ts in prices_index:
            return ts, float(nav)
        # snap to nearest bar at or after deploy
        try:
            pos = prices_index.get_indexer([ts], method="bfill")[0]
            if pos >= 0:
                return prices_index[pos], float(nav)
        except Exception:
            pass
    return None


async def _counterfactual_signal_frame(
    signal_fn: Any, ref: BacktestRef, deploy_ts: pd.Timestamp
) -> tuple[Any, pd.DataFrame]:
    """Re-run signal_fn against a window wide enough for its lookback warmup
    (deploy_ts − lookback_bars). The window-only prices the main reconcile
    path fetches are too short for stateful signals."""
    lookback = int(ref.params.get("lookback_bars") or 200)
    now = datetime.now(UTC)
    warm_start = (deploy_ts - pd.Timedelta(hours=lookback + 24)).strftime("%Y-%m-%d")
    warm_end = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    warm_prices, warm_funding = await _fetch_window(
        ref.data.symbols, warm_start, warm_end, ref.data.interval
    )
    raw = signal_fn(warm_prices, warm_funding, dict(ref.params))
    sf = normalize_signal(
        raw, fallback_index=warm_prices.index, fallback_columns=ref.data.symbols
    )
    return sf, warm_prices


def _counterfactual_pnl(
    signal_frame: Any,
    prices: pd.DataFrame,
    ref: BacktestRef,
    deploy_ts: pd.Timestamp,
    deploy_nav: float,
    fills: list[dict[str, Any]],
    current_state: dict[str, Any] | None,
) -> dict[str, Any]:
    """What PnL the strategy would have made under perfect hourly execution.

    Walks each bar from deploy_ts forward. Target sizes derived from
    signal weights (already leverage-scaled by convention) × NAV / mid.
    Realizes PnL on closes/flips, fees from `ref.execution_assumptions`,
    marks unrealized to current live mid (or last bar mid if absent).
    """
    syms = list(signal_frame.targets.columns)
    win_idx = signal_frame.targets.index[signal_frame.targets.index >= deploy_ts]
    if len(win_idx) == 0:
        return {"ok": False, "reason": "no bars after deploy_ts"}

    targets = signal_frame.targets.loc[win_idx]
    bar_prices = prices.loc[win_idx]
    fee_bps = ref.execution_assumptions.fee_bps
    min_order_usd = ref.execution_assumptions.min_order_usd

    cur_size = dict.fromkeys(syms, 0.0)
    entry_px = dict.fromkeys(syms)
    realized = 0.0
    fees = 0.0
    volume = 0.0
    rebalances = 0

    for i, _ts in enumerate(win_idx):
        for s in syms:
            mid = float(bar_prices.iloc[i][s])
            if mid <= 0:
                continue
            target_w = float(targets.iloc[i][s])
            target_size = (target_w * deploy_nav) / mid
            cur = cur_size[s]
            delta = target_size - cur
            if abs(delta) * mid < min_order_usd:
                continue
            if cur != 0 and (cur > 0) != (delta > 0):
                close_qty = min(abs(delta), abs(cur)) * (1 if cur > 0 else -1)
                if entry_px[s] is not None:
                    realized += close_qty * (mid - entry_px[s])
            new_size = cur + delta
            if new_size != 0 and ((cur >= 0 and delta > 0) or (cur <= 0 and delta < 0)):
                if entry_px[s] is None or cur == 0:
                    entry_px[s] = mid
                else:
                    entry_px[s] = (entry_px[s] * cur + mid * delta) / new_size
            elif new_size == 0:
                entry_px[s] = None
            elif (cur > 0) != (new_size > 0):
                entry_px[s] = mid
            trade_notional = abs(delta) * mid
            fees += trade_notional * fee_bps / 10_000
            volume += trade_notional
            rebalances += 1
            cur_size[s] = target_size

    # Live mids for mark-to-now (fall back to last bar mid).
    live_mids: dict[str, float] = {}
    if current_state:
        for p in current_state.get("assetPositions", []):
            pos = p.get("position", {})
            sz = abs(float(pos.get("szi", 0) or 0))
            ntl = float(pos.get("positionValue", 0) or 0)
            if sz > 0:
                live_mids[pos["coin"]] = ntl / sz
    last_bar_mids = bar_prices.iloc[-1]

    cf_unrealized = 0.0
    cf_positions: dict[str, dict[str, float]] = {}
    for s, sz in cur_size.items():
        if sz == 0:
            continue
        m = live_mids.get(s, float(last_bar_mids[s]))
        upnl = sz * (m - entry_px[s]) if entry_px[s] is not None else 0.0
        cf_unrealized += upnl
        cf_positions[s] = {
            "size": sz,
            "entry_px": entry_px[s] or 0.0,
            "mark": m,
            "notional": abs(sz) * m,
            "upnl": upnl,
        }

    cf_net = realized + cf_unrealized - fees

    # Real PnL: closedPnl from fills + unrealized from current state - fees
    real_realized = sum(float(f.get("closedPnl") or 0.0) for f in fills)
    real_fees = sum(float(f.get("fee") or 0.0) for f in fills)
    real_volume = sum(
        float(f.get("sz") or 0.0) * float(f.get("px") or 0.0) for f in fills
    )
    real_unrealized = 0.0
    real_positions: dict[str, dict[str, float]] = {}
    real_nav_now: float | None = None
    if current_state:
        for p in current_state.get("assetPositions", []):
            pos = p.get("position", {})
            sz = float(pos.get("szi", 0) or 0)
            ntl = float(pos.get("positionValue", 0) or 0)
            upnl = float(pos.get("unrealizedPnl", 0) or 0)
            real_unrealized += upnl
            real_positions[pos["coin"]] = {
                "size": sz,
                "entry_px": float(pos.get("entryPx", 0) or 0),
                "mark": ntl / abs(sz) if sz != 0 else 0.0,
                "notional": ntl,
                "upnl": upnl,
            }
        ms = current_state.get("marginSummary", {})
        if ms.get("accountValue") is not None:
            real_nav_now = float(ms["accountValue"])
    real_net = real_realized + real_unrealized - real_fees

    return {
        "ok": True,
        "deploy_ts": str(deploy_ts),
        "deploy_nav": deploy_nav,
        "bars": len(win_idx),
        "counterfactual": {
            "rebalances": rebalances,
            "volume": volume,
            "realized_pnl": realized,
            "unrealized_pnl": cf_unrealized,
            "fees": fees,
            "net_pnl": cf_net,
            "nav_now": deploy_nav + cf_net,
            "positions": cf_positions,
        },
        "real": {
            "rebalances": len(fills),
            "volume": real_volume,
            "realized_pnl": real_realized,
            "unrealized_pnl": real_unrealized,
            "fees": real_fees,
            "net_pnl": real_net,
            "nav_now": real_nav_now,
            "positions": real_positions,
        },
        "operational_gap": {
            "real_minus_counterfactual_pnl": real_net - cf_net,
            "missed_rebalances": rebalances - len(fills),
            "nav_gap": (real_nav_now - (deploy_nav + cf_net))
            if real_nav_now is not None
            else None,
        },
    }


def _compute_verdict(
    drift: dict[str, Any], warnings: list[str]
) -> tuple[str, list[str]]:
    """PASS / WARN / FAIL aggregated across drift axes. FAIL = parity-broken
    (code, data, config, position, signal, or no-fill). WARN = operational
    noise (partial fills, slippage outliers, funding drift)."""
    reasons: list[str] = []

    drift_warning_markers = (
        "decide source_sha256 drift",
        "ref source_sha256 drift",
        "data fingerprint drift",
        "signal_fn",
        "decide_fn",
        "not importable",
    )
    for w in warnings:
        if any(marker in w for marker in drift_warning_markers):
            reasons.append(f"code/data drift: {w}")

    config = drift.get("config") or {}
    if config.get("drifted_from_ref"):
        reasons.append(
            f"live params hash {config.get('live_params_hashes')} differ from "
            f"ref {config.get('ref_params_hash')}"
        )

    position = drift.get("position") or {}
    pos_summary = position.get("summary") or {}
    sim_only = int(pos_summary.get("sim_only_count") or 0)
    live_only = int(pos_summary.get("live_only_count") or 0)
    if sim_only > 0:
        reasons.append(f"position: {sim_only} sim-only intent(s) without live fill")
    if live_only > 0:
        reasons.append(f"position: {live_only} live-only fill(s) without sim intent")

    fill_rate = drift.get("fill_rate") or {}
    fr_summary = fill_rate.get("summary") or {}
    no_fill = int(fr_summary.get("no_fill_count") or 0)
    if no_fill > 0:
        reasons.append(f"fill_rate: {no_fill} intended order(s) had zero fill")

    signal = drift.get("signal") or {}
    sig_summary = signal.get("summary") or {}
    bars_drift = int(sig_summary.get("bars_with_drift") or 0)
    max_drift = float(sig_summary.get("max_abs_drift") or 0.0)
    if bars_drift > 0 or max_drift > 1e-6:
        reasons.append(
            f"signal: {bars_drift} bars with weight drift, max |Δw|={max_drift:.4f}"
        )

    if reasons:
        return "FAIL", reasons

    soft_reasons: list[str] = []
    partial = int((fill_rate.get("summary") or {}).get("partial_count") or 0)
    if partial > 0:
        soft_reasons.append(f"{partial} partial fill(s)")

    slip = drift.get("slippage") or {}
    slip_outliers = slip.get("price_drifts") or []
    outliers_excess = [
        d
        for d in slip_outliers
        if abs(float(d.get("drift_bps") or 0.0))
        > 2.0 * float(d.get("expected_bps") or 0.0)
    ]
    if outliers_excess:
        soft_reasons.append(f"{len(outliers_excess)} slippage outlier(s) >2× expected")

    funding = drift.get("funding") or {}
    f_drift = abs(float((funding.get("summary") or {}).get("drift") or 0.0))
    total_real = abs(
        float((funding.get("summary") or {}).get("total_real_funding") or 0.0)
    )
    # Skip when total_real is small — likely an empty counterfactual baseline.
    if f_drift > 0.50 and total_real > 0.50:
        soft_reasons.append(f"funding drift ${f_drift:.2f}")

    if soft_reasons:
        return "WARN", soft_reasons
    return "PASS", []


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
        snap_nav = 0.0
        for h in handlers.values():
            h.set_bar(i)
            snap = h.load_snapshot_at(t.to_pydatetime())
            if h.venue == "perp":
                snap_nav = float(snap.get("nav") or 0.0)
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
            nav=snap_nav,
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

    # Counterfactual PnL — what the strategy WOULD have made under perfect
    # hourly execution from deploy → now. Surfaces operational drift cost.
    counterfactual: dict[str, Any] = {"ok": False, "reason": "no deploy snapshot"}
    anchor = _deploy_anchor(strategy_name, prices.index)
    cf_signal_frame: Any = signal_frame
    cf_prices: pd.DataFrame = prices
    if anchor is not None:
        deploy_ts, deploy_nav = anchor
        try:
            current_state = await _pull_current_state(strategy_name)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"current state fetch failed: {e}")
            current_state = None
        # Refetch a wider window so the signal fn can warm up its lookback.
        try:
            cf_signal_frame, cf_prices = await _counterfactual_signal_frame(
                signal_fn, ref, deploy_ts
            )
            counterfactual = _counterfactual_pnl(
                cf_signal_frame,
                cf_prices,
                ref,
                deploy_ts,
                deploy_nav,
                fills,
                current_state,
            )
        except Exception as e:  # noqa: BLE001
            counterfactual = {"ok": False, "reason": f"warm fetch failed: {e}"}
            warnings.append(f"counterfactual warm fetch failed: {e}")

    # Pull live funding payments for axis 5
    funding_payments: list[dict[str, Any]] = []
    if not no_fills:
        try:
            funding_payments = await _pull_funding_payments(strategy_name, start, end)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"live funding fetch failed: {e}")

    cf_positions = (
        counterfactual.get("counterfactual", {}).get("positions")
        if isinstance(counterfactual, dict) and counterfactual.get("ok")
        else None
    )
    # Derive cf "should-have" intents from the warmed-up signal frame so
    # position drift compares against meaningful expected trades, not the
    # narrow-window replay (which can't warm up the signal's lookback).
    cf_expected_intents: list[dict[str, Any]] = []
    if (
        anchor is not None
        and isinstance(counterfactual, dict)
        and counterfactual.get("ok")
    ):
        deploy_ts, deploy_nav = anchor
        try:
            cf_expected_intents = _cf_expected_intents(
                cf_signal_frame, cf_prices, deploy_ts, deploy_nav, ref
            )
        except Exception as e:  # noqa: BLE001
            warnings.append(f"cf intents derivation failed: {e}")

    drift = _compute_drift(
        cf_expected_intents=cf_expected_intents,
        recorded_live=recorded_live,
        fills=fills,
        funding_payments=funding_payments,
        funding_rates=funding,
        replay_signal=cf_signal_frame,
        prices=cf_prices,
        ref=ref,
        strategy_name=strategy_name,
        bars=prices.index,
        counterfactual_positions=cf_positions,
    )

    verdict, verdict_reasons = _compute_verdict(drift, warnings)

    payload = {
        "strategy": strategy_name,
        "ref_hash": ref.produced.ref_hash,
        "window": {"start": start, "end": end, "bars": len(prices.index)},
        "verdict": verdict,
        "verdict_reasons": verdict_reasons,
        "warnings": warnings,
        "intents": replay_intents,
        "recorded_live_intents": recorded_live,
        "live_fills": fills,
        "live_funding_payments": funding_payments,
        "drift": drift,
        "counterfactual": counterfactual,
    }

    if write_report:
        out_dir = sd / "reconciliation"
        out_dir.mkdir(parents=True, exist_ok=True)
        run_ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"{run_ts}.json"
        out_path.write_text(json.dumps(payload, indent=2, default=str))
        payload["report_path"] = str(out_path)

    return payload
