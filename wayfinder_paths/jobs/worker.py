from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from wayfinder_paths.core.clients.OpenCodeClient import OPENCODE_CLIENT
from wayfinder_paths.core.config import is_opencode_instance
from wayfinder_paths.jobs.derived_features import refresh_derived_features_if_stale
from wayfinder_paths.jobs.failures import disk_used_pct
from wayfinder_paths.jobs.forward import is_forward_empty
from wayfinder_paths.jobs.ledger import tail_ledger
from wayfinder_paths.jobs.lifecycle import bootstrap_directive
from wayfinder_paths.jobs.memory_hygiene import sanitize_job_memory
from wayfinder_paths.jobs.models import (
    EVOLUTION_SESSION_ARCHIVE_PATH,
    EVOLUTION_SESSION_PATH,
    JOB_AUTO_WORKER_AGENT_NAME,
    JOB_EVOLUTION_DESIGNER_AGENT_NAME,
    JOB_EVOLUTION_WORKER_AGENT_NAME,
    JOB_WORKER_AGENT_NAME,
    AgentMode,
    normalize_agent_mode,
    utc_now_iso,
)
from wayfinder_paths.jobs.research_contract import RESEARCH_CONTRACT_VERSION
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import snapshot_job, sync_all_jobs
from wayfinder_paths.jobs.wake_economy import (
    REMEDIATION_QUIET_LINE,
    WAKE_ECONOMY_PATH,
    maybe_skip_wake,
    record_full_wake,
    remediation_backed_off,
    remediation_wake_block,
)

JOB_RESULT_MARKER = "WAYFINDER_JOB_RESULT "
STABLE_PREFIX_END_MARKER = "## End Stable Cache Prefix"
DYNAMIC_CONTEXT_MARKER = "## Dynamic Wakeup Context"
VOLATILE_STABLE_KEYS = {"created_at", "updated_at", "ts"}
EVOLUTION_STAGE_RETRY_AFTER = dt.timedelta(minutes=10)


def _read_text(path: Path, *, max_chars: int = 12_000) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _read_doc_head(path: Path, *, max_chars: int) -> str:
    """Tail-keeping _read_text is for logs (recent lines matter). Reference
    docs lead with their core content — when one outgrows the budget, keep
    the HEAD. The curriculum growth in research_priors.md silently dropped
    the entire family table for weeks because the read kept the tail."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    return text[:max_chars]


def _canonical_json(data: Any, *, max_chars: int | None = None) -> str:
    text = json.dumps(data, indent=2, sort_keys=True, default=str)
    if max_chars is not None and len(text) > max_chars:
        return text[:max_chars] + "\n...<truncated>"
    return text


def _work_order(
    *,
    lane: str,
    action: str,
    objective: str,
    inputs: list[str],
    editable_paths: list[str],
    exclusions: list[str],
    completion: str,
) -> dict[str, Any]:
    """Build a prompt-context contract, never a durable workflow artifact."""
    return {
        "schema_version": "1.0",
        "lane": lane,
        "action": action,
        "objective": objective,
        "inputs": list(dict.fromkeys(item for item in inputs if item)),
        "editable_paths": list(dict.fromkeys(item for item in editable_paths if item)),
        "exclusions": list(dict.fromkeys(item for item in exclusions if item)),
        "completion": completion,
    }


def _render_work_order(order: dict[str, Any]) -> str:
    return "COMPILED WORK ORDER (one action):\n" + _canonical_json(
        order, max_chars=4_500
    )


def _worker_work_order(
    *,
    job_id: str,
    mode: str,
    apply_proposal_id: str | None,
    maintenance_ready: bool,
    remediation_overrides: bool,
    restage_tasks: list[dict[str, Any]],
    ideation_due: bool,
    gate_red: bool,
) -> dict[str, Any]:
    base = f".wayfinder/jobs/{job_id}"
    if apply_proposal_id:
        lane = "maintenance" if maintenance_ready else "application"
        return _work_order(
            lane=lane,
            action="validate_and_complete_application",
            objective=(
                f"Validate and complete approved proposal {apply_proposal_id} "
                "using its existing candidate bundle."
            ),
            inputs=[
                f"{base}/proposals/{apply_proposal_id}.json",
                f"{base}/job.yaml",
                f"{base}/workspace",
            ],
            editable_paths=["the proposal application candidate only"],
            exclusions=[
                "active workspace until deterministic promotion",
                "new strategy research",
                "new proposal creation",
            ],
            completion=(
                "Call validate_application after material edits, then call "
                "complete_application exactly once with applied or failed."
            ),
        )
    if remediation_overrides:
        return _work_order(
            lane="remediation",
            action="advance_regime_remediation",
            objective="Advance the open regime-remediation case by one accountable outcome.",
            inputs=[
                f"{base}/state/regime_remediation.json",
                f"{base}/results/research/regime_health.json",
                f"{base}/results/research/attribution.json",
                f"{base}/results/forward/summary.json",
            ],
            editable_paths=[
                f"{base}/reports/intervene",
                "an isolated proposal candidate when the evidence supports treatment",
            ],
            exclusions=[
                "routine island search",
                "replacement-alpha mining",
                "owner provenance",
                "blocking reduce-only exits",
            ],
            completion=(
                "Produce one linked green proposal, one bounded evaluation plus "
                "remediation_progress, or one structured blocker."
            ),
        )
    if restage_tasks:
        objective = "Complete the already-approved re-stage task without opening a new proposal."
        action = "restage_approved_candidate"
    elif ideation_due and mode == "intervene":
        objective = "Complete one bounded external-research expedition and persist its artifact."
        action = "complete_research_expedition"
    elif gate_red:
        objective = "Diagnose and advance the currently red deterministic gate."
        action = "advance_red_gate"
    else:
        objective = "Perform the single highest-priority sensing or intervention action for this wake."
        action = "run_intervention_wake" if mode == "intervene" else f"run_{mode}_wake"
    return _work_order(
        lane="intervention" if mode == "intervene" else mode,
        action=action,
        objective=objective,
        inputs=[
            f"{base}/job.yaml",
            f"{base}/reports",
            f"{base}/results/forward",
            f"{base}/results/research",
        ],
        editable_paths=[f"{base}/reports/{mode}", f"{base}/research"],
        exclusions=[
            "ordinary alpha candidate generation when evolution is enabled",
            "owner provenance",
            "live-mode changes outside a proposal",
        ],
        completion="Write the lane report and finish after one accountable outcome.",
    )


def _trade_forensics_block(root: Path) -> dict[str, Any]:
    """Compact exit-quality context: per-trade path metrics the agent cannot
    read off PnL rows (MAE/MFE during the hold, post-exit excursion, stop
    survival) plus the backtest-population aggregate that adjudicates them."""
    block: dict[str, Any] = {}
    forward_path = root / "results" / "forward" / "trade_forensics.jsonl"
    if forward_path.exists():
        rows = []
        for line in forward_path.read_text(encoding="utf-8").splitlines()[-5:]:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                row.pop("coverage", None)
                rows.append(row)
        if rows:
            block["recent_forward_trades"] = rows
    backtest_path = root / "results" / "backtest" / "trade_forensics.json"
    if backtest_path.exists():
        try:
            doc = json.loads(backtest_path.read_text(encoding="utf-8"))
        except ValueError:
            doc = {}
        aggregate = doc.get("aggregate")
        if aggregate:
            block["backtest_aggregate"] = aggregate
    if block:
        block["_basis"] = (
            "Exit-quality path metrics, bps of entry price, positive = in the "
            "trade's favor. hold_mae/mfe = worst/best excursion DURING the "
            "hold; post_exit_favorable = move in the trade's direction AFTER "
            "the exit (what a later exit would have captured); "
            "exit_reason 'bracket_stop' = the protective stop fired (bracket "
            "fills carry no strategy label); stop_survives = that stop width "
            "was never breached — for bracket_stop trades the scan extends "
            "through the post-exit window (the hypothetical wider-stop hold "
            "continues), for labeled exits it covers the actual hold. Forward "
            "rows are single-trade ANECDOTES — hypothesis fuel only. "
            "Adjudicate any exit tweak on backtest_aggregate + an experiments "
            "grid over the exit params with walk-forward before proposing."
        )
    return block


def _attribution_block(root: Path) -> dict[str, Any]:
    """Compact diagnosis context: archetype counts + the top expectation
    deltas from results/research/attribution.json (refreshed on demand via
    core_jobs(action="attribution")). Capped hard — this rides the 12k
    dynamic budget."""
    path = root / "results" / "research" / "attribution.json"
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    deltas = [d for d in doc.get("expectation_deltas") or [] if not d.get("small_n")]
    block: dict[str, Any] = {
        "forward_trades": doc.get("forward_trades"),
        "archetypes_forward": (doc.get("forward") or {}).get("archetype") or {},
        "top_expectation_deltas": deltas[:5],
        "_basis": (
            "Diagnosis artifact: archetype counts name the dominant failure "
            "mode; expectation_deltas are slices where the forward book "
            "deviates most from the SAME slice in the backtest (adequately "
            "sampled only). Start treatment design here. Refresh with "
            'core_jobs(action="attribution", job_id=...).'
        ),
    }
    return block


def _operator_block(store: JobStore, job_id: str) -> dict[str, Any]:
    """Operator decisions of record (state/operator.json) — who last set the
    script mode and when. Rendered in the stable prefix so agents can tell an
    authorized owner decision from the unexplained-flip incidents their halt
    discipline was built on."""
    doc = store.read_json(job_id, "state/operator.json") or {}
    if not isinstance(doc, dict) or not doc:
        return {}
    return {
        **doc,
        "_basis": (
            "Operator decisions of record. script_mode.set_by=owner means the "
            "current mode is an AUTHORIZED operator decision — never revert "
            "it, never halt solely because it changed."
        ),
    }


def _counterfactual_block(store: JobStore, job_id: str) -> dict[str, Any]:
    """Mechanical post-apply three-book: pre-apply shadow (A) and promoted
    shadow (B) replayed over the forward bars since apply, diffed against
    the actual book (C) — strategy effect (B-A) split from execution effect
    (C-B). Computed here (cached, ~6h refresh) so the evidence EXISTS every
    wake — the agent reads it, it never reconstructs counterfactuals."""
    from wayfinder_paths.jobs.counterfactual import counterfactual_job

    try:
        doc = counterfactual_job(job_id, store=store)
    except Exception as exc:  # noqa: BLE001 — wake context must not die on this
        return {"_status": f"unavailable: {exc}"}
    if not doc.get("available"):
        return {"_status": str(doc.get("reason") or "unavailable")}
    keys = (
        "proposal_id",
        "applied_at",
        "window",
        "actual",
        "shadow",
        "active_shadow",
        "delta_net_pnl",
        "effects",
        "by_symbol",
        "entries_skipped_by_change",
        "entries_added_by_change",
        "entries_execution_missed",
        "entries_execution_extra",
        "_basis",
    )
    return {key: doc[key] for key in keys if key in doc}


def _research_substrate_block(root: Path) -> dict[str, Any]:
    """Freshness of the research substrate, computed from disk EVERY wake.

    Staleness must be data the agent reads, not a memory it repeats: an
    agenda entry that parked a lane as 'feed stale' otherwise outlives the
    fix forever, because nothing in the context ever contradicts it (the
    2026-07-27 BTC-exog wedge — columns were refreshed, three wakes kept
    citing the old timestamps from the agenda)."""
    block: dict[str, Any] = {}
    bars_path = root / "results" / "backtest" / "input_bars.json"
    if bars_path.exists():
        try:
            meta = (
                json.loads(bars_path.read_text(encoding="utf-8")).get("metadata") or {}
            )
            block["dataset_fetched_at"] = meta.get("fetched_at")
            block["dataset_days"] = meta.get("days")
        except ValueError:
            pass
    feats = root / "state" / "features.jsonl"
    if feats.exists():
        newest = ""
        # Appends are batched chronologically — the tail bounds the newest ts.
        for line in feats.read_text(encoding="utf-8").splitlines()[-400:]:
            try:
                ts = str(json.loads(line).get("timestamp") or "")
            except ValueError:
                continue
            newest = max(newest, ts)
        if newest:
            block["derived_features_newest_ts"] = newest
    stamp_path = root / "results" / "research" / "derived_refresh.json"
    if stamp_path.exists():
        try:
            block["derived_refresh_stamp"] = json.loads(
                stamp_path.read_text(encoding="utf-8")
            )
        except ValueError:
            pass
    if block:
        block["_basis"] = (
            "Substrate freshness, read from disk THIS wake. Any agenda/"
            "dead-map entry blocked on 'stale feed/columns' must be checked "
            "against these timestamps EVERY wake: if they are current, the "
            "blocker no longer exists — update the agenda and run the lane. "
            "Never repeat a staleness claim from memory when this block "
            "contradicts it. To advance the dataset yourself: "
            "wayfinder job fetch-dataset (derived columns re-derive "
            "automatically as part of the build)."
        )
    return block


_IDEATION_PATH = "research/ideation/latest.json"
_IDEATION_SEEN_PATH = "research/ideation/last_seen.json"


def _ideation_thresholds(root: Path) -> tuple[int, int]:
    """(due_s, overdue_s) from the active improver spec — daily expedition,
    stamp-gated under the wake rhythm (20h, not 24h, so a 30m cadence cannot
    alias it to every-other-day)."""
    from wayfinder_paths.jobs.improver.spec import ImproverSpec

    spec = ImproverSpec.load(root)
    return spec.ideation_due_s, spec.ideation_overdue_s


def _ideation_age_s(root: Path) -> float | None:
    path = root / _IDEATION_PATH
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        stamp = dt.datetime.fromisoformat(str(doc.get("generated_at")))
    except (ValueError, TypeError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.UTC)
    return (dt.datetime.now(dt.UTC) - stamp).total_seconds()


def _ideation_bookkeeping(store: JobStore, job_id: str) -> None:
    """Mechanical accountability for the ideation contract.

    Prose ideation degenerated into "nothing new, agenda stands" 130 wakes in
    a row — no external tool was ever consulted. The contract is now an
    ARTIFACT (research/ideation/latest.json); this journals each new artifact
    into the decision log (owner-visible bucket counts) and escalates once
    when the expedition is >48h overdue."""
    root = store.job_dir(job_id)
    path = root / _IDEATION_PATH
    seen = store.read_json(job_id, _IDEATION_SEEN_PATH) or {}
    doc = None
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            doc = None
    generated_at = str((doc or {}).get("generated_at") or "")
    if doc and generated_at and generated_at != str(seen.get("generated_at") or ""):
        hypotheses = [h for h in doc.get("hypotheses") or [] if isinstance(h, dict)]
        buckets: dict[str, int] = {}
        for h in hypotheses:
            bucket = str(h.get("bucket") or "unbucketed")
            buckets[bucket] = buckets.get(bucket, 0) + 1
        store.append_journal(
            job_id,
            {
                "type": "ideation_artifact",
                "generated_at": generated_at,
                "sources": len(doc.get("sources_consulted") or []),
                "hypotheses": len(hypotheses),
                "buckets": buckets,
            },
        )
        store.write_json(job_id, _IDEATION_SEEN_PATH, {"generated_at": generated_at})
        return
    age = _ideation_age_s(root)
    overdue = age is None or age > _ideation_thresholds(root)[1]
    if overdue and seen.get("escalated_for") != generated_at:
        store.append_journal(
            job_id,
            {
                "type": "ideation_incomplete",
                "artifact_age_s": None if age is None else int(age),
            },
        )
        store.write_json(
            job_id,
            _IDEATION_SEEN_PATH,
            {**seen, "escalated_for": generated_at},
        )


def _restage_block(root: Path) -> list[dict[str, Any]]:
    """Approved proposals awaiting re-stage after a stale-baseline refusal.

    Approval carryover: the owner already approved these — the workspace moved
    under the staged candidate, so the change must be re-authored against the
    CURRENT workspace and re-staged. This is a top-priority mechanical task,
    not a new decision."""
    tasks: list[dict[str, Any]] = []
    proposals_dir = root / "proposals"
    if not proposals_dir.exists():
        return tasks
    for path in sorted(proposals_dir.glob("*.json")):
        try:
            proposal = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        application = proposal.get("application") or {}
        if proposal.get("status") != "approved" or not application.get(
            "restage_requested"
        ):
            continue
        pid = str(proposal.get("proposal_id") or path.stem)
        params = (proposal.get("proposed_change") or {}).get("execution_params")
        if params:
            instruction = (
                f"Run `wayfinder job restage {root.name} {pid}` — params "
                "updates re-stage mechanically from the recorded params."
            )
        else:
            instruction = (
                "Re-author this exact change against the CURRENT workspace: "
                f"copy .wayfinder/jobs/{root.name}/workspace to a scratch dir, "
                "apply the same change (the stale candidate at "
                f"applications/{pid}/candidate shows what it looked like), then "
                f"run `wayfinder job restage {root.name} {pid} "
                "--candidate-dir <scratch>`. Do NOT alter the approved intent; "
                "if the change no longer makes sense on the new base, reject "
                "it (agent housekeeping) and propose fresh."
            )
        tasks.append(
            {
                "proposal_id": pid,
                "summary": (proposal.get("proposed_change") or {}).get("summary"),
                "base_revision": proposal.get("base_revision"),
                "changed_files": proposal.get("changed_files") or [],
                "instruction": instruction,
            }
        )
    return tasks


def _archive_block(store: JobStore, job_id: str) -> dict[str, Any]:
    """Frontier + refuted branches: exploration cites archive state, not
    memory. Never raises."""
    try:
        from wayfinder_paths.jobs.archive import archive_snapshot_block

        return archive_snapshot_block(store, job_id)
    except Exception:  # noqa: BLE001
        return {}


def _evolution_block(store: JobStore, job_id: str) -> dict[str, Any]:
    """Promotion-reliability scoreboard: the improver reads its own audited
    outcomes instead of its memory of them. Never raises."""
    try:
        from wayfinder_paths.jobs.evolution_ledger import evolution_snapshot_block

        return evolution_snapshot_block(store, job_id)
    except Exception:  # noqa: BLE001
        return {}


def _standing_checks_block(
    root: Path,
    *,
    store: JobStore | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Mechanical routine numbers, computed by the harness each wake.

    The audit found ~30 ledger entries re-deriving `funding_mean > 0` in LLM
    sessions — threshold arithmetic done by the most expensive component in
    the system. This block does the arithmetic mechanically; a wake READS the
    numbers and compares them to its gates."""
    block: dict[str, Any] = {}
    trades_path = root / "results" / "forward" / "trades.jsonl"
    if trades_path.exists():
        per_symbol: dict[str, int] = {}
        last_close = ""
        for line in trades_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            symbol = str(row.get("symbol") or "?")
            per_symbol[symbol] = per_symbol.get(symbol, 0) + 1
            last_close = max(last_close, str(row.get("closed_at") or ""))
        if per_symbol:
            block["closed_trades"] = {
                "total": sum(per_symbol.values()),
                "per_symbol": dict(sorted(per_symbol.items())),
                "last_close_ts": last_close,
            }
    feats = root / "state" / "features.jsonl"
    if feats.exists():
        from wayfinder_paths.jobs.indicators import REGIME_LABELS

        regime_newest: dict[str, tuple[str, float]] = {}
        funding: dict[str, list[tuple[str, float]]] = {}
        # Reverse scan with early exit: newest regime per symbol + a week of
        # funding rows, without parsing the whole multi-MB store.
        scanned = 0
        for line in reversed(feats.read_text(encoding="utf-8").splitlines()):
            scanned += 1
            if scanned > 30_000:
                break
            try:
                row = json.loads(line)
            except ValueError:
                continue
            name = str(row.get("name") or "")
            symbol = str(row.get("symbol") or "")
            ts = str(row.get("timestamp") or "")
            if name == "regime_code":
                if symbol not in regime_newest or ts > regime_newest[symbol][0]:
                    try:
                        regime_newest[symbol] = (ts, float(row.get("value")))
                    except (TypeError, ValueError):
                        pass
            elif name == "funding":
                rows = funding.setdefault(symbol, [])
                if len(rows) < 42:  # ~7d of 4h readings
                    try:
                        rows.append((ts, float(row.get("value"))))
                    except (TypeError, ValueError):
                        pass
        if regime_newest:
            block["regime_now"] = {
                symbol: {
                    "label": REGIME_LABELS[int(code)]
                    if 0 <= int(code) < len(REGIME_LABELS)
                    else "unknown",
                    "as_of": ts,
                }
                for symbol, (ts, code) in sorted(regime_newest.items())
            }
        if funding:
            block["funding_recent"] = {
                symbol: {
                    "mean_1d": round(
                        sum(v for _, v in rows[:6]) / max(len(rows[:6]), 1), 9
                    ),
                    "mean_7d": round(sum(v for _, v in rows) / len(rows), 9),
                    "n": len(rows),
                    "newest_ts": max(ts for ts, _ in rows),
                }
                for symbol, rows in sorted(funding.items())
                if rows
            }
    replication = None
    rep_path = root / "results" / "backtest" / "replication.json"
    if rep_path.exists():
        try:
            rep = json.loads(rep_path.read_text(encoding="utf-8"))
        except ValueError:
            rep = None
        if isinstance(rep, dict) and rep.get("available"):
            replication = {
                "revision": rep.get("revision"),
                "declared_revision": rep.get("declared_revision"),
                "status": rep.get("status"),
                "decayed": rep.get("decayed"),
                "baseline_net_return": (rep.get("baseline") or {}).get("net_return"),
                "current_net_return": (rep.get("current") or {}).get("net_return"),
                "current_sharpe": (rep.get("current") or {}).get("sharpe"),
                "current_max_drawdown": (rep.get("current") or {}).get("max_drawdown"),
                "dataset_days": (rep.get("dataset") or {}).get("days_received")
                or (rep.get("dataset") or {}).get("days"),
            }
    if replication:
        block["backtest_replication"] = replication
    from wayfinder_paths.jobs.regime_contract import REGIME_HEALTH_PATH

    regime_path = root / REGIME_HEALTH_PATH
    if regime_path.exists():
        try:
            regime = json.loads(regime_path.read_text(encoding="utf-8"))
        except ValueError:
            regime = None
        if isinstance(regime, dict):
            from wayfinder_paths.jobs.regime_health import compact_regime_health

            block["portfolio_regime_health"] = compact_regime_health(regime)
    from wayfinder_paths.jobs.remediation import compact_remediation, load_remediation

    case = (
        load_remediation(store, job_id)
        if store is not None and job_id is not None
        else None
    )
    remediation = compact_remediation(case)
    if remediation:
        block["regime_remediation"] = remediation
        block["remediation"] = remediation_wake_block(case)
    if block:
        block["_basis"] = (
            "Routine numbers computed mechanically THIS wake — never re-fetch "
            "or re-derive them in-session; compare them to your gates and "
            "cite them. A pure status observation (runner healthy, gate "
            "still closed, no new trades) is an ops note, NOT research: "
            "write it with family operations/monitoring/no_change and it "
            "lands in the ops ledger automatically. The candidates ledger "
            "is for research verdicts only. "
            "backtest_replication status decayed/invalid/stale means the ACTIVE "
            "revision's deploy evidence is not currently trustworthy; treat it "
            "as grounds for a revert/kill or re-validation proposal. "
            "portfolio_regime_health warning/critical is an incumbent-health "
            "alarm, not a request to mine a replacement signal: cite the "
            "fresh attribution artifact before designing treatment."
        )
    return block


def _compute_status_block(root: Path) -> dict[str, Any]:
    """Mechanical box truth: is compute healthy RIGHT NOW?

    One production OOM froze research for 14 hours because the agent carried
    "OOM-blocked" claims in its agendas long after memory recovered — nothing
    in the context ever contradicted the stale belief. This block is computed
    from the box on every wake so an infrastructure claim can always be
    checked against current reality. Best-effort on every field, never
    raises."""
    from wayfinder_paths.jobs.resource_envelope import resource_snapshot

    resources = resource_snapshot(sample_cpu=True)
    available = resources.get("mem_available_mb")
    mem_available_mb = int(available) if isinstance(available, (int, float)) else None
    last_experiment_at: str | None = None
    experiments_path = root / "results" / "backtest" / "experiments.jsonl"
    try:
        for line in reversed(experiments_path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict):
                last_experiment_at = str(row.get("ts") or "") or None
            break
    except Exception:  # noqa: BLE001
        last_experiment_at = None
    last_backtest_ok_at: str | None = None
    viz_path = root / "results" / "backtest" / "visualization.json"
    try:
        last_backtest_ok_at = dt.datetime.fromtimestamp(
            viz_path.stat().st_mtime, tz=dt.UTC
        ).isoformat()
    except Exception:  # noqa: BLE001
        last_backtest_ok_at = None
    steal = resources.get("cpu_steal_pct")
    disk = disk_used_pct(root)
    try:
        loadavg: list[float] | None = [round(v, 2) for v in os.getloadavg()]
    except OSError:
        loadavg = None
    return {
        "mem_available_mb": mem_available_mb,
        "cpu_steal_pct": None if steal is None else round(steal, 1),
        "disk_used_pct": None if disk is None else round(disk, 1),
        "loadavg": loadavg,
        "last_experiment_at": last_experiment_at,
        "last_backtest_ok_at": last_backtest_ok_at,
        "_basis": (
            "Mechanical box truth computed THIS wake. Infrastructure "
            "failures (OOM, locks, timeouts) are TRANSIENT and "
            "watchdog-retried — they are FORBIDDEN as reasons to mark a "
            "research lane blocked or exhausted, in agendas or reports. "
            "NEVER carry an infrastructure claim from memory when this "
            "block contradicts it. If this block shows healthy memory and "
            "recent successful compute, every 'OOM-blocked' qualifier you "
            "have ever written is void. cpu_steal_pct above ~60 means the "
            "hypervisor is taking most of this box's CPU and local "
            "subprocesses crawl — prefer resident MCP tools for reads, "
            "defer optional heavy compute, and NEVER interpret local "
            "timeouts (exit 124) as remote outages. disk_used_pct above "
            "~85 means the jobs volume is filling toward the full-disk "
            "failure mode (dead rsync, crash-looping services) — prune "
            "stale artifacts before writing anything large."
        ),
    }


def _drop_volatile_stable_keys(value: Any) -> Any:
    match value:
        case dict():
            return {
                str(key): _drop_volatile_stable_keys(item)
                for key, item in sorted(value.items())
                if str(key) not in VOLATILE_STABLE_KEYS
            }
        case list():
            return [_drop_volatile_stable_keys(item) for item in value]
        case _:
            return value


def _render_research_priors(text: str, spec) -> str:
    tier1, tier2 = spec.tier("tier1"), spec.tier("tier2")
    tokens = {
        "%%T1_Q%%": f"{float(tier1['max_q']):.2f}",
        "%%T1_FOLDS%%": str(tier1["folds"]),
        "%%T2_Q%%": f"{float(tier2['max_q']):.2f}",
        "%%T2_FOLDS%%": str(tier2["folds"]),
        "%%T2_REGIME_Q%%": f"{float(tier2['regime_max_q']):.2f}",
        "%%T2_REGIME_N%%": str(int(tier2["regime_min_n"])),
        "%%T2_RECENT_Q%%": f"{float(tier2['recent_window_max_q']):.2f}",
        "%%PROB_SIZE_PCT%%": f"{spec.probation_max_size_fraction:.0%}",
        "%%PROB_LEGS%%": str(spec.probation_max_active_legs),
        "%%STUCK_N%%": str(spec.stuck_same_family_non_wins),
    }
    for token, value in tokens.items():
        text = text.replace(token, value)
    return text


def _portfolio_context(store: JobStore, job_id: str):
    try:
        from wayfinder_paths.jobs.portfolio import portfolio_block

        return portfolio_block(store, job_id)
    except Exception:  # noqa: BLE001
        return None


def _build_worker_prompt_sections(
    *,
    store: JobStore,
    job_id: str,
    mode: str,
    snapshot: dict[str, Any],
    apply_proposal_id: str | None = None,
    wake_source: str = "scheduled_timer",
    wake_triggers: list[str] | None = None,
    wake_id: str | None = None,
) -> dict[str, Any]:
    root = store.job_dir(job_id)
    from wayfinder_paths.jobs.improver.spec import ImproverSpec

    improver_spec = ImproverSpec.load(root)
    evolution_enabled = improver_spec.evolution_eligibility(root, job_id)["eligible"]
    intervene_scope = (
        "Intervene mode is the sensor/safety lane for this evolution-enabled "
        "job. Ordinary alpha/parameter candidate authoring, signal-family "
        "generation, and paper-probation admission belong exclusively to the "
        "evolution campaign. When deterministic research produces executable "
        "code, submit it as an evolution research seed instead of proposing it "
        "directly (`wayfinder job evolution-submit-seed`). This lane may still "
        "stage concrete risk reduction, "
        "revert/kill, operational remediation, exact behavior-equivalence "
        "maintenance, and owner-requested re-stage/application work. This rule "
        "supersedes generic candidate/probation mandates below."
        if evolution_enabled
        else "Intervene mode may create candidate proposals under the job bundle, but cannot activate them."
    )
    verdict_matured_rule = (
        "Treat a matured promotion verdict as sensing evidence: update the "
        "existing verdict/archive artifacts and run the next diagnostic or "
        "falsification. Do not author the next ordinary strategy candidate; "
        "the evolution campaign consumes this evidence."
        if evolution_enabled
        else (
            "Act on it THIS wake — propose the next candidate, run the next "
            "experiment, or record in the report precisely why not. A neutral "
            "verdict means the change did nothing: that is license to try the "
            "next candidate, not to wait."
        )
    )
    progress_rule = (
        "- SENSOR PROGRESS CONSTITUTION: when research_staleness is true, this "
        "evolution-enabled job's intervention wake must produce exactly ONE "
        "bounded "
        "deterministic diagnostic, falsification, or adjudication and record its "
        "result in an existing archive/verdict/evidence artifact. It must not "
        "open probation or author an ordinary alpha candidate merely to satisfy "
        "staleness; evolution is the sole candidate factory.\n"
        if evolution_enabled
        else (
            "- PROGRESS CONSTITUTION: `evolution.research_staleness` is "
            "visible state. When the job is healthy, no verdict is pending, "
            "and `research_staleness.stale` is true (no experiment in "
            f">{improver_spec.staleness_experiment_days:g} days, or "
            f">{improver_spec.staleness_wakes} wakes since the last "
            "proposal), the wake MUST end in exactly ONE of: (a) a staged AND "
            "executed experiment, (b) a new probation leg — the paper entry "
            "tier accepts any candidate not clearly worse than baseline, no "
            "owner approval needed (wayfinder_paths.jobs.probation."
            "open_paper_probation_leg / `wayfinder job probation-open-paper`) "
            "— or (c) an exhaustion claim FILED for mechanical coverage audit "
            "(`wayfinder job exhaustion file ...`). Stating that research is "
            "not warranted is NOT a legal outcome of a stale wake — prose "
            "never satisfies the constitution.\n"
        )
    )
    memory_md = _read_text(root / "memory.md", max_chars=6000)
    # The research prior library: idea families, prior strengths, archetype
    # mapping, and test paths. Lives in the STABLE prefix so it prompt-caches
    # across wakes instead of taxing the dynamic budget. Policy numbers
    # (tier thresholds, probation caps, stuck rule) interpolate from the
    # active improver spec — a spec change is a real behavior change.
    research_priors = _render_research_priors(
        _read_doc_head(
            Path(__file__).parent / "prompts" / "research_priors.md", max_chars=16_000
        ),
        improver_spec,
    )
    research_contract = _read_doc_head(
        Path(__file__).parent / "prompts" / "research_contract.md", max_chars=4000
    )
    memory_json = store.read_json(job_id, "memory.json", default={}) or {}
    recent_journal = _read_text(root / "journal.jsonl", max_chars=4000)
    # The cumulative research state — a curated map (dead hypotheses, open
    # ones, starved ones with unlock conditions), NOT a scrolling log. The
    # 20-row ledger tails forget; this is what makes ideation build on itself.
    research_agenda = _read_text(root / "research" / "agenda.md", max_chars=5000)
    stable_payload = {
        "job": _drop_volatile_stable_keys(snapshot["job"]),
        "memory_json": _drop_volatile_stable_keys(memory_json),
        "improver": {
            "revision": improver_spec.revision,
            "source": improver_spec.source,
            "policy": improver_spec.policy,
        },
        "research_contract_version": RESEARCH_CONTRACT_VERSION,
    }
    # Compact each recent_* detail list to its last 6 rows (25 raw trade/run
    # rows can blow the 12k canonical-json budget and, since keys serialize
    # alphabetically, starve the later high-signal keys out of the prompt).
    forward_block = {
        key: value[-6:] if key.startswith("recent_") else value
        for key, value in (snapshot.get("forward") or {}).items()
    }
    backtest_block = dict(snapshot.get("backtest") or {})
    # Fence the two blocks the agent confuses, co-located with the numbers so the
    # disambiguation leads the data (the `_`-prefixed key sorts first inside its
    # block under canonical alphabetical serialization). The backtest label is
    # UNCONDITIONAL: a backtest is always historical, and the agent conflates it
    # with forward even on non-empty wakes — it wrote a candidate-backtest result
    # into durable memory as a "forward prove-out" post-apply, which then poisons
    # every later wake. Stats stay intact; the agent needs them to diagnose.
    if backtest_block:
        backtest_block["_basis"] = (
            "HISTORICAL_BACKTEST: pre-launch/candidate simulation, NOT "
            "forward/live performance. Never restate these figures as a forward "
            "result or 'forward prove-out', and never write them to memory as "
            "forward performance."
        )
    # The zero-evidence marker only makes sense when forward is actually empty.
    if is_forward_empty(forward_block):
        forward_block["_status"] = (
            "NO_FORWARD_DATA: 0 runs/trades/orders/fills have executed — you "
            "have ZERO forward evidence this wake. Do not report any win rate, "
            "PnL, or trade count as a forward result."
        )

    restage_tasks = _restage_block(root)
    # Island rotation: routine research wakes get a deterministic search
    # assignment; apply/restage wakes and trigger wakes (bypass inside)
    # handle their event instead. Never blocks the wake.
    standing_checks = _standing_checks_block(root, store=store, job_id=job_id)
    remediation_case = standing_checks.get("regime_remediation") or {}
    remediation_actionable = remediation_case.get("state") in {
        "open",
        "evaluating",
        "blocked",
    }
    # A backed-off case (bounded blocker recorded, no forward evidence moved
    # since) never satisfies a wake: it must not consume the search
    # assignment or override research obligations.
    remediation_quiet = remediation_actionable and remediation_backed_off(
        store, job_id, remediation_case
    )
    remediation_overrides = remediation_actionable and not remediation_quiet
    search_assignment = None
    if (
        mode == "intervene"
        and apply_proposal_id is None
        and not wake_triggers
        and not restage_tasks
        and not remediation_overrides
    ):
        try:
            from wayfinder_paths.jobs.improver.scheduler import assign_island

            search_assignment = assign_island(store, job_id)
        except Exception:  # noqa: BLE001
            search_assignment = None
    dynamic_payload = {
        "scorecard": snapshot.get("scorecard") or {},
        "forward": forward_block,
        "runner_links": snapshot.get("runner_links") or {},
        "proposals": snapshot.get("proposals") or [],
        "proposal_queue": snapshot.get("proposal_queue") or {},
        "reports": snapshot.get("reports") or {},
        # Loop-protocol context: the improve loop's baseline + gate state, and
        # the exploration/decision history that keeps wakes non-amnesic (never
        # re-explore a logged no_edge/rejected candidate family unchanged).
        "backtest": backtest_block,
        "gate": snapshot.get("gate") or {},
        "operator": _operator_block(store, job_id),
        "ledgers": {
            "candidates": tail_ledger(store, job_id, "candidates", limit=20),
            "decisions": tail_ledger(store, job_id, "decisions", limit=20),
        },
        "trade_forensics": _trade_forensics_block(root),
        "attribution": _attribution_block(root),
        "post_apply_shadow": _counterfactual_block(store, job_id),
        "research_substrate": _research_substrate_block(root),
        "standing_checks": standing_checks,
        "compute_status": _compute_status_block(root),
        "evolution": _evolution_block(store, job_id),
        "archive": _archive_block(store, job_id),
        "restage_tasks": restage_tasks,
        "search_assignment": search_assignment,
        "wake": {
            "id": wake_id,
            "source": wake_source,
            "triggers": sorted(set(wake_triggers or [])),
        },
        "portfolio": _portfolio_context(store, job_id),
    }

    stable_prefix = (
        "Run a Wayfinder job worker wakeup.\n\n"
        f"Mode: {mode}\n"
        "Cache contract:\n"
        "- This prefix is intentionally stable for this job and mode.\n"
        "- Live prices, timestamps, recent logs, reports, and run results appear after "
        f"`{STABLE_PREFIX_END_MARKER}`.\n"
        "- Update durable memory only when the job's standing goals, constraints, "
        "rules, or lessons materially change.\n\n"
        "Rules:\n"
        "- Monitor mode is read-only except reports/memory.\n"
        f"- {intervene_scope}\n"
        "- Proposals stage ONLY `workspace/` + `job.yaml`; code outside `workspace/` "
        "cannot be versioned, proposed, or promoted. If the active script entrypoint "
        "resolves outside `workspace/`, your FIRST proposal must migrate it into "
        "`workspace/src/` and point `script_loop.entrypoint` at `workspace/src/<file>.py`.\n"
        "- ONE open proposal per concern: before drafting a corrected version of a "
        "proposal you made this wake or earlier, reject the superseded draft first "
        "(core_jobs(action='reject_proposal', job_id=..., proposal_id=..., "
        "reason='superseded by <new-id>')) so the owner never reviews stale drafts. "
        "ALWAYS pass a reason on self-rejections — an unreasoned rejection is "
        "recorded as an OWNER veto.\n"
        "- Rejections have provenance (`rejection.by` + `rejection.kind` on "
        "the proposal): an OWNER rejection with kind='substantive' is a "
        "DECISION, not a formality — a binding veto. kind='process' owner "
        "rejections (superseded drafts, re-stage mechanics, red-gate "
        "housekeeping) are INVITATIONS, not vetoes: file the corrected "
        "successor proposal promptly. NEVER re-propose an "
        "equivalent change after a substantive owner rejection unless you "
        "have NAMED new "
        "evidence that did not exist at rejection time, and the new proposal "
        "summary must cite both the rejected proposal id and that evidence. "
        "- Applying an approved proposal is a separate lifecycle: pending proposals do not pause jobs, "
        "approval only queues application, and runner loops pause only after the apply worker claims the proposal.\n"
        "- Auto mode may execute live trades only inside the configured auto_limits.\n"
        "- Never move funds, send onchain transactions, or execute contracts.\n"
        "- Paper/live mode lives in `job.yaml` (`script_loop.mode`). Change it ONLY "
        "by a proposal that edits `script_loop.mode` (intervene) or, out of band, "
        "`core_jobs(action='set_script_mode', ...)` — both recompile the runner env. "
        "NEVER patch a runner env var (`WAYFINDER_JOB_MODE`) to flip mode: the env is "
        "baked from job.yaml, so a hand-patch is a split-brain the next recompile "
        "reverts. If the runner mode and job.yaml disagree, the fix is a recompile "
        "(sync/set_script_mode), not an env edit.\n"
        "- OWNER PROVENANCE IS NEVER YOURS TO CLAIM. You may not pass "
        "`--by owner` (or set_by='owner') to ANY command — mode stamps, "
        "governance commits, or anything else. Audit flags that say owner "
        "action is required (e.g. `unstamped_live_mode`) are RESOLVED BY THE "
        "OWNER: your job is to surface them prominently in your report, "
        "never to clear them yourself. A provenance claim is a permanent "
        "governance violation on your record.\n"
        "- script_loop.mode is OPERATOR-OWNED. The stable spec's `operator."
        "script_mode` block records who last set it and when. When set_by is "
        "`owner`, the mode is an AUTHORIZED decision: NEVER flip it back and "
        "NEVER halt solely because the mode changed — halt is for concrete "
        "risk (mounting losses, venue failure, runaway sizing), and mode "
        "concerns belong in your wake report for the owner to read. Only a "
        "mode change with NO operator record is the split-brain incident "
        "case — reconcile that via recompile and say so in the report.\n"
        "- execution_params.initial_capital and wallet_label are OPERATOR-"
        "OWNED too: the owner's Fund/Withdraw buttons move venue money and "
        "write capital in lockstep (journal: operator_initial_capital_set). "
        "NEVER edit initial_capital yourself — equity_recon drift equal to a "
        "recent deposit/withdrawal is the EXPECTED signature of owner "
        "funding, not a mismatch to fix. If you must move job bankroll, the "
        "only sanctioned path is core_jobs venue_deposit / venue_withdraw "
        "(same lockstep the buttons use); raw hyperliquid deposit/withdraw "
        "tools against the job's bound wallet de-sync sizing.\n"
        "- Use structured forward results first (summary, runs, trades, orders, fills); "
        "raw runner logs are fallback/debug only.\n"
        "- Wallet/venue errors: the live wallet is `execution_params.wallet_label` in "
        "job.yaml (the engine default 'main' rarely exists on this instance) — it is "
        "NOT a job-root key, an env var, or an adapter config file, so never hunt for "
        "one. Compare the trigger event's timestamp against job.yaml's updated_at "
        "BEFORE investigating: a wake often fires on an error the current config "
        "already fixed.\n"
        "- A `verdict_matured` trigger (or a fresh non-pending verdict in "
        "`evolution.promotion_reliability`) is a RESEARCH EVENT: the forward "
        f"window has judged the last promotion. {verdict_matured_rule}\n"
        "- Infrastructure failures are BOX conditions, not research "
        "verdicts: any claim that a lane is OOM-blocked, locked, or "
        "timed-out must cite the `compute_status` block from THIS wake — "
        "stale infrastructure beliefs carried from memory, agendas, or "
        "prior reports are VOID when compute_status contradicts them. "
        "Completed background operations must be harvested and acted on "
        "regardless of this wake's island assignment.\n"
        "- TOOL CALLS ARE MCP-FIRST: API/research reads (research_*, "
        "hyperliquid_*, onchain_*, core_web_search, delta lab, funding "
        "history, prices) MUST use this session's resident MCP tools — "
        "NEVER `wayfinder <tool-name>` CLI subprocesses, which cold-import "
        "the whole SDK (~90s CPU and 150-250MB per call under load; the "
        "box's OOM cascade was mostly these). The `wayfinder job ...` CLI "
        "remains correct ONLY for job machinery and detached heavy ops "
        "(backtests, experiments, scans) — wrap those in `timeout 300` "
        "minimum and run them one at a time. An `exit 124` from a LOCAL "
        "CLI command is LOCAL CPU STARVATION, never evidence a backend is "
        "down: verify backend health ONLY via a resident MCP tool result, "
        "and void any agenda/memory claim of 'backend DOWN' that is not "
        "backed by an MCP-tool failure observed THIS wake.\n"
        f"{progress_rule}"
        "- Self-rejections are development evidence, never verdicts: a "
        "proposal YOU rejected cannot mark a lane settled in the agenda/"
        "dead map, and reopening bars ('requires named new evidence') may "
        "only be set by owner-accepted or coverage-audit-passed exhaustion "
        "claims, or executed-experiment results. Any lane marked settled on agent-self-"
        "rejected provenance is OPEN.\n"
        "- Routine research wakes carry a `search_assignment` (dynamic "
        "context): one island among exploit / adjacent / divergent / "
        "diversification / falsifier / historian, rotated deterministically "
        "by the improver's allocation weights. The island's directive says "
        "WHAT KIND of search this wake advances — follow it. Maintain the "
        "island's persistent agenda (`research/islands/<island>.md`): read "
        "it first, append what you did and learned. If ops consume the wake "
        "(halt, errors), say so in the report — the rotation still counts "
        "this island as served.\n"
        "- Heavy quant work (signal scans, grids, walk-forwards, bulk "
        "analytics) SHOULD be delegated to the `wayfinder-quant` subagent "
        "with a bounded brief; you stay the orchestrator — synthesize its "
        "results, decide, and keep this session light.\n"
        "- The improver is a VERSIONED artifact: the stable spec's `improver` "
        "block is the active search policy (staleness thresholds, tier "
        "numbers, probation caps, island weights) and every artifact you "
        "produce is stamped with `improver.revision`. To change search "
        "policy, file a `kind='improver_change'` proposal carrying the full "
        "proposed spec via `improver={...}` — owner approval applies it. "
        "NEVER edit improver.yaml directly.\n"
        "- Always write/return a compact structured finding.\n\n"
        "Stable job spec:\n"
        f"{_canonical_json(stable_payload, max_chars=12000)}\n\n"
        "Stable research execution contract (already loaded; do not reload the "
        "strategy skill on each wake):\n"
        f"{research_contract}\n\n"
        "Research prior library (idea families -> priors -> archetypes -> "
        "test paths — pick treatments from here):\n"
        f"{research_priors}\n\n"
        "Durable job memory:\n"
        f"{memory_md}\n\n"
        f"{STABLE_PREFIX_END_MARKER}\n"
    )
    task_line = (
        f"- Apply approved proposal `{apply_proposal_id}`. Check "
        "`proposal_queue`/proposal application status first: if it is queued, "
        "claim it yourself with "
        '`core_jobs(action="claim_application", job_id=..., proposal_id=...)`; '
        "if it is already applying, do not claim again. Claiming pauses the "
        "runner loops and starts a watchdog clock: complete the application "
        "(applied or failed) within ~60 minutes or the SDK will fail it and "
        "resume the loops without you. Apply edits in the candidate "
        "workspace recorded on the proposal application, not the active workspace. "
        'Proposals created via `core_jobs(action="propose")` stage their change '
        "in the candidate at propose time and the claim REUSES that candidate — "
        "verify the change is already present before re-deriving it from the "
        "proposal text, and never recreate the candidate from scratch. "
        "If the current script entrypoint lives outside the candidate workspace, "
        "copy the active script into the candidate workspace and update the "
        "candidate `job.yaml` so promotion will use the copied script. "
        "The SDK will promote only after deterministic validation succeeds. "
        "For jobs_v1 jobs the strategy module exposes ONLY "
        "`build_strategy(params)` / `decide(ctx)` — the SDK driver owns the "
        "live loop, data fetch, order routing, and telemetry; never write a "
        "trading `main()`. Include bars-based scenario fixtures "
        "(`scenario_plan` entries with `bars` + `expect`) that prove the "
        "approved intent contract, and attach a `candidate_report` to the "
        "proposal carrying the candidate revision, backtest stats, and gate "
        "result so approval is never blind. (Legacy jobs cannot pass this "
        "flow; they must be migrated with `wayfinder job migrate-contract`.) "
        "Run validation on the claimed "
        "candidate before completion, and rerun it after material candidate edits: "
        '`core_jobs(action="validate_application", job_id=..., proposal_id=...)` '
        "or `poetry run wayfinder job validate-application <job_id> <proposal_id>`. "
        "If validation fails, read the failed checks, patch the same candidate, "
        "and rerun validation inside this same apply wake. Do not complete a "
        "candidate as applied until validation passes. Include validation attempts "
        "in the apply report when checks fail before the final pass. In one final local step write "
        "`reports/apply/latest.json` and call "
        '`core_jobs(action="complete_application", ...)` with applied or failed. '
        "If MCP job tools are unavailable, use the CLI fallback shape "
        "`poetry run wayfinder job complete-application <job_id> <proposal_id> "
        "--status applied --changed-file <relative-job-file> "
        '--validation-json \'{"py_compile":"passed","smoke_run":"passed"}\'`. '
        "Use normal local development tools to apply the change inside the job "
        "bundle: edit/write, shell, Python/YAML helpers, syntax checks, and tests "
        "are allowed. Keep durable candidate changes under the proposal's candidate "
        "directory unless the task explicitly says otherwise. Keep validation "
        "bounded and fit for the patch: syntax/import, smoke, scenario checks, "
        "telemetry preservation, no duplicate async order behavior when relevant, "
        "and no in-progress candle/lookahead behavior for bar-driven strategies. "
        "After the first sufficient validation pass, complete the application "
        "immediately instead of running open-ended exploratory tests. If validation "
        "fails, complete the application as failed so runner loops resume cleanly.\n"
        if apply_proposal_id
        else (
            "- EVOLUTION SENSOR CONTRACT: inspect structured forward/live "
            "results, attribution, counterfactuals, gate state, and completed "
            "experiments. Produce one bounded deterministic diagnosis, "
            "falsification, or adjudication and record it in an existing "
            "archive/verdict/evidence artifact. Ordinary strategy/parameter "
            "candidates, signal-family authoring, and paper probation belong "
            "to the evolution campaign. The only proposals allowed here are "
            "concrete risk reduction or revert/kill, operational remediation, "
            "exact behavior-equivalence maintenance, and owner-requested "
            "re-stage work. A reproducible executable hypothesis may be checked "
            "in only through `core_jobs(action='evolution_submit_seed', "
            "job_id=..., candidate_dir=..., family=..., hypothesis=..., "
            "base_revision=..., evidence_refs=[...])`; it receives no inherited "
            "evidence and must pass the one evolution funnel. Forward results "
            "adjudicate changes; never fit to "
            "the forward stream.\n"
            "- Regime alarm triage: a standing_checks."
            "portfolio_regime_health warning/critical OVERRIDES ordinary "
            "sensor work: stop routine diagnosis, read the detector's named "
            "signals, then cite the automatically refreshed `attribution` "
            "block before choosing whether to revert, de-risk, gate a regime, "
            "or re-validate. For a symbol-specific break, immediately call "
            "`core_jobs(action='risk_block_symbol', job_id=..., symbol=..., "
            "reason=..., evidence_refs=[...], wake_id=<wake.id>)`: it can only "
            "block new entries, "
            "leaves reduce-only exits available, permits one new symbol per "
            "wake, and only the owner can re-arm. Never explain away a critical "
            "drawdown because "
            "one entry signal still backtests.\n"
            "- If an allowed propose returns a failed validation, read ALL "
            "failed check names and fix them in ONE follow-up propose; after "
            "2 failed propose attempts in a wake, stop and report the blocker "
            "instead of retrying.\n"
            "- A pending proposal whose candidate_report failed with "
            "failure_kind 'infrastructure' is a box condition, not evidence: "
            "run `wayfinder job revalidate <job_id> <proposal_id>` on the "
            "same candidate instead of rejecting or re-proposing.\n"
        )
        if evolution_enabled
        else (
            "- Review the dynamic context against the stable job contract. "
            "When you want to RECOMMEND a strategy/params change, do not "
            'hand-write proposal JSON: call `core_jobs(action="propose", '
            "job_id=..., kind=..., summary=..., intent_contract={...}, "
            "execution_params={...} | candidate_dir=...)` — it stages a "
            "validated candidate, runs the baseline-vs-candidate backtest "
            "comparison, and attaches the candidate_report approvals "
            "require. For an implementation-only Python refactor whose trading "
            "behavior must remain identical, pass "
            "acceptance_policy='behavior_equivalence'. That lane is limited to "
            "workspace/src Python changes, mechanically compares full-history "
            "execution outputs on identical dataset bytes, and auto-applies only "
            "an exact match; a mismatch creates no owner task. Never use it for "
            "signal, parameter, risk, config, entry, or exit changes. "
            "Behavior-preserving changes (perf refactors, logging, cleanup) "
            "MUST be staged as behavior-equivalence proposals — they "
            "auto-apply once the equivalence proof certifies; the economic "
            "gate is for edge claims only.\n"
            "- Wake priority ladder: (1) operational failures (script errors, "
            "reconcile mismatches, halts — INCLUDING a live job that cannot act "
            "for days despite full lookback data, e.g. warmup gated on "
            "strategy_state tick counters instead of ctx.bar_index/"
            "ctx.every_n_bars: propose the data-derived fix); (2) live-gate blockers — read "
            "`gate.reasons` and the FAILING validation check names and fix "
            "exactly those; (3) evidence-based strategy iteration from forward "
            "fills/PnL vs backtest expectation. If the gate is green and forward "
            "evidence shows no problem, report 'healthy, no change warranted' — "
            "do not invent plumbing churn.\n"
            "- Exploration vs exploitation: the forward-sample floor "
            "(drift_policy.min_forward_trades) gates EXPLOITATION only — never "
            "retune the ACTIVE strategy off a forward sample below it. It does "
            "NOT gate EXPLORATION: on a healthy intervene/auto wake while the "
            "forward sample is still below the floor, spend the wake on "
            "research-side analysis instead of going idle — signal-scan/"
            "signal-check other symbols, timeframes, or trigger families, "
            "holdout-check FROZEN scan candidates, or run experiments grids in "
            "research space — and record conclusions in the candidates ledger. "
            "When the canonical library exhausts, COMPOSE: author "
            "hypothesis-driven SignalDefs in workspace/src/signals.py (cap "
            "12; each must cite a fingerprint quadrant, path_stats shape, or "
            "failure-table row) and rerun signal-scan so composed trials "
            "ride the pooled BH family — NEVER a serial one-off signal-check "
            "mining loop (that is p-hacking). rank-check screens continuous "
            "features; sanctioned external axes are funding "
            "(job fetch-funding), session/time-of-day (canonical session "
            "triggers), and cross-asset via the multi-symbol view. "
            "Exploration writes results/research artifacts and ledger entries "
            "only: no workspace/ or job.yaml edits, and no proposals whose "
            "evidence is the sub-floor forward sample. Monitor-mode wakes stay "
            "read-only.\n"
            "- Exit-quality lane: `trade_forensics` in the dynamic context "
            "shows each closed trade's PATH — hold MAE/MFE, post-exit "
            "favorable excursion (what a later exit would have captured), "
            "stop-survival counterfactuals — plus the backtest-population "
            "aggregate by exit reason. When forward trades show a pattern "
            "(e.g. stop-outs where price then runs far in the trade's favor, "
            "or time-exits leaving large post-exit moves), treat it as a "
            "HYPOTHESIS about the exit params, then adjudicate on the "
            "population: run an experiments grid over the pre-registered exit "
            "params (e.g. hold_scale, stop_pct) with walk-forward, require "
            "the improvement to hold in OOS folds and across neighbor cells "
            "(plateau), and only then propose. A handful of forward "
            "anecdotes NEVER justifies a retune directly — but a "
            "stop-loss treatment must also include a stress cell with "
            "execution_params.stop_market_slippage_bps=1000 (Hyperliquid's "
            "native trigger-market tolerance envelope); the simulator applies "
            "that haircut only to stop fills and already prices bar gaps at open. "
            "grid+WF-validated exit change motivated by them is a legitimate "
            "proposal even below the forward-sample floor, because its "
            "evidence is the backtest population, not the forward sample.\n"
            "- Post-apply shadow lane: `post_apply_shadow` in the dynamic "
            "context is a MECHANICAL A/B — the pre-apply strategy (rollback "
            "backup) replayed over the forward bars since apply, diffed "
            "against the actual book. Read it on every wake while a change "
            "is live; never hand-recompute counterfactuals. If the shadow "
            "outperforms the active book by a meaningful sustained margin "
            "(>=14 days of divergence, or entries_skipped_by_change whose "
            "shadow outcomes are clearly positive), that is first-class "
            "evidence for a revert/adjust proposal — cite the block. If the "
            "active book leads, record that in the decisions ledger as the "
            "change's forward validation. This is how entry-gating changes "
            "(filters) are adjudicated: their cost never prints in the live "
            "book, only here.\n"
            "- Universe lane: the symbol set is NOT fixed. When "
            "exploitation lanes are exhausted or a symbol has accumulated "
            "definitive negative evidence, run `wayfinder job universe-scan "
            "<job_id>` (venue-wide screen with YOUR signal library + regime "
            "conditioning, one pooled BH family) and propose the swap in ONE "
            "proposal: remove the dead symbol citing its evidence, admit "
            "the candidate at probation sizing with pre-registered "
            "graduate/kill criteria registered in the probation registry. "
            "Screen results are shortlist evidence ONLY — the admitted "
            "symbol earns full size via its own on-job scans and forward "
            "probation. When sample rate is the binding constraint, this "
            "lane is the fix.\n"
            "- EVIDENCE TIERS: verdict 'promote' (unchanged full gates) -> "
            "full-size leg. Verdict 'probation' (near-miss alive NOW, "
            "regime-conditional edge in the CURRENT regime, or a declared "
            "recent-window survivor) -> deployable at <=50% leg size via a "
            "proposal that PRE-REGISTERS a graduate criterion (N forward "
            "trades in band -> propose full size) and a kill criterion "
            "(auto-disable gate param) — paper forward IS the holdout, that "
            "is why this tier exists. Max 2 concurrent probation legs. "
            "Regime-conditional legs gate live via enabled_regimes and a "
            "regime FLIP is a first-class kill trigger. recency_trend="
            "'decaying' on a deployed leg's signal is a diagnosis; "
            "'strengthening' near-misses are prime probation material. "
            "Graduation uses FORWARD trades only — never re-scan history "
            "for a better story. BOOKKEEPING: probation legs live in "
            "probation.json (synced to the owner's UI) — on a probation "
            "apply, register the leg via wayfinder_paths.jobs.probation."
            "record_probation_leg; update graduate.progress and kill.status "
            "every wake via update_probation_leg; set status "
            "graduated/killed when a criterion trips. A leg missing from "
            "the registry is a protocol violation.\n"
            "- Regime alarm triage: a standing_checks."
            "portfolio_regime_health warning/critical "
            "OVERRIDES the ordinary search assignment: stop signal mining, "
            "read the detector's named signals, then cite the automatically "
            "refreshed `attribution` block before choosing whether to revert, "
            "de-risk, gate a regime, or re-validate. Never explain away a "
            "critical drawdown because one entry signal still backtests.\n"
            "- Quant loop (diagnose -> design -> ablate -> propose): start "
            "from the `attribution` block — archetype counts name the "
            "dominant failure mode; expectation_deltas name where forward "
            "deviates from the model's own backtest. Every new hypothesis "
            "cites the slice/archetype it treats or is labeled a prior-driven "
            "bet from the Research prior library. Triage by prior x symptom "
            "x cost; each ideation runs a PORTFOLIO (>=1 cheap, >=1 "
            "structural, <=1 moonshot, >=1 family not in the dead map). "
            "New-def sweeps go through `signal-scan --campaign NAME` (your "
            "declared BH family; sub-50-cell campaigns are pooled with the "
            "canonical library and every scan includes the incumbent control "
            "cells; declare the campaign in the agenda first). "
            "`controller.incumbent_signal_controls` is the live strategy's "
            "list of symbol/signal/timeframe/horizon yardsticks — update it "
            "whenever the incumbent entry rules change. Multi-intervention "
            "treatments: 2-4 "
            "pre-registered factors, two-stage factorial (screen "
            "--workers 1 --quick 10000, then full-history+WF on the winner "
            "and its one-factor neighbors), and the proposal must cite "
            "factor_attribution. Every proposal carries a pre-mortem (its "
            "expected new failure mode) and a pre-registered kill/re-arm "
            "threshold. Dead-map scope: dead = the tested claim, never the "
            "asset or family. Cross-symbol rank-check and derive-features "
            "are RESEARCH — never gated by the forward-trade floor.\n"
            "- Chart lenses (LOOK before hypothesizing): "
            '`core_jobs(action="chart", job_id=..., symbol=..., '
            'timeframe="30m", indicators=["ema:9","ema:50","rsi:14"], '
            'around_trade="last")` renders the bars around any trade (or the '
            "latest window) as per-bar rows with whatever indicator columns "
            "you request, forward entries/exits annotated, plus a regime "
            'header. `core_jobs(action="analogs", job_id=..., symbol=..., '
            "window=24, horizon=12)` finds the nearest historical analogs of "
            "the recent window and reports what followed them. Use chart to "
            "eyeball every loser before proposing anything about exits; use "
            "analogs when a pattern 'looks familiar'. Both are OBSERVATION "
            "tools — no multiplicity control — so what they suggest becomes a "
            "workspace SignalDef or grid hypothesis for the scan/holdout "
            "gate, never direct evidence.\n"
            "- Ideation cadence: ideation is a FORCED session. Roughly daily "
            "the harness marks a wake as an IDEATION SESSION via a block "
            "above the snapshot — when present, execute it exactly: named "
            "external research tools, ranked hypotheses, and the "
            "research/ideation/latest.json artifact (mechanically checked; "
            "buckets: testable / starved / refuted). Do NOT run ideation on "
            "other wakes. research/agenda.md remains the cumulative research "
            "state — sections: Dead map (refuted, one line of evidence "
            "each), Open hypotheses (ranked, each citing its evidence), "
            "Starved (insufficient data, each with an explicit unlock "
            "condition), and a Last-ideation timestamp. Reopening a refuted "
            "hypothesis requires NAMED new evidence. Keep the agenda compact "
            "(~150 lines): it is a curated map, not a log — compact it in "
            "place as part of updating it.\n"
            "- Probation legs carry TYPED graduate/kill rules "
            "(graduate_rules/kill_rules predicate dicts, e.g. "
            "win_rate__lt: 0.2 with min_closed_trades: 10). The lifecycle "
            "controller evaluates them mechanically every ~6h and flips leg "
            "status itself — register honest rules at leg creation; do not "
            "hand-adjudicate outcomes the controller owns.\n"
            "- Economic promotion gate: full-size promotion requires the "
            "candidate to beat the incumbent on paired OOS folds under the "
            "owner constitution (LCB > 0); it is computed by gate code and "
            "you can NEVER claim or negotiate economic_ready yourself. "
            "Weak-but-positive evidence: set proposed_change.probation=true "
            "for a reduced-size canary leg (clears on point estimate). "
            "Forward results ADJUDICATE changes — never fit parameters to "
            "the forward stream.\n"
            "- If propose returns a failed validation, read ALL failed check "
            "names and fix them in ONE follow-up propose; after 2 failed propose "
            "attempts in a wake, stop and report the blocker instead of "
            "retrying.\n"
            "- A pending proposal whose candidate_report failed with "
            "failure_kind 'infrastructure' is a box condition, not evidence: "
            "run `wayfinder job revalidate <job_id> <proposal_id>` to re-run "
            "validation on the same candidate instead of rejecting or "
            "re-proposing.\n"
        )
    )
    # Re-stage tasks are rendered as prompt text, never only inside the JSON
    # snapshot: the snapshot is truncated at 12k chars with sort_keys=True, so
    # a busy job can silently swallow a payload-only instruction — which is
    # exactly how an agent once missed a pending re-stage and burned the
    # owner's carried-over approval on a duplicate proposal.
    # Gate state renders as prompt text too: a red gate buried in the
    # truncated snapshot JSON let a wake report "gate green" while approvals
    # were actually blocked for 28 hours (2026-08-12).
    gate_alert = ""
    gate_state = snapshot.get("gate") or {}
    if gate_state and gate_state.get("live_ready") is False:
        gate_reasons = "; ".join(str(r) for r in (gate_state.get("reasons") or [])[:4])
        gate_alert = (
            "GATE STATUS: RED — approvals and go-live are blocked.\n"
            f"Reasons: {gate_reasons}\n"
            "If every reason is a revision mismatch (stamps older than the "
            "workspace), the watchdog re-stamps automatically — do not treat "
            "it as a strategy failure, but DO NOT report the gate as green. "
            "For any other reason, fixing the gate is a priority this wake.\n\n"
        )
    # Bootstrap contract: a never-operational job past half its deadline gets
    # the bootstrap-first directive on every wake until it bootstraps or is
    # parked. Rendered as prompt text — never only snapshot JSON.
    bootstrap_alert = (
        bootstrap_directive(store, job_id) if apply_proposal_id is None else ""
    )
    remediation_directive = ""
    if remediation_quiet and apply_proposal_id is None:
        remediation_directive = REMEDIATION_QUIET_LINE
    elif remediation_overrides and apply_proposal_id is None:
        remediation_directive = (
            "REGIME REMEDIATION REQUIRED — this durable case remains open:\n"
            f"{_canonical_json(remediation_case, max_chars=3000)}\n"
            "It OVERRIDES routine search and cannot end with no_change, "
            "owner_terminal_decision, or an explanation that the technical "
            "gate is green. The owner policy is continue-trading while review "
            "is pending: do not pause, flatten, or silently clamp leverage. "
            "Start with attribution and the cheapest causal treatment: ablate "
            "or disable the largest losing symbol/leg before mining a replacement.\n"
            "This wake must produce exactly one accountable outcome: (1) a "
            "candidate-backed green proposal linked to this case; (2) a bounded "
            "evaluation artifact plus core_jobs(action='remediation_progress', "
            "remediation_state='evaluating', remediation_note=..., "
            "artifact_path=...); or (3) a structured blocker recorded with "
            "remediation_state='blocked'. Failed/red candidate attempts do not "
            "close the case; record the blocker and the scheduler will retry.\n\n"
        )
    # Impasse directive: written by the watchdog when a research-stale job's
    # last K wakes produced zero progress artifacts, cleared only when one
    # appears. Rendered as prompt text (never only snapshot JSON) with the
    # escape hatch stripped — the hatch is how three production jobs closed
    # every stale wake with "stated-not-advanced" prose for weeks.
    impasse_directive = ""
    impasse_marker = store.read_json(job_id, "state/research_impasse.json") or {}
    if impasse_marker.get("alerted_at"):
        if evolution_enabled:
            mandate = impasse_marker.get("mandate") or {}
            required = mandate.get("required_next_experiments") or []
            impasse_directive = (
                "RESEARCH IMPASSE — evolution owns candidate generation. This "
                "sensor wake must execute one bounded deterministic diagnostic "
                "or falsification and write its result to an existing archive/"
                "verdict/evidence artifact. Do not open probation or build a "
                "parallel candidate.\n"
                f"REQUIRED_DIAGNOSTICS={json.dumps(required, default=str)}\n\n"
            )
        elif impasse_marker.get("status") == "mandated_work":
            mandate = impasse_marker.get("mandate") or {}
            required = mandate.get("required_next_experiments") or []
            impasse_directive = (
                "RESEARCH IMPASSE — a mechanical coverage audit REJECTED "
                f"claim {mandate.get('claim_id')}. This wake MUST execute or "
                "start one of the named required experiments below; filing "
                "another exhaustion claim does not satisfy this mandate.\n"
                f"REQUIRED_NEXT_EXPERIMENTS={json.dumps(required, default=str)}\n"
                "Only a semantic-hash-unique experiment or a probation "
                "graduate/kill outcome clears the impasse.\n\n"
            )
        else:
            impasse_directive = (
                "RESEARCH IMPASSE — the watchdog flagged `research_impasse`: "
                f"the last {impasse_marker.get('stale_wakes')} wakes staged zero "
                "experiments, probation legs, staged proposals, or exhaustion "
                "claims while research is stale.\n"
                "This wake MUST end in exactly one of: (a) a staged+executed "
                "experiment, (b) a new probation leg (paper entry tier "
                "qualifies), (c) an exhaustion claim FILED for mechanical "
                "coverage audit. No other outcome closes an impasse wake.\n"
                "It must also make a DIVERSITY move — resurrect an archived/"
                "historical candidate or recombine signals across lanes. "
                "Another audit of the incumbent does not count.\n\n"
            )
    # Ideation is a FORCED session, not a prose suggestion: 130 consecutive
    # "nothing new, agenda stands" wakes proved the agent never consults an
    # external tool unless the wake's task IS the expedition. Stamp-gated on
    # the artifact itself (~daily); skipped while ops need attention (red
    # gate, pending re-stage, apply wake) so it lands on the next clean wake.
    ideation_directive = ""
    ideation_task_line = ""
    ideation_age = _ideation_age_s(root)
    ideation_due = ideation_age is None or ideation_age > _ideation_thresholds(root)[0]
    if (
        ideation_due
        and mode == "intervene"
        and apply_proposal_id is None
        and not restage_tasks
        and not (gate_state and gate_state.get("live_ready") is False)
    ):
        age_desc = (
            "missing (no expedition has ever produced one)"
            if ideation_age is None
            else f"{ideation_age / 3600:.0f}h old"
        )
        ideation_directive = (
            "IDEATION SESSION — this wake is a research EXPEDITION, not a "
            "routine review.\n"
            f"The external-research artifact ({_IDEATION_PATH}) is {age_desc}; "
            "the contract is one expedition per day.\n"
            "Give routine ops ONE glance (act only if something is red), then:\n"
            "1. Consult at least 3 DISTINCT external sources via named research "
            "tool calls — research_search_alpha, research_crypto_sentiment, "
            "research_social_x_search, research_search_perp, web search/fetch. "
            "Re-reading the agenda, memory, or internal scan results does NOT "
            "count as a source.\n"
            "2. Look OUTWARD at the next 1-2 weeks for YOUR symbols and their "
            "sector: scheduled events, token unlocks, listings, macro prints, "
            "funding-regime shifts, narrative rotation.\n"
            "3. Write research/ideation/latest.json with exactly this shape: "
            '{"generated_at": "<UTC ISO8601>", "sources_consulted": [{"tool": '
            '..., "query": ..., "takeaway": ...}], "hypotheses": [{"title": '
            '..., "thesis": ..., "bucket": "testable"|"starved"|"refuted", '
            '"next_step": ...}]} — hypotheses ranked best-first, at least 3, '
            "every one bucketed honestly:\n"
            "   - testable: next_step names the exact scan/SignalDef/feature "
            "run to do NOW with data you already have.\n"
            "   - starved: next_step names the unlock condition — the data or "
            "feature that must exist first (e.g. an events/catalyst feed that "
            "is not built yet). Naming missing data is a VALID and valuable "
            "outcome, not a failure.\n"
            "   - refuted: next_step names the disqualifying evidence.\n"
            "4. Fold the artifact into research/agenda.md (curated + compact, "
            "as usual).\n"
            "A 'nothing actionable' expedition is valid ONLY if "
            "sources_consulted proves you looked. The artifact is checked "
            "mechanically each wake; a missing or stale artifact is journaled "
            "as ideation_incomplete for the owner to see.\n\n"
        )
        ideation_task_line = (
            "- This wake is an IDEATION SESSION: execute the IDEATION SESSION "
            "block above the snapshot, including writing "
            "research/ideation/latest.json.\n"
        )
    restage_priority = ""
    restage_task_line = ""
    if restage_tasks:
        restage_task_line = (
            "- FIRST: complete the PRIORITY re-stage tasks listed above the snapshot.\n"
        )
        items = "".join(
            f"  - {task['proposal_id']}: {task['instruction']}\n"
            for task in restage_tasks
        )
        restage_priority = (
            "PRIORITY — approved changes awaiting re-stage (do this FIRST):\n"
            "The owner already approved these; an intervening apply moved the "
            "workspace, so each candidate must be rebuilt against the CURRENT "
            "workspace and re-staged.\n"
            f"{items}"
            "Use `wayfinder job restage` exactly as instructed per task. Do "
            "NOT create a new proposal for these and do NOT use propose — a "
            "new proposal discards the owner's approval and forces a "
            "duplicate review. Re-staging re-runs every gate and re-queues "
            "the apply automatically; if the change no longer makes sense on "
            "the new base, reject it (agent housekeeping) and only then "
            "propose fresh.\n\n"
        )
    report_outcome_directive = ""
    if apply_proposal_id is None:
        report_outcome_directive = (
            "- REPORT OUTCOME: include `outcome` as exactly one of "
            "no_change | deferred | experiment_completed | candidate_proposed | "
            "remediation_advanced | blocked, plus `material_change` as a boolean. "
            "For a scheduled wake with no material delta, use no_change and update "
            "only the compact latest report; do not append candidate/decision "
            "ledgers, memory, the research agenda/island agenda, or remediation "
            "reaffirmations.\n"
        )
    apply_proposal: dict[str, Any] = next(
        (
            proposal
            for proposal in snapshot.get("proposals") or []
            if str(proposal.get("proposal_id") or proposal.get("id") or "")
            == str(apply_proposal_id or "")
        ),
        {},
    )
    maintenance_ready = bool(
        ((apply_proposal.get("candidate_report") or {}).get("maintenance") or {}).get(
            "ready"
        )
        is True
    )
    work_order = _worker_work_order(
        job_id=job_id,
        mode=mode,
        apply_proposal_id=apply_proposal_id,
        maintenance_ready=maintenance_ready,
        remediation_overrides=bool(remediation_overrides),
        restage_tasks=list(restage_tasks or []),
        ideation_due=bool(ideation_due),
        gate_red=bool(gate_state and gate_state.get("live_ready") is False),
    )
    lane = str(work_order["lane"])
    if lane == "remediation":
        prompt_payload = {
            key: dynamic_payload.get(key)
            for key in (
                "scorecard",
                "forward",
                "runner_links",
                "proposals",
                "proposal_queue",
                "gate",
                "trade_forensics",
                "attribution",
                "standing_checks",
                "compute_status",
                "wake",
                "portfolio",
            )
            if dynamic_payload.get(key) not in (None, {}, [])
        }
    elif lane in {"maintenance", "application"}:
        prompt_payload = {
            key: dynamic_payload.get(key)
            for key in (
                "scorecard",
                "runner_links",
                "proposals",
                "proposal_queue",
                "reports",
                "gate",
                "standing_checks",
                "compute_status",
                "restage_tasks",
                "wake",
            )
            if dynamic_payload.get(key) not in (None, {}, [])
        }
    else:
        prompt_payload = dynamic_payload
    dynamic_context = (
        f"{DYNAMIC_CONTEXT_MARKER}\n"
        f"{_render_work_order(work_order)}\n\n"
        f"{gate_alert}"
        f"{bootstrap_alert}"
        f"{remediation_directive}"
        f"{restage_priority}"
        f"{impasse_directive}"
        f"{ideation_directive}"
        f"Current wake id (pass verbatim to risk_block_symbol): {wake_id}\n"
        "Current snapshot:\n"
        f"{_canonical_json(prompt_payload, max_chars=12000)}\n\n"
        "Research agenda (research/agenda.md — cumulative exploration "
        "state; maintain it, do not restart it):\n"
        f"{research_agenda or '(none yet — bootstrap it on the next healthy ideation wake)'}\n\n"
        "Recent journal:\n"
        f"{recent_journal}\n\n"
        "Task:\n"
        f"{restage_task_line}"
        f"{ideation_task_line}"
        f"{task_line}"
        "- Write the appropriate monitor/intervene/auto/apply report.\n"
        f"{report_outcome_directive}"
        "- Emit a user-visible result only for meaningful state transitions, "
        "warnings, proposals, or blocked auto decisions.\n"
    )
    return {
        "prompt": stable_prefix + "\n" + dynamic_context,
        "stable_prefix": stable_prefix,
        "dynamic_context": dynamic_context,
        "stable_prefix_hash": hashlib.sha256(stable_prefix.encode()).hexdigest(),
        "dynamic_context_hash": hashlib.sha256(dynamic_context.encode()).hexdigest(),
        "work_order": work_order,
    }


def prepare_job_worker_prompt(
    *,
    store: JobStore,
    job_id: str,
    mode: str,
    apply_proposal_id: str | None = None,
    wake_source: str = "scheduled_timer",
    wake_triggers: list[str] | None = None,
    wake_id: str | None = None,
) -> dict[str, Any]:
    """Prepare the exact prompt payload used for a job worker wakeup.

    Never claims the application: claiming pauses the runner loops, and prompt
    delivery to an LLM session is not guaranteed — a dropped prompt after a
    claim stalls the job (2026-07-22 incident). Deterministic applies claim in
    apply_launcher; agent-owned applies claim from inside the live session.
    """
    job = store.load(job_id)
    mode = normalize_agent_mode(mode) if mode else job.agent_loop.mode
    if mode == "off":
        mode = "monitor"
    mode_typed: AgentMode = mode

    snapshot = snapshot_job(job.id, store=store)

    # Deterministic memory hygiene: on a wake with no forward telemetry, strip
    # unsupported performance claims from durable memory before the agent reads
    # it, so a prior wake's confabulation cannot propagate. No-op otherwise.
    sanitize_job_memory(store, job.id, forward=snapshot.get("forward"))

    # Backtest replication monitor: was the evidence that justified the
    # active revision real? Stamp-gated daily; never raises. Runs before the
    # standing-checks block so a fresh verdict is readable this wake.
    from wayfinder_paths.jobs.replication import replication_job

    replication_job(job.id, store=store)

    # Research-side derived features (cross/exog/venue/regime) refresh here
    # because THIS wake's scans are their consumer — a one-time backfill
    # otherwise goes silently stale (btc_trend froze for 4 days while
    # merging cleanly into every scan frame). Stamp-gated hourly; never
    # raises.
    refresh_derived_features_if_stale(job.id, store=store)

    # Portfolio regime/incumbent health consumes the fresh compact market
    # artifact above plus forward PnL. On alert it refreshes attribution before
    # this wake is prompted, and applies only an owner-governed response.
    try:
        from wayfinder_paths.jobs.regime_health import regime_health_job

        regime_health_job(job.id, store=store)
    except Exception as exc:  # noqa: BLE001 — monitor cannot kill a wake
        store.append_journal(
            job.id, {"type": "regime_health_failed", "error": str(exc)[:300]}
        )

    # Ideation accountability: journal freshly produced expedition artifacts
    # (owner-visible bucket counts) and escalate once when the daily research
    # expedition is >48h overdue. Never raises.
    _ideation_bookkeeping(store, job.id)

    prompt_sections = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode=mode_typed,
        snapshot=snapshot,
        apply_proposal_id=apply_proposal_id,
        wake_source=wake_source,
        wake_triggers=wake_triggers,
        wake_id=wake_id or f"wake-{uuid.uuid4().hex}",
    )
    return {
        **prompt_sections,
        "job_id": job.id,
        "mode": mode_typed,
    }


def _emit_job_result(
    summary: str,
    job_id: str,
    *,
    proposal_id: str | None = None,
    severity: str = "warning",
) -> None:
    payload: dict[str, Any] = {
        "type": "job_result",
        "severity": severity,
        "summary": summary,
        "job_id": job_id,
    }
    if proposal_id:
        # Contract C5: the FE renders a "Review proposal" deep-link chip when
        # both job_id and proposal_id are present.
        payload["proposal_id"] = proposal_id
    print(JOB_RESULT_MARKER + json.dumps(payload))


def run_job_worker(
    job_id: str,
    mode: str = "monitor",
    *,
    apply_proposal_id: str | None = None,
    wake_source: str = "scheduled_timer",
    wake_triggers: list[str] | None = None,
    force_llm: bool = False,
) -> dict[str, Any]:
    store = JobStore()
    job = store.load(job_id)
    mode = normalize_agent_mode(mode) if mode else job.agent_loop.mode
    if mode == "off":
        mode = "monitor"
    mode_typed: AgentMode = mode
    if apply_proposal_id is not None and wake_source == "scheduled_timer":
        wake_source = "proposal_apply"
    wake_context: dict[str, Any] = {
        "wake_source": wake_source,
        "wake_triggers": sorted(set(wake_triggers or [])),
    }

    blocked_reason = (
        _auto_limits_error(job.agent_loop.auto_limits) if mode_typed == "auto" else None
    )
    if blocked_reason:
        report = _write_report(
            store=store,
            job_id=job.id,
            mode=mode_typed,
            status="red",
            summary=f"Auto agent blocked: {blocked_reason}",
            session_id=None,
            queued=False,
            error=blocked_reason,
            wake_context=wake_context,
        )
        _emit_job_result(report["summary"], job.id)
        return report

    # Open-ended evolution has its own long-lived session. It is intentionally
    # kept separate from the hourly funnel so a four-hour campaign can keep
    # coherent context without running two LLM sessions against one CPU pool.
    evolution_wake: dict[str, Any] | None = None
    if mode_typed == "intervene" and apply_proposal_id is None:
        from wayfinder_paths.jobs.evolution_campaign import campaign_status

        campaign = campaign_status(store, job.id)
        evolution_status = campaign.get("status")
        risk_preempt = bool(
            set(wake_triggers or [])
            & {"risk_halt", "regime_shift", "regime_remediation_due"}
        )
        if risk_preempt and evolution_status in {"active", "finalizing"}:
            retire_evolution_session(
                store, job.id, str(campaign.get("campaign_id") or "")
            )
            store.append_journal(
                job.id,
                {
                    "type": "evolution_paused_for_risk",
                    "campaign_id": campaign.get("campaign_id"),
                    "triggers": sorted(set(wake_triggers or [])),
                },
            )
        if not risk_preempt:
            evolution_wake = _queue_evolution_worker(store, job.id)
        evolution_claimed_lane = bool(
            evolution_wake is not None and not evolution_wake.get("error")
        )
        if not risk_preempt and (
            evolution_claimed_lane or evolution_status in {"active", "finalizing"}
        ):
            session_id = str((evolution_wake or {}).get("session_id") or "") or None
            reason = str((evolution_wake or {}).get("reason") or "").strip()
            return _write_report(
                store=store,
                job_id=job.id,
                mode=mode_typed,
                status="green",
                summary=(
                    f"Intervention LLM wake deferred to evolution: {reason}"
                    if reason
                    else "Intervention LLM wake deferred while evolution owns the lane"
                ),
                session_id=session_id,
                queued=False,
                error=None,
                wake_context=wake_context,
            )

    # Wake economy cheap-skip (same tier as the auto-limits guard above): a
    # saturated paper job whose evidence watermark has not moved since the
    # last full wake skips the LLM session entirely.
    quiet_report = maybe_skip_wake(
        store,
        job,
        mode=mode_typed,
        apply_proposal_id=apply_proposal_id,
        force=force_llm,
        wake_source=wake_source,
        wake_triggers=wake_triggers,
    )
    if quiet_report is not None:
        return quiet_report

    session_id = _ensure_worker_session(job.id, mode_typed)
    queued = False
    error: str | None = None
    prompt_sections = prepare_job_worker_prompt(
        store=store,
        job_id=job.id,
        mode=mode_typed,
        apply_proposal_id=apply_proposal_id,
        wake_source=wake_source,
        wake_triggers=wake_triggers,
    )
    prompt = prompt_sections["prompt"]

    if session_id:
        queued = OPENCODE_CLIENT.prompt_async(
            session_id=session_id,
            text=prompt,
            agent=JOB_AUTO_WORKER_AGENT_NAME
            if mode_typed == "auto"
            else JOB_WORKER_AGENT_NAME,
        )
        if not queued:
            # No cleanup needed: the wake never claims, so a dropped prompt
            # leaves the application queued with the runner loops running.
            error = "OpenCode prompt_async failed"
    else:
        error = "OpenCode server unavailable"

    if queued:
        # Anchor the wake-economy skip window on delivered wakes only: a
        # failed queue leaves the state untouched so the retry runs in full.
        record_full_wake(store, job, wake_source=wake_source)
        wake_state = store.read_json(job.id, WAKE_ECONOMY_PATH, default={}) or {}
        wake_context["decision_watermark_hash"] = wake_state.get(
            "decision_watermark_hash"
        )

    report = _write_report(
        store=store,
        job_id=job.id,
        mode=mode_typed,
        status="green" if queued else "yellow",
        summary=(
            f"{mode_typed} wakeup queued in OpenCode session {session_id}"
            + (f" for proposal {apply_proposal_id}" if apply_proposal_id else "")
            if queued
            else "Worker could not queue an OpenCode wakeup"
        ),
        session_id=session_id,
        queued=queued,
        error=error,
        apply_proposal_id=apply_proposal_id,
        cache={
            "prompt_cache_key": session_id,
            "stable_prefix_hash": prompt_sections["stable_prefix_hash"],
            "dynamic_context_hash": prompt_sections["dynamic_context_hash"],
            "metrics": "not_available",
        },
        wake_context=wake_context,
    )

    if report["status"] != "green":
        _emit_job_result(report["summary"], job.id, proposal_id=apply_proposal_id)
    return report


def _ensure_worker_session(job_id: str, mode: str) -> str | None:
    if not OPENCODE_CLIENT.healthy():
        return None
    controller_session_id = os.environ.get("OPENCODE_SESSION_ID") or os.environ.get(
        "OPENCODE_SESSIONID"
    )
    try:
        existing = OPENCODE_CLIENT.find_child_session(
            parent_id=controller_session_id,
            title=f"job/{job_id}/{mode}",
        )
        if existing:
            return existing
        return OPENCODE_CLIENT.create_session(
            parent_id=controller_session_id,
            title=f"job/{job_id}/{mode}",
            agent=JOB_AUTO_WORKER_AGENT_NAME
            if mode == "auto"
            else JOB_WORKER_AGENT_NAME,
        )
    except Exception:
        logger.opt(exception=True).debug(
            "Failed to create/find OpenCode job worker session"
        )
        return None


def job_worker_session_busy(job_id: str, mode: str) -> bool:
    """Whether the ordinary job worker currently owns the shared LLM lane."""
    if not is_opencode_instance() or not OPENCODE_CLIENT.healthy():
        return False
    controller_session_id = os.environ.get("OPENCODE_SESSION_ID") or os.environ.get(
        "OPENCODE_SESSIONID"
    )
    try:
        session_id = OPENCODE_CLIENT.find_child_session(
            parent_id=controller_session_id,
            title=f"job/{job_id}/{mode}",
        )
        return bool(session_id and OPENCODE_CLIENT.session_statuses().get(session_id))
    except Exception:
        logger.opt(exception=True).debug("Failed to inspect OpenCode job worker")
        return False


def _queue_evolution_worker(store: JobStore, job_id: str) -> dict[str, Any] | None:
    from wayfinder_paths.jobs.improver.spec import ImproverSpec

    spec = ImproverSpec.load(store.job_dir(job_id))
    if not spec.evolution_eligibility(store.job_dir(job_id), job_id)["eligible"]:
        return None
    if not OPENCODE_CLIENT.healthy():
        return None
    try:
        from wayfinder_paths.jobs.background import spawn_detached_op
        from wayfinder_paths.jobs.evolution_campaign import (
            campaign_due,
            campaign_prompt_block,
            campaign_status,
        )
        from wayfinder_paths.jobs.resource_envelope import evolution_launch_readiness

        if campaign_due(store, job_id):
            readiness = evolution_launch_readiness()
            if not readiness["ready"]:
                deferred = {
                    "queued": False,
                    "starting": False,
                    "reason": str(readiness.get("reason") or "resource guard"),
                }
                if readiness.get("source") == "governor":
                    # A healthy governor with low credit needs the ordinary
                    # LLM lane quiet so the reserve can actually recover.
                    deferred["deferred"] = True
                else:
                    # A stale/invalid governor must not wedge both lanes.
                    deferred["error"] = deferred["reason"]
                return deferred
            if job_worker_session_busy(job_id, "intervene"):
                return {
                    "queued": False,
                    "starting": False,
                    "deferred": True,
                    "reason": "the intervention worker is already active",
                }
            started = spawn_detached_op(
                store, job_id, "evolution_start", {"job_id": job_id}
            )
            return {"queued": False, "starting": True, **started}
        if campaign_status(store, job_id).get("status") != "active":
            return None
        campaign = campaign_prompt_block(store, job_id)
    except Exception as exc:  # noqa: BLE001 - the hourly funnel remains independent
        return {"queued": False, "error": str(exc)[:300]}
    if not campaign or campaign.get("status") == "blocked":
        return None
    return _prompt_evolution_session(store, job_id, campaign, source="wake")


def nudge_evolution_session(store: JobStore, job_id: str) -> dict[str, Any] | None:
    """Advance the campaign into a fresh stage session after an op lands."""
    if os.environ.get("WAYFINDER_EVOLUTION_NUDGE") == "0":
        return None
    if not OPENCODE_CLIENT.healthy():
        return None
    try:
        from wayfinder_paths.jobs.evolution_campaign import (
            campaign_prompt_block,
            campaign_status,
        )

        if campaign_status(store, job_id).get("status") != "active":
            return None
        campaign = campaign_prompt_block(store, job_id)
    except Exception as exc:  # noqa: BLE001 - a nudge must never fail its op
        return {"queued": False, "error": str(exc)[:300]}
    if not campaign or campaign.get("status") == "blocked":
        return None
    return _prompt_evolution_session(store, job_id, campaign, source="op_completion")


def recover_evolution_stage_session(
    store: JobStore, job_id: str, *, now: dt.datetime
) -> dict[str, Any] | None:
    """Restart a missing or stale idle stage without reviving its old context."""
    if not OPENCODE_CLIENT.healthy():
        return None
    from wayfinder_paths.jobs.evolution_campaign import campaign_prompt_block

    campaign = campaign_prompt_block(store, job_id, now=now)
    if not campaign or campaign.get("status") == "blocked":
        return None
    desired_stage = str(campaign.get("session_stage") or "")
    desired_artifact = str(campaign.get("artifact_key") or desired_stage)
    session = store.read_json(job_id, EVOLUTION_SESSION_PATH, default={}) or {}
    session_id = str(session.get("session_id") or "")
    if session_id and OPENCODE_CLIENT.session_statuses().get(session_id):
        return None
    missing = bool(
        not session_id
        or session.get("retired_at")
        or OPENCODE_CLIENT.session_exists(session_id) is False
    )
    stored_stage = str(session.get("session_stage") or "")
    stored_artifact = str(session.get("artifact_key") or stored_stage)
    artifact_changed = stored_artifact != desired_artifact
    stale = False
    try:
        prompted_at = dt.datetime.fromisoformat(str(session["last_prompt_at"]))
        if prompted_at.tzinfo is None:
            prompted_at = prompted_at.replace(tzinfo=dt.UTC)
        stale = now - prompted_at >= EVOLUTION_STAGE_RETRY_AFTER
    except (KeyError, TypeError, ValueError):
        stale = bool(session_id)
    if not (missing or artifact_changed or stale):
        return None
    if session_id and not session.get("retired_at") and not missing:
        retired = retire_evolution_session(
            store, job_id, str(session.get("campaign_id") or "")
        )
        if not retired or not retired.get("retired"):
            return {
                "queued": False,
                "transition_pending": True,
                "error": (retired or {}).get("error") or "stage retirement failed",
            }
    return _prompt_evolution_session(
        store, job_id, campaign, source="watchdog_recovery"
    )


def build_evolution_stage_prompt(
    job_id: str,
    campaign: dict[str, Any],
    *,
    prior_handoff: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render the production evolution stage prompt and its agent identity.

    The benchmark harness calls this same pure formatter. Keeping prompt
    construction here makes prompt hashes meaningful: an A/B cannot quietly
    compare a simplified benchmark instruction against the live worker.
    """
    campaign_id = str(campaign.get("campaign_id") or "").strip()
    if not campaign_id:
        raise ValueError("evolution campaign id missing")
    session_stage = str(campaign.get("session_stage") or "").strip()
    if not session_stage:
        raise ValueError("evolution session stage missing")
    artifact_key = str(campaign.get("artifact_key") or session_stage).strip()
    campaign_payload = dict(campaign)
    prior_stage_text = (
        _canonical_json(prior_handoff, max_chars=1_500) if prior_handoff else "null"
    )
    candidate_outcomes = list(reversed(campaign_payload.pop("candidate_outcomes", [])))
    outcomes_text = _canonical_json(
        {"order": "newest_first", "candidates": candidate_outcomes[:3]},
        max_chars=3_000,
    )
    lane = (
        "evolution_design"
        if session_stage == "design"
        else "evolution_finalize"
        if session_stage == "finalize"
        else "evolution_repair"
        if campaign_payload.get("postmortem_path")
        else "evolution_candidate"
    )
    action = (
        "submit_campaign_design"
        if session_stage == "design"
        else "launch_finalize"
        if session_stage == "finalize"
        else "repair_and_launch_evaluation"
        if campaign_payload.get("postmortem_path")
        else "prepare_edit_and_launch_evaluation"
    )
    inputs = list(campaign_payload.get("work_inputs") or [])
    for key in ("diagnostic_pack", "manifest_path", "postmortem_path"):
        if campaign_payload.get(key):
            inputs.append(str(campaign_payload[key]))
    editable_paths = list(campaign_payload.get("editable_paths") or [])
    work_order = _work_order(
        lane=lane,
        action=action,
        objective=(
            "Design and submit the campaign's bounded hypothesis/slot allocation."
            if session_stage == "design"
            else "Launch deterministic campaign finalization and end the stage."
            if session_stage == "finalize"
            else "Change the assigned candidate mechanism and launch exactly one detached evaluation."
        ),
        inputs=inputs,
        editable_paths=editable_paths,
        exclusions=[
            "active incumbent workspace",
            "live trading or owner provenance",
            "future or sealed holdout data",
            "another candidate in the same stage",
        ],
        completion=(
            "Call evolution_design once and end."
            if session_stage == "design"
            else "Call evolution_finalize with background=true and end."
            if session_stage == "finalize"
            else "Call evolution_evaluate with background=true and end without waiting."
        ),
    )
    evidence_pointers = list(campaign_payload.get("valid_evidence_pointers") or [])
    if evidence_pointers:
        work_order["valid_evidence_pointers"] = evidence_pointers
    title = f"job/{job_id}/evolution/{campaign_id}/{artifact_key}"
    agent_name = str(campaign_payload.pop("agent_name", "") or "")
    if agent_name not in {
        JOB_EVOLUTION_DESIGNER_AGENT_NAME,
        JOB_EVOLUTION_WORKER_AGENT_NAME,
    }:
        agent_name = JOB_EVOLUTION_WORKER_AGENT_NAME
    control_keys = (
        "campaign_id",
        "stage",
        "session_stage",
        "artifact_key",
        "deadline_at",
        "deadline_elapsed",
        "counts",
        "constraints",
        "forward_context_cutoff",
    )
    control_state = {
        key: campaign_payload[key]
        for key in control_keys
        if campaign_payload.get(key) is not None
    }
    prompt = (
        f"Run PAPER-ONLY evolution stage `{session_stage}`. Use the persisted "
        "candidate outcomes and prior-stage handoff; do not reload the retired "
        "session or its raw tool results.\n\n"
        f"{_render_work_order(work_order)}\n\n"
        "Perform exactly this next action, then end the stage:\n"
        f"{campaign_payload.get('next_action')}\n\n"
        f"Prior stage handoff:\n{prior_stage_text}\n\n"
        f"Persisted candidate outcomes:\n{outcomes_text}\n\n"
        "Campaign control state:\n" + _canonical_json(control_state, max_chars=2_000)
    )
    return {
        "campaign_id": campaign_id,
        "session_stage": session_stage,
        "artifact_key": artifact_key,
        "title": title,
        "agent_name": agent_name,
        "prompt": prompt,
        "work_order": work_order,
    }


def _prompt_evolution_session(
    store: JobStore, job_id: str, campaign: dict[str, Any], *, source: str
) -> dict[str, Any] | None:
    from wayfinder_paths.jobs.compute_lock import job_state_lock

    campaign_id = str(campaign.get("campaign_id") or "").strip()
    if not campaign_id:
        return {"queued": False, "error": "evolution campaign id missing"}
    session_stage = str(campaign.get("session_stage") or "").strip()
    if not session_stage:
        return {"queued": False, "error": "evolution session stage missing"}
    artifact_key = str(campaign.get("artifact_key") or session_stage).strip()
    prior_handoff = _latest_evolution_stage_handoff(store, job_id, campaign_id)
    existing = store.read_json(job_id, EVOLUTION_SESSION_PATH, default={}) or {}
    existing_id = str(existing.get("session_id") or "")
    existing_campaign = str(existing.get("campaign_id") or "")
    existing_stage = str(existing.get("session_stage") or "")
    existing_artifact = str(existing.get("artifact_key") or existing_stage)
    existing_gone = bool(
        existing_id and OPENCODE_CLIENT.session_exists(existing_id) is False
    )
    transition = bool(
        existing_id
        and not existing.get("retired_at")
        and (
            existing_campaign != campaign_id
            or existing_artifact != artifact_key
            or existing_gone
        )
    )
    if transition:
        retired = retire_evolution_session(
            store, job_id, existing_campaign, abort_busy=False
        )
        if not retired or not retired.get("retired"):
            return {
                "queued": False,
                "transition_pending": True,
                "session_id": existing_id,
                "from_stage": existing_stage or None,
                "to_stage": session_stage,
                "error": (retired or {}).get("error")
                or (
                    "previous stage session is still busy"
                    if (retired or {}).get("busy")
                    else "stage session retirement failed"
                ),
            }
        if existing_campaign == campaign_id:
            prior_handoff = retired.get("handoff") or prior_handoff
    rendered = build_evolution_stage_prompt(
        job_id, campaign, prior_handoff=prior_handoff
    )
    title = rendered["title"]
    agent_name = rendered["agent_name"]
    prompt = rendered["prompt"]
    controller_session_id = os.environ.get("OPENCODE_SESSION_ID") or os.environ.get(
        "OPENCODE_SESSIONID"
    )
    fingerprint = hashlib.sha256(prompt.encode()).hexdigest()
    created_at = utc_now_iso()
    try:
        with job_state_lock(store.repo_root, job_id, name="evolution_session"):
            session = store.read_json(job_id, EVOLUTION_SESSION_PATH, default={}) or {}
            session_id = str(session.get("session_id") or "")
            stored_campaign = str(session.get("campaign_id") or "")
            stored_stage = str(session.get("session_stage") or "")
            stored_artifact = str(session.get("artifact_key") or stored_stage)
            reusable = bool(
                session_id
                and stored_campaign == campaign_id
                and stored_artifact == artifact_key
                and not session.get("retired_at")
                and OPENCODE_CLIENT.session_exists(session_id) is not False
            )
            if not reusable:
                session_id = OPENCODE_CLIENT.find_child_session(
                    parent_id=controller_session_id, title=title
                ) or str(
                    OPENCODE_CLIENT.create_session(
                        parent_id=controller_session_id,
                        title=title,
                        agent=agent_name,
                    )
                    or ""
                )
                session = {
                    "schema_version": "1.2",
                    "campaign_id": campaign_id,
                    "session_stage": session_stage,
                    "artifact_key": artifact_key,
                    "session_id": session_id,
                    "created_at": created_at,
                }
            if not session_id:
                return {"queued": False, "error": "OpenCode server unavailable"}
            if OPENCODE_CLIENT.session_statuses().get(session_id):
                return {"queued": False, "session_id": session_id, "busy": True}
            if session.get("last_prompt_fingerprint") == fingerprint:
                return {
                    "queued": False,
                    "session_id": session_id,
                    "deduplicated": True,
                }
            queued = OPENCODE_CLIENT.prompt_async(
                session_id=session_id,
                text=prompt,
                agent=agent_name,
            )
            if queued:
                session.update(
                    {
                        "campaign_id": campaign_id,
                        "session_stage": session_stage,
                        "artifact_key": artifact_key,
                        "last_prompt_at": created_at,
                        "last_prompt_fingerprint": fingerprint,
                        "last_source": source,
                    }
                )
            store.write_json(job_id, EVOLUTION_SESSION_PATH, session)
    except Exception as exc:  # noqa: BLE001 - optional lane cannot block the funnel
        return {"queued": False, "error": str(exc)[:300]}
    store.write_json(
        job_id,
        "reports/evolution/latest.json",
        {
            "job_id": job_id,
            "mode": "evolution",
            "status": "green" if queued else "yellow",
            "summary": "Dedicated evolution wake queued"
            if queued
            else "Dedicated evolution wake could not be queued",
            "session_id": session_id,
            "session_stage": session_stage,
            "artifact_key": artifact_key,
            "queued": queued,
            "source": source,
            "created_at": created_at,
        },
    )
    store.append_journal(
        job_id,
        {
            "type": "evolution_worker_wakeup",
            "campaign_id": campaign.get("campaign_id"),
            "session_stage": session_stage,
            "artifact_key": artifact_key,
            "session_id": session_id,
            "queued": queued,
            "source": source,
        },
    )
    return {"queued": queued, "session_id": session_id}


def _evolution_stage_handoff(entry: dict[str, Any]) -> dict[str, Any]:
    diagnostics = entry.get("diagnostics") or {}
    return {
        "from_stage": entry.get("session_stage"),
        "from_artifact": entry.get("artifact_key") or entry.get("session_stage"),
        "final_summary": str(diagnostics.get("final_assistant_text") or "")[-1_200:],
    }


def _latest_evolution_stage_handoff(
    store: JobStore, job_id: str, campaign_id: str
) -> dict[str, Any] | None:
    archive = store.read_json(job_id, EVOLUTION_SESSION_ARCHIVE_PATH, default={}) or {}
    for entry in reversed(list(archive.get("sessions") or [])):
        if str(entry.get("campaign_id") or "") == campaign_id:
            return _evolution_stage_handoff(entry)
    return None


def _evolution_artifact_behavior_changed(
    store: JobStore, job_id: str, session: dict[str, Any]
) -> bool | None:
    artifact_key = str(
        session.get("artifact_key") or session.get("session_stage") or ""
    )
    parts = artifact_key.split("-")
    if len(parts) != 4 or parts[0] != "candidate" or parts[2] != "attempt":
        return None
    try:
        slot = int(parts[1])
        attempt_index = int(parts[3])
    except ValueError:
        return None
    state = store.read_json(job_id, "state/evolution_campaign.json", default={}) or {}
    candidate = next(
        (
            item
            for item in state.get("candidates") or []
            if int(item.get("slot") or 0) == slot
        ),
        None,
    )
    if not candidate:
        return None
    attempts = list(candidate.get("attempts") or [])
    if attempt_index < 1 or attempt_index > len(attempts):
        return None
    postmortem = attempts[attempt_index - 1].get("postmortem") or {}
    value = (postmortem.get("behavior_diff") or {}).get("material_change")
    return value if isinstance(value, bool) else None


def retire_evolution_session(
    store: JobStore,
    job_id: str,
    campaign_id: str,
    *,
    abort_busy: bool = True,
) -> dict[str, Any] | None:
    """Export and delete exactly the automation-owned session for a campaign.

    The export is written before deletion, making retries safe if the process
    dies between the durable accounting write and the OpenCode API call.
    """
    from wayfinder_paths.jobs.benchmarks.agent_adapter import (
        meter_session_ids,
        session_diagnostic_summary,
    )
    from wayfinder_paths.jobs.compute_lock import job_state_lock

    with job_state_lock(store.repo_root, job_id, name="evolution_session"):
        session = store.read_json(job_id, EVOLUTION_SESSION_PATH, default={}) or {}
        if str(session.get("campaign_id") or "") != str(campaign_id):
            return None
        if session.get("retired_at"):
            handoff = _latest_evolution_stage_handoff(store, job_id, str(campaign_id))
            return {
                "retired": True,
                "already_retired": True,
                "session_id": session.get("session_id"),
                "handoff": handoff,
            }
        session_id = str(session.get("session_id") or "")
        if not session_id:
            return None
        if not abort_busy and OPENCODE_CLIENT.session_statuses().get(session_id):
            return {"retired": False, "session_id": session_id, "busy": True}
        try:
            metrics = meter_session_ids([session_id])
        except Exception as exc:  # noqa: BLE001 - never delete before export
            return {
                "retired": False,
                "session_id": session_id,
                "error": f"session metrics export failed: {str(exc)[:300]}",
            }
        if int(metrics.get("sessions") or 0) != 1:
            return {
                "retired": False,
                "session_id": session_id,
                "error": "session metrics export did not resolve the exact persisted session",
            }
        try:
            diagnostics = session_diagnostic_summary(session_id)
        except Exception as exc:  # noqa: BLE001 - never delete before export
            return {
                "retired": False,
                "session_id": session_id,
                "error": f"session diagnostic export failed: {str(exc)[:300]}",
            }
        archive = (
            store.read_json(job_id, EVOLUTION_SESSION_ARCHIVE_PATH, default={}) or {}
        )
        entries = list(archive.get("sessions") or [])
        exported = {
            "campaign_id": str(campaign_id),
            "session_stage": session.get("session_stage"),
            "artifact_key": session.get("artifact_key") or session.get("session_stage"),
            "session_id": session_id,
            "created_at": session.get("created_at"),
            "exported_at": utc_now_iso(),
            "metrics": metrics,
            "diagnostics": diagnostics,
            "behavior_changed": _evolution_artifact_behavior_changed(
                store, job_id, session
            ),
        }
        entries = [
            item
            for item in entries
            if not (
                item.get("campaign_id") == str(campaign_id)
                and item.get("session_id") == session_id
            )
        ]
        entries.append(exported)
        store.write_json(
            job_id,
            EVOLUTION_SESSION_ARCHIVE_PATH,
            {"schema_version": "1.0", "sessions": entries},
        )
        if OPENCODE_CLIENT.session_statuses().get(session_id):
            if not OPENCODE_CLIENT.abort_session(session_id):
                return {"retired": False, "session_id": session_id, "busy": True}
        deleted = OPENCODE_CLIENT.session_exists(
            session_id
        ) is False or OPENCODE_CLIENT.delete_session(session_id)
        if not deleted:
            return {"retired": False, "session_id": session_id}
        retired_at = utc_now_iso()
        session["retired_at"] = retired_at
        store.write_json(job_id, EVOLUTION_SESSION_PATH, session)
        entries[-1]["retired_at"] = retired_at
        store.write_json(
            job_id,
            EVOLUTION_SESSION_ARCHIVE_PATH,
            {"schema_version": "1.0", "sessions": entries},
        )
        store.append_journal(
            job_id,
            {
                "type": "evolution_session_retired",
                "campaign_id": str(campaign_id),
                "session_id": session_id,
                "session_stage": session.get("session_stage"),
                "artifact_key": session.get("artifact_key")
                or session.get("session_stage"),
                "metrics": metrics,
                "behavior_changed": entries[-1].get("behavior_changed"),
            },
        )
        return {
            "retired": True,
            "session_id": session_id,
            "metrics": metrics,
            "handoff": _evolution_stage_handoff(entries[-1]),
        }


def _write_report(
    *,
    store: JobStore,
    job_id: str,
    mode: AgentMode,
    status: str,
    summary: str,
    session_id: str | None,
    queued: bool,
    error: str | None,
    apply_proposal_id: str | None = None,
    cache: dict[str, Any] | None = None,
    wake_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report_dir = (
        store.job_dir(job_id) / "reports" / ("apply" if apply_proposal_id else mode)
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "job_id": job_id,
        "mode": mode,
        "status": status,
        "summary": summary,
        "session_id": session_id,
        "queued": queued,
        "error": error,
        "created_at": utc_now_iso(),
    }
    if apply_proposal_id:
        report["apply_proposal_id"] = apply_proposal_id
    if cache is not None:
        report["cache"] = cache
    if wake_context is not None:
        report.update(wake_context)
    (report_dir / "latest.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    # Durable session pointer. The wake agent overwrites latest.json with its
    # own structured finding, which drops session_id/created_at — so the
    # frontend's per-job Conversations list loses the link (observed: a job
    # whose agent wrote rich findings showed "No linked conversations yet"
    # while one whose envelope survived linked fine). This sidecar is never
    # touched by the agent; snapshot_job backfills from it. The session per
    # (job, mode) is stable (reused by title), so a stale sidecar stays
    # correct. Only overwrite on a real session so a failed wake can't blank
    # a good pointer.
    if session_id:
        (report_dir / "session.json").write_text(
            json.dumps(
                {"session_id": session_id, "created_at": report["created_at"]},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    scorecard_updates: dict[str, Any] = {
        "health": status,
        "last_agent_check_at": report["created_at"],
        "last_agent_mode": mode,
        "last_agent_summary": report["summary"],
    }
    if cache is not None:
        scorecard_updates["last_agent_cache"] = cache
    store.refresh_scorecard(
        job_id,
        scorecard_updates,
    )
    store.append_journal(
        job_id,
        {
            "type": "agent_wakeup",
            "mode": mode,
            "wake_source": report.get("wake_source"),
            "wake_triggers": report.get("wake_triggers") or [],
            "report": report,
        },
    )

    try:
        sync_all_jobs(store=store)
    except Exception:
        logger.opt(exception=True).debug(
            "Wayfinder job sync failed after worker wakeup"
        )
    return report


def _auto_limits_error(limits: dict[str, Any]) -> str | None:
    venues = [
        str(v).strip() for v in limits.get("enabled_venues") or [] if str(v).strip()
    ]
    symbols = [
        str(v).strip() for v in limits.get("allowed_symbols") or [] if str(v).strip()
    ]
    markets = [
        str(v).strip() for v in limits.get("allowed_markets") or [] if str(v).strip()
    ]
    if not venues:
        return "enabled_venues must include at least one venue"
    if not symbols and not markets:
        return "allowed_symbols or allowed_markets must include at least one tradable target"
    for key in (
        "max_notional_per_decision",
        "max_daily_notional",
        "max_open_positions",
        "max_open_orders",
    ):
        if float(limits.get(key) or 0) <= 0:
            return f"{key} must be greater than 0"
    return None
