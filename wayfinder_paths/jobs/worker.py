from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

from wayfinder_paths.core.clients.OpenCodeClient import OPENCODE_CLIENT
from wayfinder_paths.jobs.derived_features import refresh_derived_features_if_stale
from wayfinder_paths.jobs.failures import cpu_steal_pct, disk_used_pct
from wayfinder_paths.jobs.forward import is_forward_empty
from wayfinder_paths.jobs.ledger import tail_ledger
from wayfinder_paths.jobs.memory_hygiene import sanitize_job_memory
from wayfinder_paths.jobs.models import (
    JOB_AUTO_WORKER_AGENT_NAME,
    JOB_WORKER_AGENT_NAME,
    AgentMode,
    normalize_agent_mode,
    utc_now_iso,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import snapshot_job, sync_all_jobs

JOB_RESULT_MARKER = "WAYFINDER_JOB_RESULT "
STABLE_PREFIX_END_MARKER = "## End Stable Cache Prefix"
DYNAMIC_CONTEXT_MARKER = "## Dynamic Wakeup Context"
VOLATILE_STABLE_KEYS = {"created_at", "updated_at", "ts"}


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


def _standing_checks_block(root: Path) -> dict[str, Any]:
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
                "decayed": rep.get("decayed"),
                "baseline_net_return": (rep.get("baseline") or {}).get("net_return"),
                "current_net_return": (rep.get("current") or {}).get("net_return"),
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
    if block:
        block["_basis"] = (
            "Routine numbers computed mechanically THIS wake — never re-fetch "
            "or re-derive them in-session; compare them to your gates and "
            "cite them. A pure status observation (runner healthy, gate "
            "still closed, no new trades) is an ops note, NOT research: "
            "write it with family operations/monitoring/no_change and it "
            "lands in the ops ledger automatically. The candidates ledger "
            "is for research verdicts only. "
            "backtest_replication.decayed=true means the ACTIVE revision's "
            "deploy-time in-sample edge is not reproducing on refreshed data "
            "— mechanical evidence of selection on window-local noise; treat "
            "it as grounds for a revert/kill or re-validation proposal. "
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
    mem_available_mb: int | None = None
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                mem_available_mb = int(line.split()[1]) // 1024
                break
    except Exception:  # noqa: BLE001 — non-Linux boxes have no /proc/meminfo
        mem_available_mb = None
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
    steal = cpu_steal_pct()
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
) -> dict[str, str]:
    root = store.job_dir(job_id)
    from wayfinder_paths.jobs.improver.spec import ImproverSpec

    improver_spec = ImproverSpec.load(root)
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
    search_assignment = None
    if mode == "intervene" and apply_proposal_id is None and not restage_tasks:
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
        "standing_checks": _standing_checks_block(root),
        "compute_status": _compute_status_block(root),
        "evolution": _evolution_block(store, job_id),
        "archive": _archive_block(store, job_id),
        "restage_tasks": restage_tasks,
        "search_assignment": search_assignment,
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
        "- Intervene mode may create candidate proposals under the job bundle, but cannot activate them.\n"
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
        "Your own reasoned self-rejections (superseded/stale drafts) do not "
        "bind — iterate freely. Before proposing ANYTHING, scan the rejected "
        "proposals for an equivalent prior ask.\n"
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
        "window has judged the last promotion. Act on it THIS wake — propose "
        "the next candidate, run the next experiment, or record in the "
        "report precisely why not. A neutral verdict means the change did "
        "nothing: that is license to try the next candidate, not to wait.\n"
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
            "- Review the dynamic context against the stable job contract. "
            "When you want to RECOMMEND a strategy/params change, do not "
            'hand-write proposal JSON: call `core_jobs(action="propose", '
            "job_id=..., kind=..., summary=..., intent_contract={...}, "
            "execution_params={...} | candidate_dir=...)` — it stages a "
            "validated candidate, runs the baseline-vs-candidate backtest "
            "comparison, and attaches the candidate_report approvals "
            "require.\n"
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
    # Impasse directive: written by the watchdog when a research-stale job's
    # last K wakes produced zero progress artifacts, cleared only when one
    # appears. Rendered as prompt text (never only snapshot JSON) with the
    # escape hatch stripped — the hatch is how three production jobs closed
    # every stale wake with "stated-not-advanced" prose for weeks.
    impasse_directive = ""
    impasse_marker = store.read_json(job_id, "state/research_impasse.json") or {}
    if impasse_marker.get("alerted_at"):
        if impasse_marker.get("status") == "mandated_work":
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
    dynamic_context = (
        f"{DYNAMIC_CONTEXT_MARKER}\n"
        f"{gate_alert}"
        f"{restage_priority}"
        f"{impasse_directive}"
        f"{ideation_directive}"
        "Current snapshot:\n"
        f"{_canonical_json(dynamic_payload, max_chars=12000)}\n\n"
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
        "- Emit a user-visible result only for meaningful state transitions, "
        "warnings, proposals, or blocked auto decisions.\n"
    )
    return {
        "prompt": stable_prefix + "\n" + dynamic_context,
        "stable_prefix": stable_prefix,
        "dynamic_context": dynamic_context,
        "stable_prefix_hash": hashlib.sha256(stable_prefix.encode()).hexdigest(),
        "dynamic_context_hash": hashlib.sha256(dynamic_context.encode()).hexdigest(),
    }


def prepare_job_worker_prompt(
    *,
    store: JobStore,
    job_id: str,
    mode: str,
    apply_proposal_id: str | None = None,
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
    job_id: str, mode: str = "monitor", *, apply_proposal_id: str | None = None
) -> dict[str, Any]:
    store = JobStore()
    job = store.load(job_id)
    mode = normalize_agent_mode(mode) if mode else job.agent_loop.mode
    if mode == "off":
        mode = "monitor"
    mode_typed: AgentMode = mode

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
        )
        _emit_job_result(report["summary"], job.id)
        return report

    session_id = _ensure_worker_session(job.id, mode_typed)
    queued = False
    error: str | None = None
    prompt_sections = prepare_job_worker_prompt(
        store=store,
        job_id=job.id,
        mode=mode_typed,
        apply_proposal_id=apply_proposal_id,
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
) -> dict[str, Any]:
    report_dir = (
        store.job_dir(job_id) / "reports" / ("apply" if apply_proposal_id else mode)
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    report = {
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
        job_id, {"type": "agent_wakeup", "mode": mode, "report": report}
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
