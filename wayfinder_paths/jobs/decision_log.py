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
    entries.extend(_journal_entries(root, proposals))
    entries.extend(_ledger_entries(root))
    entries.extend(_universe_entries(root))
    entries.extend(_shadow_entries(root))
    entries.sort(key=lambda entry: str(entry.get("ts") or ""), reverse=True)
    _assign_threads(entries)
    entries = entries[: max(int(limit), 1)]
    return {
        "entries": entries,
        "stats": _glance_stats(entries),
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
