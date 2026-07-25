"""PnL attribution: where the alpha is, where the bleed is, and where the
forward book deviates from its own backtest expectation.

This is the human quant's primary daily input, computed deterministically:
both trade populations (backtest baseline + forward record) sliced by symbol,
entry reason, exit reason, session, regime trend, archetype, and hold-length
bucket — plus EXPECTATION DELTAS (forward slice vs the same backtest slice),
which is where "the model disagrees with reality" becomes a named, ranked
fact instead of a vibe. Small-n slices are flagged, never hidden.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.trade_forensics import classify_trade_archetype

MIN_DELTA_N = 5
_HOLD_BUCKETS = (
    (0, 6, "<30m"),
    (6, 24, "30m-2h"),
    (24, 96, "2h-8h"),
    (96, 10**9, ">8h"),
)


def _hold_bucket(row: dict[str, Any]) -> str | None:
    try:
        entry = pd.Timestamp(str(row["entry_ts"]))
        exit_ = pd.Timestamp(str(row["exit_ts"]))
    except (KeyError, ValueError):
        return None
    bars = (exit_ - entry).total_seconds() / 300
    for lo, hi, label in _HOLD_BUCKETS:
        if lo <= bars < hi:
            return label
    return None


def _slice_keys(row: dict[str, Any]) -> dict[str, str | None]:
    regime = row.get("regime_at_entry") or {}
    return {
        "symbol": row.get("symbol"),
        "entry_reason": row.get("entry_reason"),
        "exit_reason": row.get("exit_reason"),
        "session": regime.get("session"),
        "regime_trend": regime.get("trend"),
        "archetype": row.get("archetype") or classify_trade_archetype(row),
        "hold_bucket": _hold_bucket(row),
    }


def _decompose(rows: list[dict[str, Any]]) -> dict[str, Any]:
    slices: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        realized = float(row.get("realized_bps") or 0.0)
        for dim, value in _slice_keys(row).items():
            if value is None:
                continue
            bucket = slices.setdefault(dim, {}).setdefault(
                str(value), {"n": 0, "total_bps": 0.0, "wins": 0}
            )
            bucket["n"] += 1
            bucket["total_bps"] += realized
            bucket["wins"] += int(realized > 0)
    out: dict[str, Any] = {}
    for dim, groups in slices.items():
        out[dim] = {
            value: {
                "n": g["n"],
                "total_bps": round(g["total_bps"], 1),
                "avg_bps": round(g["total_bps"] / g["n"], 1),
                "win_rate": round(g["wins"] / g["n"], 3),
            }
            for value, g in sorted(groups.items())
        }
    return out


def _expectation_deltas(
    backtest: dict[str, Any], forward: dict[str, Any]
) -> list[dict[str, Any]]:
    deltas: list[dict[str, Any]] = []
    for dim, fwd_groups in forward.items():
        for value, fwd in fwd_groups.items():
            expected = (backtest.get(dim) or {}).get(value)
            if expected is None:
                continue
            deltas.append(
                {
                    "slice": f"{dim}={value}",
                    "forward_n": fwd["n"],
                    "avg_bps_delta": round(fwd["avg_bps"] - expected["avg_bps"], 1),
                    "win_rate_delta": round(fwd["win_rate"] - expected["win_rate"], 3),
                    "expected_avg_bps": expected["avg_bps"],
                    "forward_avg_bps": fwd["avg_bps"],
                    "small_n": fwd["n"] < MIN_DELTA_N,
                }
            )
    # Anomaly ranking: adequately-sampled slices first, by deviation size.
    deltas.sort(key=lambda d: (d["small_n"], -abs(d["avg_bps_delta"])))
    return deltas


def _load_rows(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from wayfinder_paths.jobs.execution.driver import _read_jsonl_tail

    forward = _read_jsonl_tail(
        root / "results" / "forward" / "trade_forensics.jsonl", 500
    )
    backtest_path = root / "results" / "backtest" / "trade_forensics.json"
    backtest: list[dict[str, Any]] = []
    if backtest_path.exists():
        try:
            backtest = list(
                json.loads(backtest_path.read_text(encoding="utf-8")).get("trades")
                or []
            )
        except ValueError:
            backtest = []
    return backtest, forward


def _fee_summary(root: Path) -> dict[str, Any]:
    from wayfinder_paths.jobs.execution.driver import _read_jsonl_tail

    fills = _read_jsonl_tail(root / "results" / "forward" / "fills.jsonl", 1000)
    fees = [float(f.get("fee") or 0.0) for f in fills]
    return {"forward_fills": len(fills), "forward_fees_total": round(sum(fees), 4)}


def attribution_job(job_id: str, *, store: JobStore | None = None) -> dict[str, Any]:
    store = store or JobStore()
    root = store.job_dir(job_id)
    backtest_rows, forward_rows = _load_rows(root)
    if not backtest_rows and not forward_rows:
        raise ValueError(
            "no forensics rows found — run `wayfinder job backtest` (writes "
            "results/backtest/trade_forensics.json) and let live ticks "
            "accumulate results/forward/trade_forensics.jsonl"
        )
    backtest = _decompose(backtest_rows)
    forward = _decompose(forward_rows)
    result = {
        "backtest_trades": len(backtest_rows),
        "forward_trades": len(forward_rows),
        "backtest": backtest,
        "forward": forward,
        "expectation_deltas": _expectation_deltas(backtest, forward),
        "fees": _fee_summary(root),
        "read": (
            "PnL decomposition (bps of entry) for the backtest population and "
            "the forward record, plus expectation deltas per slice. The "
            "DIAGNOSIS lives in: archetype counts (which failure mode "
            "dominates), and the top adequately-sampled deltas (where forward "
            "behavior deviates from the model's own expectation). Treat "
            "small_n slices as anecdotes."
        ),
    }
    out = root / "results" / "research" / "attribution.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    return result
