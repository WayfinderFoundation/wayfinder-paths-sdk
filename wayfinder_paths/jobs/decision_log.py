"""Decision log: the job's narrative, assembled mechanically for the UI.

The strongest trust evidence the system produces — a proposal generation
chain culled by its own verification bars, a screen discovering a
promote-grade symbol — was invisible in the UI, which showed binary
Ran/Failed rows. This module assembles a threaded, outcome-labeled feed
from events the agents ALREADY record (journal provenance, research-ledger
notes, proposal summaries, scan artifacts). Deliberately no new agent-side
writing protocol: protocols get dropped; recorded reasons do not.

Framing matters: an agent rejection whose reason cites a verification bar
or supersession is the system WORKING (`self_culled`), not failure noise —
the outcome vocabulary encodes that so the UI can say "verification caught
it".
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.evolution_funnel import (
    format_evolution_funnel,
    summarize_evolution_funnel,
)
from wayfinder_paths.jobs.store import JobStore

DEFAULT_LIMIT = 50
_DETAIL_CAP = 300
_JOURNAL_TAIL_LINES = 4000
_LEDGER_TAIL_ROWS = 120
_STATS_WINDOW_DAYS = 7

_PROPOSAL_ID_RE = re.compile(r"prop-[a-z0-9-]+-[0-9a-f]{8}|prop-[0-9a-f]{8}")
_SELF_CULL_HINTS = ("verification bar", "superseded", "supersede", "stale draft")


def build_decision_log(
    store: JobStore, job_id: str, *, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    root = store.job_dir(job_id)
    proposals = _load_proposals(root)
    entries: list[dict[str, Any]] = []
    active_evolution = _active_evolution_entry(root)
    if active_evolution:
        entries.append(active_evolution)
    entries.extend(_journal_entries(root, proposals))
    entries.extend(_ledger_entries(root))
    entries.extend(_universe_entries(root))
    entries.extend(_shadow_entries(root))
    entries.sort(key=lambda entry: str(entry.get("ts") or ""), reverse=True)
    _assign_threads(entries)
    # Stats over the FULL set BEFORE the display cap — a busy week must not
    # undercount, and a small --limit must not produce nonsense glance
    # numbers (caught live: --limit 5 reported self_culled=0 for a week
    # that had them).
    stats = _glance_stats(entries)
    return {
        "entries": entries[: max(int(limit), 1)],
        "stats": stats,
        "generated_at": _now_iso(),
    }


# ── sources ──────────────────────────────────────────────────────────────


def _journal_entries(
    root: Path, proposals: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    path = root / "journal.jsonl"
    entries: list[dict[str, Any]] = []
    for event in _read_jsonl_tail(path, _JOURNAL_TAIL_LINES):
        kind = str(event.get("type") or "")
        ts = str(event.get("ts") or "")
        pid = str(event.get("proposal_id") or "") or None
        title = _proposal_title(proposals, pid)
        if kind == "proposal_created":
            entries.append(
                _entry(
                    ts,
                    "proposal",
                    title or "Proposal created",
                    "",
                    "pending",
                    actor="agent",
                    proposal_id=pid,
                )
            )
        elif kind == "proposal_rejected":
            by = str(event.get("rejected_by") or "")
            reason = str(event.get("reason") or "")
            if by == "owner":
                outcome = "owner_rejected"
            elif reason and any(h in reason.lower() for h in _SELF_CULL_HINTS):
                outcome = "self_culled"
            else:
                outcome = (
                    "self_culled" if by == "agent" and reason else "owner_rejected"
                )
            entries.append(
                _entry(
                    ts,
                    "proposal",
                    title or "Proposal rejected",
                    reason,
                    outcome,
                    actor=by or "unknown",
                    proposal_id=pid,
                )
            )
        elif kind == "proposal_promoted":
            files = event.get("changed_files") or []
            entries.append(
                _entry(
                    ts,
                    "proposal",
                    title or "Proposal applied",
                    f"revision {event.get('revision')}; {len(files)} file(s)",
                    "applied",
                    actor="owner",
                    proposal_id=pid,
                )
            )
        elif kind.startswith("probation_leg_"):
            action = kind.removeprefix("probation_leg_")
            entries.append(
                _entry(
                    ts,
                    "probation",
                    f"Probation leg {event.get('leg')}: {action}",
                    "",
                    "applied" if action == "opened" else "info",
                    actor="agent",
                    proposal_id=str(event.get("proposal_id") or "") or None,
                )
            )
        elif kind == "application_watchdog_recovered":
            entries.append(
                _entry(
                    ts,
                    "recovery",
                    "Watchdog recovered a stalled apply",
                    str(event.get("proposal_id") or ""),
                    "info",
                    actor="watchdog",
                )
            )
        elif kind == "stale_baseline_promotion_refused":
            entries.append(
                _entry(
                    ts,
                    "proposal",
                    (
                        f"Apply deferred: {title}"
                        if title
                        else "Apply deferred — stale baseline"
                    ),
                    "Candidate was staged before an earlier change applied; "
                    "promoting it would revert that change. Re-stage requested "
                    "— approval carries over, no action needed.",
                    "info",
                    actor="system",
                    proposal_id=pid,
                )
            )
        elif kind == "proposal_restaged":
            entries.append(
                _entry(
                    ts,
                    "proposal",
                    (
                        f"Re-staged against current strategy: {title}"
                        if title
                        else "Change re-staged against current strategy"
                    ),
                    f"new base revision {event.get('new_base_revision')}; "
                    "apply re-queued automatically",
                    "info",
                    actor="agent",
                    proposal_id=pid,
                )
            )
        elif kind == "proposal_apply_finished":
            status = str(event.get("application_status") or "")
            error = str(event.get("error") or "")
            # Applied is already narrated by proposal_promoted; deferrals by
            # stale_baseline_promotion_refused. Only genuine failures add news.
            if status == "failed" and "baseline drift" not in error:
                entries.append(
                    _entry(
                        ts,
                        "proposal",
                        f"Apply failed: {title}" if title else "Apply failed",
                        error or "no error recorded",
                        "info",
                        actor="system",
                        proposal_id=pid,
                    )
                )
        elif kind == "owner_workspace_repair":
            entries.append(
                _entry(
                    ts,
                    "recovery",
                    "Owner repaired the strategy workspace",
                    str(event.get("reason") or ""),
                    "info",
                    actor="owner",
                )
            )
        elif kind == "data_feed_degraded":
            cause = str(event.get("cause") or "unknown")
            hint = {
                "out_of_credits": " — top up API credits to restore data",
                "rate_limited": " — backing off; recovers on its own",
                "features_stale": " — research features are not advancing",
            }.get(cause, "")
            entries.append(
                _entry(
                    ts,
                    "halt",
                    f"Data feed degraded ({cause}){hint}",
                    str(event.get("error") or ""),
                    "info",
                    actor="system",
                )
            )
        elif kind == "lifecycle_decision":
            decision = str(event.get("decision") or "")
            metrics = event.get("metrics") or {}
            entries.append(
                _entry(
                    ts,
                    "proposal",
                    f"Lifecycle controller {decision} probation leg "
                    f"{event.get('leg')}",
                    f"{metrics.get('closed_trades')} trades, "
                    f"WR {metrics.get('win_rate')}, net {metrics.get('net_pnl')} USD "
                    "— pre-registered rules, evaluated mechanically",
                    "info",
                    actor="system",
                )
            )
        elif kind == "promotion_verdict":
            verdict = str(event.get("verdict") or "unknown")
            delta = float(event.get("delta_net_pnl") or 0.0)
            label = {
                "beat": "beat the incumbent",
                "neutral": "ran even with the incumbent",
                "hurt": "underperformed the incumbent",
            }.get(verdict, verdict)
            entries.append(
                _entry(
                    ts,
                    "proposal",
                    f"Forward verdict: promoted change {label} ({delta:+.2f} USD)",
                    f"{event.get('window_days')}d shadow window, "
                    f"{event.get('closes')} closes",
                    "info",
                    actor="system",
                    proposal_id=str(event.get("proposal_id") or "") or None,
                )
            )
        elif kind == "ideation_artifact":
            buckets = event.get("buckets") or {}
            bucket_desc = ", ".join(
                f"{buckets[name]} {name}"
                for name in ("testable", "starved", "refuted")
                if buckets.get(name)
            )
            entries.append(
                _entry(
                    ts,
                    "research",
                    f"Research expedition: {int(event.get('hypotheses') or 0)} "
                    f"hypotheses from {int(event.get('sources') or 0)} external sources",
                    bucket_desc,
                    "info",
                    actor="agent",
                )
            )
        elif kind == "ideation_incomplete":
            age_s = event.get("artifact_age_s")
            detail = (
                "no expedition artifact has ever been produced"
                if age_s is None
                else f"last artifact is {int(age_s) // 3600}h old"
            )
            entries.append(
                _entry(
                    ts,
                    "research",
                    "Research expedition overdue — daily ideation contract not met",
                    detail,
                    "info",
                    actor="system",
                )
            )
        elif kind == "evolution_campaign_completed":
            counts = event.get("counts") or {}
            funnel = event.get("funnel")
            paper_proposals = _count(event, "paper_proposals")
            title = "Evolution campaign completed"
            if paper_proposals:
                title += (
                    f" — {paper_proposals} advanced to forward paper testing"
                )
            elif "paper_proposals" in event:
                title += " — no candidate advanced"
            entries.append(
                _entry(
                    ts,
                    "research",
                    title,
                    (
                        format_evolution_funnel(funnel)
                        if isinstance(funnel, dict)
                        else _evolution_counts_detail(counts)
                    ),
                    "info",
                    actor="harness",
                    metadata={
                        "campaign_id": event.get("campaign_id"),
                        "funnel": funnel,
                    }
                    if isinstance(funnel, dict)
                    else None,
                )
            )
        elif kind == "evolution_campaign_failed":
            funnel = event.get("funnel")
            reason = str(
                event.get("reason")
                or "campaign did not reach a terminal verdict within its safety horizon"
            )
            attempts = _count(event, "finalize_attempts")
            if attempts:
                reason += f"; {attempts} finalization attempt(s)"
            if isinstance(funnel, dict):
                reason += f" · {format_evolution_funnel(funnel)}"
            entries.append(
                _entry(
                    ts,
                    "recovery",
                    "Evolution campaign stopped before completion",
                    reason,
                    "info",
                    actor="watchdog",
                    metadata={
                        "campaign_id": event.get("campaign_id"),
                        "funnel": funnel,
                    }
                    if isinstance(funnel, dict)
                    else None,
                )
            )
        elif kind == "data_feed_recovered":
            entries.append(
                _entry(
                    ts,
                    "halt",
                    "Data feed recovered",
                    "",
                    "info",
                    actor="system",
                )
            )
        elif kind in ("halt_requested", "halt_flattened"):
            entries.append(
                _entry(
                    ts,
                    "halt",
                    "Halt requested"
                    if kind == "halt_requested"
                    else "Halt flattened positions",
                    str(event.get("reason") or ""),
                    "info",
                    actor="system",
                )
            )
    return entries


def _active_evolution_entry(root: Path) -> dict[str, Any] | None:
    """Project mutable campaign state into one compact live feed row.

    Worker wakes and candidate evaluations are intentionally not journal rows:
    the UI needs the campaign's progress, not its orchestration chatter.  The
    row disappears once the campaign is terminal, when the durable completion
    or failure journal event takes over.
    """
    state = _read_json(root / "state" / "evolution_campaign.json")
    if not state or state.get("status") not in {"active", "finalizing"}:
        return None
    stage = str(state.get("stage") or "generate")
    title = {
        "generate": "Evolution campaign generating candidates",
        "draining": "Evolution campaign preparing full development",
        "full_dev": "Evolution campaign running full development",
        "paper_proposal": "Evolution campaign checking finalists",
    }.get(stage, "Evolution campaign in progress")
    funnel = summarize_evolution_funnel(state)
    return _entry(
        _latest_evolution_ts(state),
        "research",
        title,
        format_evolution_funnel(funnel),
        "info",
        actor="harness",
        metadata={"campaign_id": state.get("campaign_id"), "funnel": funnel},
    )


def _latest_evolution_ts(state: dict[str, Any]) -> str:
    timestamp_keys = (
        "started_at",
        "finalize_started_at",
        "prepared_at",
        "evaluation_claimed_at",
        "evaluated_at",
        "full_dev_claimed_at",
        "full_dev_at",
        "proposal_claimed_at",
        "proposed_at",
    )
    values = [str(state.get(key) or "") for key in timestamp_keys]
    for candidate in state.get("candidates") or []:
        if isinstance(candidate, dict):
            values.extend(str(candidate.get(key) or "") for key in timestamp_keys)
    return max((value for value in values if value), default="")


def _count(counts: Any, name: str) -> int:
    if not isinstance(counts, dict):
        return 0
    try:
        return max(int(counts.get(name) or 0), 0)
    except (TypeError, ValueError):
        return 0


def _evolution_counts_detail(counts: Any) -> str:
    return (
        f"{_count(counts, 'generated')} generated · "
        f"{_count(counts, 'quick_evaluated')} screened · "
        f"{_count(counts, 'full_dev')} full-development evaluations · "
        f"{_count(counts, 'proposed')} finalist-gate evaluations"
    )


def _ledger_entries(root: Path) -> list[dict[str, Any]]:
    path = root / "ledgers" / "candidates.jsonl"
    entries: list[dict[str, Any]] = []
    for row in _read_jsonl_tail(path, _LEDGER_TAIL_ROWS):
        note = str(row.get("note") or "")
        entries.append(
            _entry(
                str(row.get("ts") or ""),
                "research",
                str(row.get("name") or row.get("family") or "research"),
                note,
                "info",
                actor="agent",
                proposal_id=_first_proposal_id(note),
                family=str(row.get("family") or "") or None,
            )
        )
    return entries


def _universe_entries(root: Path) -> list[dict[str, Any]]:
    path = root / "results" / "research" / "universe_scan.json"
    doc = _read_json(path)
    if not doc:
        return []
    found = [
        candidate
        for candidate in doc.get("candidates") or []
        if (candidate.get("promote") or 0) + (candidate.get("probation") or 0) > 0
    ]
    if not found:
        return []
    names = ", ".join(
        f"{c['symbol']} ({c.get('promote', 0)}P/{c.get('probation', 0)}p)"
        for c in found[:5]
    )
    return [
        _entry(
            str(doc.get("generated_at") or ""),
            "discovery",
            f"Universe screen: {len(found)} candidate symbol(s) with edge rows",
            f"{names} — pooled over {doc.get('pooled_tests')} tests",
            "discovery",
            actor="agent",
        )
    ]


def _shadow_entries(root: Path) -> list[dict[str, Any]]:
    doc = _read_json(root / "results" / "forward" / "counterfactual.json")
    if not doc or not doc.get("available"):
        return []
    delta = doc.get("delta_net_pnl")
    lead = "active ahead" if (delta or 0) >= 0 else "prior revision ahead"
    return [
        _entry(
            str(doc.get("computed_at") or ""),
            "shadow_check",
            f"Shadow A/B vs pre-change revision: {lead} ({delta:+.2f})",
            f"window {doc.get('window', {}).get('days')}d since "
            f"{str(doc.get('applied_at'))[:10]}",
            "info",
            actor="harness",
            proposal_id=str(doc.get("proposal_id") or "") or None,
        )
    ]


# ── threading + stats ────────────────────────────────────────────────────


def _assign_threads(entries: list[dict[str, Any]]) -> None:
    """Union proposals into threads via supersession references.

    The agents already write "superseded by prop-…" (and cite prior ids) in
    rejection reasons and ledger notes — chase those references so a
    three-generation chain shares one thread key (the OLDEST proposal id in
    the chain, stable as generations are added)."""
    parent: dict[str, str] = {}

    def find(pid: str) -> str:
        while parent.get(pid, pid) != pid:
            parent[pid] = parent.get(parent[pid], parent[pid])
            pid = parent[pid]
        return pid

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            # deterministic: keep the lexicographically smaller root
            parent[max(ra, rb)] = min(ra, rb)

    for entry in entries:
        pid = entry.get("proposal_id")
        if not pid:
            continue
        parent.setdefault(pid, pid)
        # Titles carry proposal summaries ("v2 — supersedes prop-…"), details
        # carry rejection reasons and ledger notes; both cite prior ids.
        haystack = f"{entry.get('title') or ''} {entry.get('detail') or ''}"
        for ref in _PROPOSAL_ID_RE.findall(haystack):
            if ref != pid:
                parent.setdefault(ref, ref)
                union(pid, ref)
    for entry in entries:
        pid = entry.get("proposal_id")
        if pid:
            entry["thread"] = find(pid)


def _glance_stats(entries: list[dict[str, Any]]) -> dict[str, Any]:
    cutoff = _days_ago_iso(_STATS_WINDOW_DAYS)
    recent = [e for e in entries if str(e.get("ts") or "") >= cutoff]
    stats = {
        "window_days": _STATS_WINDOW_DAYS,
        "proposals_created": sum(
            1 for e in recent if e["kind"] == "proposal" and e["outcome"] == "pending"
        ),
        # Applied counts PROPOSALS only — probation legs opening also carry
        # the applied outcome for chip tone, but they are a consequence of
        # one proposal, not separate applies.
        "applied": sum(
            1 for e in recent if e["outcome"] == "applied" and e["kind"] == "proposal"
        ),
        "owner_rejected": sum(1 for e in recent if e["outcome"] == "owner_rejected"),
        "self_culled": sum(1 for e in recent if e["outcome"] == "self_culled"),
        "research_rows": sum(1 for e in recent if e["kind"] == "research"),
        "discoveries": sum(1 for e in recent if e["outcome"] == "discovery"),
    }
    focus = next(
        (
            e
            for e in entries
            if e["kind"] == "proposal" and e["outcome"] in ("pending", "applied")
        ),
        None,
    ) or (entries[0] if entries else None)
    stats["current_focus"] = str(focus.get("title"))[:120] if focus else None
    return stats


# ── helpers ──────────────────────────────────────────────────────────────


def _entry(
    ts: str,
    kind: str,
    title: str,
    detail: str,
    outcome: str,
    *,
    actor: str,
    proposal_id: str | None = None,
    family: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "ts": ts,
        "kind": kind,
        "title": str(title)[:160],
        "detail": str(detail)[:_DETAIL_CAP],
        "outcome": outcome,
        "actor": actor,
    }
    if proposal_id:
        entry["proposal_id"] = proposal_id
    if family:
        entry["family"] = family
    if metadata:
        entry["metadata"] = metadata
    return entry


def _load_proposals(root: Path) -> dict[str, dict[str, Any]]:
    proposals: dict[str, dict[str, Any]] = {}
    directory = root / "proposals"
    if not directory.is_dir():
        return proposals
    for path in directory.glob("*.json"):
        doc = _read_json(path)
        if isinstance(doc, dict) and doc.get("proposal_id"):
            proposals[str(doc["proposal_id"])] = doc
    return proposals


def _proposal_title(
    proposals: dict[str, dict[str, Any]], proposal_id: str | None
) -> str | None:
    if not proposal_id or proposal_id not in proposals:
        return None

    def find(d: Any) -> str | None:
        if isinstance(d, dict):
            summary = d.get("summary")
            if isinstance(summary, str) and summary.strip():
                return summary.strip()
            for value in d.values():
                found = find(value)
                if found:
                    return found
        return None

    return find(proposals[proposal_id])


def _first_proposal_id(text: str) -> str | None:
    match = _PROPOSAL_ID_RE.search(text or "")
    return match.group(0) if match else None


def _read_jsonl_tail(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _now_iso() -> str:
    import datetime as dt

    return dt.datetime.now(dt.UTC).isoformat()


def _days_ago_iso(days: int) -> str:
    import datetime as dt

    return (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).isoformat()
