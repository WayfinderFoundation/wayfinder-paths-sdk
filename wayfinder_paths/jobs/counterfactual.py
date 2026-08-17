"""Post-apply THREE-BOOK prospective: incumbent shadow (A), candidate shadow
(B), and the actual book (C) over identical forward bars.

A proposal that gates or reshapes entries leaves no live evidence of what it
cost — skipped trades never print. This module makes the counterfactual
mechanical instead of something the agent must remember to reconstruct: after
any promoted proposal, the rollback backup (`applications/{pid}/backup/`)
holds the exact pre-apply strategy (A) and the active workspace holds the
promoted one (B); both run through the same simulator the backtests use over
the same venue bars, and are diffed against the live book (C). The two-book
delta (C − A) cannot say WHY a promotion underperforms — three books can:

  strategy_effect  = B − A   (both simulated — pure effect of the change)
  execution_effect = C − B   (same strategy, real fills vs simulated fills)

The wake prompt renders the result, so "the old revision is beating the new
one, and it is an execution problem, not a strategy problem" is read, not
recomputed.

Basis caveat (recorded in the artifact): both shadows size off the
simulator's base equity while the live book sizes off its own equity at
apply time, so absolute PnL is comparable in direction and rough magnitude,
not to the cent.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore

COUNTERFACTUAL_PATH = "results/forward/counterfactual.json"
# EMA-50 on a 30m view derived from 5m bars needs 600 bars; 720 covers the
# slowest indicator families in the priors library with margin.
_WARMUP_BARS = 720
# Hyperliquid's candle endpoint caps near 5000 rows per request.
_MAX_FETCH_BARS = 4500
_MIN_WINDOW_BARS = 12
_RECOMPUTE_AFTER_S = 6 * 3600
# Worker-safety: give up on the compute lock quickly (see _compute).
_LOCK_TIMEOUT_S = 60.0
_EXAMPLE_LIMIT = 5

BASIS_NOTE = (
    "Three books over identical forward bars. shadow (A) = the PRE-apply "
    "strategy (rollback backup) replayed by the backtest simulator; "
    "active_shadow (B) = the PROMOTED strategy through the same simulator; "
    "actual (C) = the live paper book. effects.strategy_effect = B - A "
    "(what the change itself did, execution held equal); "
    "effects.execution_effect = C - B (same strategy, real fills vs "
    "simulated — slippage, missed entries, sizing base). delta_net_pnl = "
    "C - A = their sum: negative means the pre-change revision would have "
    "done better since apply. Shadows size off simulator base equity, so "
    "compare direction and magnitude, not cents. entries_skipped_by_change "
    "fired in shadow A but not the live book; entries_added_by_change is "
    "the reverse. entries_execution_missed fired in active_shadow B but "
    "not the live book (the strategy wanted them; execution never printed "
    "them); entries_execution_extra is the reverse."
)


def load_counterfactual(store: JobStore, job_id: str) -> dict[str, Any] | None:
    doc = store.read_json(job_id, COUNTERFACTUAL_PATH)
    return doc if isinstance(doc, dict) else None


def last_promotion(root: Path) -> dict[str, Any] | None:
    path = root / "versions" / "revisions.jsonl"
    if not path.exists():
        return None
    last: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("proposal_id"):
            last = row
    return last


def counterfactual_job(
    job_id: str,
    *,
    store: JobStore | None = None,
    force: bool = False,
    fetch_bars: Callable[..., list[dict[str, Any]]] | None = None,
    simulate: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    store = store or JobStore()
    root = store.job_dir(job_id)

    promotion = last_promotion(root)
    if not promotion:
        return _unavailable("no promoted proposal on record")
    proposal_id = str(promotion["proposal_id"])
    applied_at = str(promotion.get("ts") or "")
    revision = str(promotion.get("revision") or "")

    backup_root = root / "applications" / proposal_id / "backup"
    backup_yaml = backup_root / "job.yaml"
    if not (backup_root / "workspace").is_dir() or not backup_yaml.exists():
        return _unavailable(f"rollback backup missing for {proposal_id}")

    trades_path = root / "results" / "forward" / "trades.jsonl"
    actual_closes_total = _count_lines(trades_path)
    cached = load_counterfactual(store, job_id)
    if not force and cached and cached.get("available"):
        fingerprint = cached.get("fingerprint") or {}
        fresh = (
            fingerprint.get("revision") == revision
            and fingerprint.get("actual_closes_total") == actual_closes_total
            and _age_seconds(str(cached.get("computed_at"))) < _RECOMPUTE_AFTER_S
        )
        if fresh:
            return cached

    try:
        artifact = _compute(
            store,
            job_id,
            root,
            backup_root=backup_root,
            proposal_id=proposal_id,
            applied_at=applied_at,
            revision=revision,
            fetch_bars=fetch_bars,
            simulate=simulate,
        )
    except Exception as exc:  # noqa: BLE001 — wake context must not die on this
        store.append_journal(
            job_id,
            {
                "type": "counterfactual_failed",
                "proposal_id": proposal_id,
                "error": str(exc)[:400],
            },
        )
        return _unavailable(f"compute failed: {exc}")

    artifact["fingerprint"] = {
        "revision": revision,
        "actual_closes_total": actual_closes_total,
    }
    store.write_json(job_id, COUNTERFACTUAL_PATH, artifact)
    _maybe_record_promotion_verdict(store, job_id, artifact)
    return artifact


_VERDICTS_PATH = "state/promotion_verdicts.json"
# Verdict maturity defaults — overridable per job via the constitution's
# `verdict:` block (a universal 3-day/3-close rule mis-times both scalpers
# and slow holders). maximum_days caps how long a verdict may stay pending
# before it is finalized as insufficient_evidence.
_VERDICT_DEFAULTS = {
    "minimum_days": 3.0,
    "minimum_closed_trades": 3,
    "maximum_days": 30.0,
    "minimum_material_delta": 0.25,
}

VERDICT_STATES = (
    "pending",
    "insufficient_evidence",
    "beat",
    "neutral",
    "hurt",
    "censored_by_next_change",
)


def _verdict_config(root: Path) -> dict[str, Any]:
    from wayfinder_paths.jobs.constitution import load_constitution

    constitution = load_constitution(root)
    block = constitution.get("verdict")
    merged = dict(_VERDICT_DEFAULTS)
    if isinstance(block, dict):
        merged.update({k: block[k] for k in _VERDICT_DEFAULTS if k in block})
    return merged


def _maybe_record_promotion_verdict(
    store: JobStore, job_id: str, artifact: dict[str, Any]
) -> None:
    """Once per promotion: classify the forward shadow comparison and persist
    it — the promotion-reliability datapoint the evolution ledger aggregates.

    States: beat/neutral/hurt (mature evidence); insufficient_evidence (the
    window aged out before enough closes); censored_by_next_change (a newer
    promotion re-anchored the shadow before this verdict matured — recorded,
    never silently dropped). Never raises."""
    try:
        proposal_id = str(artifact.get("proposal_id") or "")
        window = artifact.get("window") or {}
        if not proposal_id or not artifact.get("available"):
            return
        root = store.job_dir(job_id)
        config = _verdict_config(root)
        verdicts = store.read_json(job_id, _VERDICTS_PATH) or {}

        # CENSORING: any prior promotion still without a verdict when a NEW
        # promotion re-anchors the shadow gets censored_by_next_change — its
        # forward window is now confounded by the newer change.
        censored = []
        for prior_id, prior in list(verdicts.items()):
            if prior_id != proposal_id and prior.get("verdict") == "pending":
                prior.update(
                    {
                        "verdict": "censored_by_next_change",
                        "censored_by": proposal_id,
                        "recorded_at": utc_now_iso(),
                    }
                )
                censored.append(prior_id)
        for prior_id in censored:
            store.append_journal(
                job_id,
                {
                    "type": "promotion_verdict",
                    "proposal_id": prior_id,
                    **verdicts[prior_id],
                },
            )

        existing = verdicts.get(proposal_id)
        if existing and existing.get("verdict") != "pending":
            if censored:
                store.write_json(job_id, _VERDICTS_PATH, verdicts)
            return

        days = float(window.get("days") or 0.0)
        closes = max(
            int((artifact.get("actual") or {}).get("closes") or 0),
            int((artifact.get("shadow") or {}).get("closes") or 0),
        )
        mature = days >= float(config["minimum_days"]) and closes >= int(
            config["minimum_closed_trades"]
        )
        aged_out = days >= float(config["maximum_days"])
        if not mature and not aged_out:
            # Track the open verdict so censoring has something to censor.
            verdicts[proposal_id] = {
                "verdict": "pending",
                "window_days": days,
                "closes": closes,
                "recorded_at": utc_now_iso(),
            }
            store.write_json(job_id, _VERDICTS_PATH, verdicts)
            return

        if not mature and aged_out:
            verdict = "insufficient_evidence"
            delta = float(artifact.get("delta_net_pnl") or 0.0)
        else:
            delta = float(artifact.get("delta_net_pnl") or 0.0)
            shadow_pnl = float((artifact.get("shadow") or {}).get("net_pnl") or 0.0)
            threshold = max(
                float(config["minimum_material_delta"]), 0.02 * abs(shadow_pnl)
            )
            verdict = (
                "beat"
                if delta > threshold
                else "hurt"
                if delta < -threshold
                else "neutral"
            )
        record = {
            "verdict": verdict,
            "delta_net_pnl": delta,
            "window_days": days,
            "closes": closes,
            "recorded_at": utc_now_iso(),
        }
        # Three-book split, when the artifact has it: a "hurt" verdict whose
        # delta is execution_effect is an execution problem, not evidence
        # against the strategy change — the ledger aggregates both.
        effects = artifact.get("effects") or {}
        if effects:
            record["strategy_effect"] = effects.get("strategy_effect")
            record["execution_effect"] = effects.get("execution_effect")
        verdicts[proposal_id] = record
        store.write_json(job_id, _VERDICTS_PATH, verdicts)
        store.append_journal(
            job_id,
            {
                "type": "promotion_verdict",
                "proposal_id": proposal_id,
                **record,
            },
        )
        # A matured verdict is a research event: "that change did nothing"
        # (or hurt/beat) is the license to advance the next candidate —
        # observed live: a neutral verdict was followed by days of pure
        # health-check wakes because nothing said "act on it".
        try:
            from wayfinder_paths.jobs.triggers import fire_triggers

            fire_triggers(
                store,
                store.load(job_id),
                ["verdict_matured"],
                source="promotion_verdict",
            )
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001 — bookkeeping must not break the wake
        return


def _compute(
    store: JobStore,
    job_id: str,
    root: Path,
    *,
    backup_root: Path,
    proposal_id: str,
    applied_at: str,
    revision: str,
    fetch_bars: Callable[..., list[dict[str, Any]]] | None,
    simulate: Callable[..., Any] | None,
) -> dict[str, Any]:
    import pandas as pd
    import yaml

    from wayfinder_paths.jobs.execution.primitives import (
        ExecutionSpec,
        bar_interval_seconds,
    )
    from wayfinder_paths.jobs.execution.simulator import (
        PreparedExecutionDataset,
        simulate_execution,
    )
    from wayfinder_paths.jobs.execution.validation import resolve_execution_spec

    active_data = yaml.safe_load((root / "job.yaml").read_text(encoding="utf-8")) or {}
    backup_data = (
        yaml.safe_load(backup_root.joinpath("job.yaml").read_text(encoding="utf-8"))
        or {}
    )

    active_spec_data, _ = resolve_execution_spec(root, active_data)
    # The spec file lives at the job root (outside the backup), so an embedded
    # spec in the backup job.yaml wins and the active file is the fallback.
    backup_spec_data, _ = resolve_execution_spec(root, backup_data)
    if not backup_spec_data:
        raise RuntimeError("execution_spec unresolvable for backup revision")
    if (active_spec_data or {}).get("data_contract") != backup_spec_data.get(
        "data_contract"
    ):
        return _unavailable(
            "data contract changed by the proposal — shadow not comparable"
        )

    spec = ExecutionSpec.from_dict(backup_spec_data)
    params = dict(backup_data.get("execution_params") or {})
    active_spec = ExecutionSpec.from_dict(active_spec_data)
    active_params = dict(active_data.get("execution_params") or {})
    active_script = store.resolve_script_entrypoint(job_id, active_data)
    if active_script is None or not active_script.exists():
        raise RuntimeError(f"active entrypoint not found: {active_script}")
    bar_interval = spec.data_contract.get("bar_interval")
    interval_seconds = bar_interval_seconds(bar_interval)
    if not interval_seconds:
        raise RuntimeError("execution_spec.data_contract.bar_interval missing")
    symbols = [
        str(symbol)
        for symbol in (params.get("symbols") or spec.data_contract.get("symbols") or [])
    ]
    if not symbols:
        raise RuntimeError("no symbols configured")

    script = store.resolve_script_entrypoint(
        job_id, backup_data, candidate_dir=backup_root
    )
    if script is None or not script.exists():
        raise RuntimeError(f"backup entrypoint not found: {script}")

    now = pd.Timestamp.now(tz="UTC")
    apply_ts = pd.Timestamp(applied_at)
    if apply_ts.tzinfo is None:
        apply_ts = apply_ts.tz_localize("UTC")
    bars_since_apply = int((now - apply_ts).total_seconds() // interval_seconds)
    if bars_since_apply < _MIN_WINDOW_BARS:
        return _unavailable(
            f"only {bars_since_apply} bars since apply — too fresh to compare"
        )
    lookback = min(bars_since_apply + _WARMUP_BARS, _MAX_FETCH_BARS)

    fetch = fetch_bars or _fetch_venue_bars
    rows = fetch(
        spec=spec,
        params=params,
        symbols=symbols,
        bar_interval=str(bar_interval),
        lookback_bars=lookback,
        as_of=now,
    )
    if not rows:
        raise RuntimeError("no bars returned for the counterfactual window")
    dataset = PreparedExecutionDataset.from_rows(
        rows, {"source": "counterfactual_venue_fetch", "lookback_bars": lookback}
    )
    timestamps = sorted({pd.Timestamp(row["timestamp"]) for row in rows})
    # If the fetch could not reach back a full warmup before apply, indicators
    # are still warming inside the window — start the comparison late instead
    # of comparing a half-warmed shadow.
    warm_index = min(_WARMUP_BARS, max(len(timestamps) - 1, 0))
    warm_ready = timestamps[warm_index]
    if warm_ready.tzinfo is None:
        warm_ready = warm_ready.tz_localize("UTC")
    effective_from = max(apply_ts, warm_ready)

    from wayfinder_paths.jobs.compute_lock import heavy_compute_lock

    run = simulate or simulate_execution
    # Short lock wait: this monitor runs inside a runner WORKER (cap 2) on
    # the wake path — waiting minutes for the lock would hold a worker and
    # delay script ticks. It is stamp-gated; skipping a cycle is free, and
    # ComputeLockBusy degrades to the journaled-unavailable path like any
    # other failure.
    with heavy_compute_lock(
        repo_root=store.repo_root,
        label=f"counterfactual:{job_id}",
        timeout_s=_LOCK_TIMEOUT_S,
    ):
        result = run(script, dataset, spec, params)
        active_result = run(active_script, dataset, active_spec, active_params)
    shadow_rows = [dict(row) for row in result.trades]
    active_rows = [dict(row) for row in active_result.trades]

    shadow_entries = _entries(shadow_rows, effective_from, interval_seconds)
    active_shadow_entries = _entries(active_rows, effective_from, interval_seconds)
    shadow_closes = _sim_closes(shadow_rows, effective_from)
    active_shadow_closes = _sim_closes(active_rows, effective_from)
    shadow_net = _sim_net(shadow_closes)
    active_shadow_net = _sim_net(active_shadow_closes)

    actual_close_rows = [
        row
        for row in _read_jsonl(root / "results" / "forward" / "trades.jsonl")
        if _ts(row.get("closed_at") or row.get("timestamp")) >= effective_from
    ]
    actual_net = sum(float(row.get("net_pnl") or 0.0) for row in actual_close_rows)
    actual_fill_rows = [
        row
        for row in _read_jsonl(root / "results" / "forward" / "fills.jsonl")
        if str(row.get("status") or "filled") == "filled" and not row.get("reduce_only")
    ]
    actual_entries = _entries(actual_fill_rows, effective_from, interval_seconds)

    skipped = sorted(set(shadow_entries) - set(actual_entries))
    added = sorted(set(actual_entries) - set(shadow_entries))
    execution_missed = sorted(set(active_shadow_entries) - set(actual_entries))
    execution_extra = sorted(set(actual_entries) - set(active_shadow_entries))

    by_symbol: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        by_symbol[symbol] = {
            "actual_net_pnl": round(
                sum(
                    float(row.get("net_pnl") or 0.0)
                    for row in actual_close_rows
                    if str(row.get("symbol")) == symbol
                ),
                4,
            ),
            "shadow_net_pnl": round(
                sum(
                    float(row.get("realized_pnl_delta") or 0.0)
                    for row in shadow_closes
                    if str(row.get("symbol")) == symbol
                ),
                4,
            ),
            "active_shadow_net_pnl": round(
                sum(
                    float(row.get("realized_pnl_delta") or 0.0)
                    for row in active_shadow_closes
                    if str(row.get("symbol")) == symbol
                ),
                4,
            ),
            "actual_closes": sum(
                1 for row in actual_close_rows if str(row.get("symbol")) == symbol
            ),
            "shadow_closes": sum(
                1 for row in shadow_closes if str(row.get("symbol")) == symbol
            ),
            "active_shadow_closes": sum(
                1 for row in active_shadow_closes if str(row.get("symbol")) == symbol
            ),
        }

    return {
        "available": True,
        "proposal_id": proposal_id,
        "applied_at": applied_at,
        "active_revision": revision,
        "window": {
            "from": effective_from.isoformat(),
            "to": now.isoformat(),
            "days": round((now - effective_from).total_seconds() / 86400.0, 1),
            "warmup_truncated": bool(effective_from > apply_ts),
        },
        "actual": {"closes": len(actual_close_rows), "net_pnl": round(actual_net, 4)},
        "shadow": {"closes": len(shadow_closes), "net_pnl": round(shadow_net, 4)},
        "active_shadow": {
            "closes": len(active_shadow_closes),
            "net_pnl": round(active_shadow_net, 4),
        },
        "delta_net_pnl": round(actual_net - shadow_net, 4),
        "effects": {
            "strategy_effect": round(active_shadow_net - shadow_net, 4),
            "execution_effect": round(actual_net - active_shadow_net, 4),
            "total_delta": round(actual_net - shadow_net, 4),
        },
        "by_symbol": by_symbol,
        "entries_skipped_by_change": {
            "count": len(skipped),
            "examples": [_entry_dict(item) for item in skipped[:_EXAMPLE_LIMIT]],
        },
        "entries_added_by_change": {
            "count": len(added),
            "examples": [_entry_dict(item) for item in added[:_EXAMPLE_LIMIT]],
        },
        "entries_execution_missed": {
            "count": len(execution_missed),
            "examples": [
                _entry_dict(item) for item in execution_missed[:_EXAMPLE_LIMIT]
            ],
        },
        "entries_execution_extra": {
            "count": len(execution_extra),
            "examples": [
                _entry_dict(item) for item in execution_extra[:_EXAMPLE_LIMIT]
            ],
        },
        "_basis": BASIS_NOTE,
        "computed_at": utc_now_iso(),
    }


def _fetch_venue_bars(
    *,
    spec: Any,
    params: dict[str, Any],
    symbols: list[str],
    bar_interval: str,
    lookback_bars: int,
    as_of: Any,
) -> list[dict[str, Any]]:
    import asyncio

    from wayfinder_paths.jobs.execution.primitives import CompletedBarsView
    from wayfinder_paths.jobs.execution.venues import build_adapter

    async def _fetch() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        # mode="paper" builds the read-only market-data side; no signing/keys.
        for venue in spec.venues or ["hyperliquid"]:
            adapter = build_adapter(venue, mode="paper", spec=spec, params=params)
            view = await adapter.feed.get_completed_bars(
                symbols, bar_interval, lookback_bars=lookback_bars, as_of=as_of
            )
            rows.extend(view.to_rows())
        if not rows:
            raise RuntimeError("no completed bars returned by any venue feed")
        return CompletedBarsView.from_rows(rows).to_rows()

    return asyncio.run(_fetch())


def _sim_closes(
    rows: list[dict[str, Any]], effective_from: Any
) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("reduce_only") and _ts(row.get("timestamp")) >= effective_from
    ]


def _sim_net(closes: list[dict[str, Any]]) -> float:
    return sum(float(row.get("realized_pnl_delta") or 0.0) for row in closes)


def _entries(
    rows: list[dict[str, Any]], effective_from: Any, interval_seconds: int
) -> list[tuple[str, str, str]]:
    """Entry identity keys: (symbol, bar-floored ts, position side)."""
    import pandas as pd

    keys: list[tuple[str, str, str]] = []
    for row in rows:
        if row.get("reduce_only"):
            continue
        ts = _ts(row.get("timestamp"))
        if ts is pd.NaT or ts < effective_from:
            continue
        floored = pd.Timestamp(
            (ts.value // (interval_seconds * 1_000_000_000))
            * interval_seconds
            * 1_000_000_000,
            tz="UTC",
        )
        side = "long" if str(row.get("side") or "").lower() == "buy" else "short"
        keys.append((str(row.get("symbol")), floored.isoformat(), side))
    return keys


def _entry_dict(key: tuple[str, str, str]) -> dict[str, str]:
    symbol, ts, side = key
    return {"symbol": symbol, "ts": ts, "side": side}


def _ts(value: Any) -> Any:
    import pandas as pd

    ts = pd.Timestamp(str(value)) if value else pd.NaT
    if ts is not pd.NaT and ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(
        1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    )


def _age_seconds(computed_at: str) -> float:
    import pandas as pd

    try:
        computed = pd.Timestamp(computed_at)
    except ValueError:
        return float("inf")
    if computed.tzinfo is None:
        computed = computed.tz_localize("UTC")
    return float((pd.Timestamp.now(tz="UTC") - computed).total_seconds())


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason}
