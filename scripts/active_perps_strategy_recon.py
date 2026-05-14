"""Replay decide() against historical state snapshots and diff intents vs live fills.

Usage:
    poetry run python scripts/active_perps_strategy_recon.py <strategy_name> \
        --start <iso> --end <iso> [--config config.json]

Output: <strategy_dir>/reconciliation/<run_ts>.json + stdout summary.

Diffs on five axes (D24-style five-axis report):
  - trigger_timing  : did update fire at expected bar?
  - decision_parity : same direction/symbol intent at the same bar?
  - size_drift      : intended size vs filled size after rounding
  - fill_price_drift: realized slippage vs assumed
  - fill_completion : placed but unfilled / partial / unexpected fills

Never halts. Prints critical findings; agents inspect the JSON for detail.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from wayfinder_paths.core.backtesting.data import (  # noqa: E402
    align_dataframes,
    fetch_funding_rates,
    fetch_prices,
)
from wayfinder_paths.core.backtesting.ref import (  # noqa: E402
    BacktestRef,
    fingerprint_frames,
    hash_module_source,
    load_ref,
)
from wayfinder_paths.core.config import load_config  # noqa: E402
from wayfinder_paths.core.perps.context import TriggerContext  # noqa: E402
from wayfinder_paths.core.perps.handlers.reconcile import ReconcileHandler  # noqa: E402
from wayfinder_paths.core.perps.state import StateStore  # noqa: E402


def _import_dotted(spec: str):
    if ":" in spec:
        module, attr = spec.split(":", 1)
    else:
        module, _, attr = spec.rpartition(".")
    return getattr(importlib.import_module(module), attr)


def _strategy_dir(strategy_name: str) -> Path:
    candidates = [
        REPO_ROOT / "wayfinder_paths" / "strategies" / strategy_name,
    ]
    for c in candidates:
        if (c / "backtest_ref.json").exists():
            return c
    raise FileNotFoundError(
        f"No backtest_ref.json found for strategy {strategy_name!r}"
    )


def _warn_hash_mismatch(ref: BacktestRef) -> list[str]:
    out: list[str] = []
    try:
        cur_signal = hash_module_source(ref.code.signal.module)
        if cur_signal != ref.code.signal.source_sha256:
            out.append(
                f"signal source_sha256 drift: {cur_signal[:12]} vs ref {ref.code.signal.source_sha256[:12]}"
            )
    except ImportError as e:
        out.append(f"signal module not importable: {e}")
    if ref.code.decide is not None:
        try:
            cur_decide = hash_module_source(ref.code.decide.module)
            if cur_decide != ref.code.decide.source_sha256:
                out.append(
                    f"decide source_sha256 drift: {cur_decide[:12]} vs ref {ref.code.decide.source_sha256[:12]}"
                )
        except ImportError as e:
            out.append(f"decide module not importable: {e}")
    return out


def _strict_intent_diff(
    replay: list[dict[str, Any]],
    live: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare replay-intents (this run) vs recorded live-intents (from snapshots).

    Buckets by (bar, venue, symbol, side). For each bucket reports size drift
    (sum of replay sizes vs sum of live sizes). Buckets present in only one side
    are reported as `replay_only` / `live_only`.
    """

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
    """Five-axis comparison.

    Buckets intents and fills by (bar_t, symbol, side) and reports drift on each.
    """
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

    decision_misses = []  # intents with no matching fill
    unexpected_fills = []  # fills with no matching intent
    size_drifts = []
    price_drifts = []
    fill_completion = []

    seen_intent_keys = set(by_bar_intents.keys())
    seen_fill_keys = set(by_bar_fills.keys())

    for key in seen_intent_keys & seen_fill_keys:
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
                px = float(f.get("px", 0.0))
                # We don't have an explicit "expected price" without per-bar mid;
                # leave fill_price_drift absolute; agents inspect using the dataset.
                price_drifts.append(
                    {
                        "bar": str(key[0]),
                        "symbol": key[1],
                        "side": key[2],
                        "fill_price": px,
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

    for key in seen_intent_keys - seen_fill_keys:
        decision_misses.append(
            {
                "bar": str(key[0]),
                "symbol": key[1],
                "side": key[2],
                "intents": len(by_bar_intents[key]),
            }
        )
    for key in seen_fill_keys - seen_intent_keys:
        unexpected_fills.append(
            {
                "bar": str(key[0]),
                "symbol": key[1],
                "side": key[2],
                "fills": len(by_bar_fills[key]),
            }
        )

    bars_with_intents = {k[0] for k in seen_intent_keys}
    expected_bars = set(bars)
    missing_trigger_bars = sorted(expected_bars - bars_with_intents)[:50]
    unexpected_trigger_bars = sorted(bars_with_intents - expected_bars)[:50]

    return {
        "trigger_timing": {
            "expected_bars": len(expected_bars),
            "bars_with_intents": len(bars_with_intents),
            "missing_trigger_bars": [str(b) for b in missing_trigger_bars],
            "unexpected_trigger_bars": [str(b) for b in unexpected_trigger_bars],
        },
        "decision_parity": {
            "missed_intents": decision_misses,
            "unexpected_fills": unexpected_fills,
        },
        "size_drift": size_drifts,
        "fill_price_drift": price_drifts,
        "fill_completion": fill_completion,
    }


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("strategy")
    parser.add_argument("--start", required=True, help="ISO datetime")
    parser.add_argument("--end", required=True, help="ISO datetime")
    parser.add_argument("--config", default="config.json")
    parser.add_argument(
        "--no-fills",
        action="store_true",
        help="Skip live fills fetch (offline replay only)",
    )
    args = parser.parse_args(argv)

    load_config(args.config)
    sd = _strategy_dir(args.strategy)
    ref = load_ref(sd)
    print(f"Loaded ref for {args.strategy}: {ref.produced.ref_hash[:12]}")

    warnings = _warn_hash_mismatch(ref)
    for w in warnings:
        print(f"⚠ {w}")

    signal_fn = _import_dotted(f"{ref.code.signal.module}:{ref.code.signal.entrypoint}")
    decide_fn = (
        _import_dotted(f"{ref.code.decide.module}:{ref.code.decide.entrypoint}")
        if ref.code.decide
        else None
    )
    if decide_fn is None:
        from wayfinder_paths.core.backtesting.perps import (
            default_decide,  # noqa: PLC0415
        )

        decide_fn = default_decide

    prices, funding = await _fetch_window(
        ref.data.symbols, args.start, args.end, ref.data.interval
    )
    cur_fp = (
        fingerprint_frames(prices)
        if funding is None
        else fingerprint_frames(prices, funding)
    )
    if cur_fp != ref.data.fingerprint:
        warnings.append(
            f"data fingerprint drift: {cur_fp[:12]} vs ref {ref.data.fingerprint[:12]}"
        )
        print("⚠ data fingerprint drift")

    # Build recon handlers.
    venues_keys = ["perp"] + [f"hip3:{d}" for d in ref.venues.hip3]
    handlers = {
        k: ReconcileHandler(
            venue=k,
            prices=prices,
            funding=funding,
            strategy_name=args.strategy,
            slippage_bps=ref.execution_assumptions.slippage_bps,
            fee_bps=ref.execution_assumptions.fee_bps,
            min_order_usd=ref.execution_assumptions.min_order_usd,
        )
        for k in venues_keys
    }
    perp = handlers["perp"]
    hip3 = {k.removeprefix("hip3:"): h for k, h in handlers.items() if k != "perp"}

    # Walk bars, replay decide.
    state = StateStore(args.strategy, "reconcile")
    from wayfinder_paths.core.perps.context import SignalFrame  # noqa: PLC0415

    raw_sig = signal_fn(prices, funding, dict(ref.params))
    if isinstance(raw_sig, pd.DataFrame):
        signal_frame = SignalFrame(targets=raw_sig)
    else:
        signal_frame = raw_sig

    expected_bars: list[pd.Timestamp] = []
    all_intents: list[dict[str, Any]] = []
    recorded_live_intents: list[dict[str, Any]] = []
    for i, t in enumerate(prices.index):
        snap_nav = 0.0
        for h in handlers.values():
            h.set_bar(i)
            snap = h.load_snapshot_at(t.to_pydatetime())
            if h.venue == "perp":
                snap_nav = float(snap.get("nav") or 0.0)
            for live_intent in h.recorded_live_intents:
                rec = dict(live_intent)
                rec["bar_t"] = str(t)
                rec.setdefault("venue", h.venue)
                recorded_live_intents.append(rec)
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
        expected_bars.append(t)
        for h in handlers.values():
            for intent in h.drain_intents():
                intent_serialisable = dict(intent)
                intent_serialisable["placed_at_t"] = str(intent["placed_at_t"])
                intent_serialisable["venue"] = h.venue
                all_intents.append(intent_serialisable)

    # Pull live fills.
    fills: list[dict[str, Any]] = []
    if not args.no_fills:
        try:
            from wayfinder_paths.adapters.hyperliquid_adapter.adapter import (
                HyperliquidAdapter,  # noqa: PLC0415
            )
            from wayfinder_paths.mcp.scripting import get_adapter  # noqa: PLC0415

            adapter = await get_adapter(HyperliquidAdapter, args.strategy)
            ok, raw = await adapter.get_user_fills(adapter.account_address)
            if ok and isinstance(raw, list):
                # Filter to window.
                start_ms = pd.Timestamp(args.start).timestamp() * 1000
                end_ms = pd.Timestamp(args.end).timestamp() * 1000
                fills = [
                    f for f in raw if start_ms <= float(f.get("time", 0)) <= end_ms
                ]
                print(f"Pulled {len(fills)} live fills in window")
        except Exception as e:  # noqa: BLE001
            warnings.append(f"live fills fetch failed: {e}")
            print(f"⚠ live fills fetch failed: {e}")

    diff = _diff_axes(all_intents, fills, prices.index)
    strict_diff = _strict_intent_diff(all_intents, recorded_live_intents)

    out_dir = sd / "reconciliation"
    out_dir.mkdir(parents=True, exist_ok=True)
    run_ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"{run_ts}.json"
    payload = {
        "strategy": args.strategy,
        "ref_hash": ref.produced.ref_hash,
        "window": {"start": args.start, "end": args.end, "bars": len(prices.index)},
        "warnings": warnings,
        "intents": all_intents,
        "recorded_live_intents": recorded_live_intents,
        "live_fills": fills,
        "diff": diff,
        "strict_intent_diff": strict_diff,
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nWrote {out_path}")

    # Headline.
    print("\n--- Reconciliation summary ---")
    tt = diff["trigger_timing"]
    print(
        f"  trigger_timing  : {tt['bars_with_intents']}/{tt['expected_bars']} bars active "
        f"(missing={len(tt['missing_trigger_bars'])}, unexpected={len(tt['unexpected_trigger_bars'])})"
    )
    dp = diff["decision_parity"]
    print(
        f"  decision_parity : {len(dp['missed_intents'])} missed, "
        f"{len(dp['unexpected_fills'])} unexpected"
    )
    print(f"  size_drift      : {len(diff['size_drift'])} (>1%)")
    print(f"  fill_price_drift: {len(diff['fill_price_drift'])} samples")
    print(f"  fill_completion : {len(diff['fill_completion'])} matched buckets")
    sd_cnt = len(strict_diff["size_drifts"])
    print(
        f"  strict_intents  : {len(strict_diff['matched_buckets'])} matched, "
        f"{len(strict_diff['replay_only'])} replay-only, "
        f"{len(strict_diff['live_only'])} live-only, "
        f"{sd_cnt} size-drifts"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
