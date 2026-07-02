"""Deterministic memory hygiene for job workers.

The intervene worker's durable memory (`memory.md` + `memory.json`) gets poisoned
when the agent writes a fabricated forward-performance figure — e.g. relabeling a
candidate-backtest result as a "forward prove-out". Once written, that claim rides
the stable prompt prefix into every later wake and gets restated. Prompt-side
steering (config prose, co-located labels) does not stop the model from writing it.

This module removes the poison deterministically at prompt-build time — but ONLY
on a wake with no forward telemetry, the one state where any win-rate / dollar-PnL
/ trade-count figure is provably unsupported. Offending lines/entries are moved to
an auditable `memory_quarantine.jsonl` (never deleted outright) and journaled, so
the agent can no longer restate them while nothing is lost.

The performance-claim detector is the single source of truth shared with the eval
harness's `no_unsupported_performance_claims` validator.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from wayfinder_paths.jobs.forward import is_forward_empty
from wayfinder_paths.jobs.models import utc_now_iso

if TYPE_CHECKING:
    from wayfinder_paths.jobs.store import JobStore

# Nonzero win-rate% (near "win"), dollar PnL, and trade/fill/win/loss counts —
# the confabulation triad. Each pattern captures a numeric group so honest zeros
# ("0 trades", "$0") are ignored. Bare metrics (sharpe, a plain net-return %) are
# NOT matched, so a backtest metric cited by name survives.
_PERF_CLAIM_PATTERNS = (
    re.compile(r"win[\s_-]*rate[^.\n%]{0,24}?(\d{1,3}(?:\.\d+)?)\s*%", re.I),
    re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%\s*win", re.I),
    re.compile(r"[+\-]?\$\s?(\d[\d,]*(?:\.\d+)?)", re.I),
    re.compile(
        r"(\d+)\s+(?:forward\s+|winning\s+|losing\s+)?"
        r"(?:trades|fills|wins|losses)\b",
        re.I,
    ),
)

QUARANTINE_REASON = "unsupported_forward_performance_claim_no_forward_telemetry"


def scan_unsupported_perf_claims(text: str) -> list[str]:
    """Return the surface forms of any NONZERO performance figures in `text`."""
    hits: list[str] = []
    for pattern in _PERF_CLAIM_PATTERNS:
        for match in pattern.finditer(text or ""):
            raw = match.group(1).replace(",", "")
            try:
                if float(raw) != 0.0:
                    hits.append(match.group(0).strip())
            except ValueError:
                continue
    return hits


def sanitize_memory_markdown(text: str) -> tuple[str, list[str]]:
    """Pull any markdown line stating an unsupported performance figure into a
    quarantine list. Returns (cleaned_text, quarantined_lines). Only offending
    lines are removed; surrounding structure (headings, other bullets) is kept."""
    if not text:
        return text, []
    kept: list[str] = []
    quarantined: list[str] = []
    for line in text.splitlines():
        if line.strip() and scan_unsupported_perf_claims(line):
            quarantined.append(line.strip())
        else:
            kept.append(line)
    if not quarantined:
        return text, []
    cleaned = "\n".join(kept)
    if text.endswith("\n") and not cleaned.endswith("\n"):
        cleaned += "\n"
    return cleaned, quarantined


def _entry_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return " ".join(str(v) for v in item.values() if isinstance(v, str))
    return ""


def sanitize_memory_json(obj: Any) -> tuple[Any, list[str]]:
    """Drop structured memory entries stating an unsupported performance figure:
    poisoned `lessons`/`constraints` entries are removed, a poisoned
    `current_concern` is nulled. Returns (cleaned_obj, quarantined_texts)."""
    if not isinstance(obj, dict):
        return obj, []
    quarantined: list[str] = []
    result = dict(obj)
    for key in ("lessons", "constraints"):
        items = obj.get(key)
        if not isinstance(items, list):
            continue
        kept: list[Any] = []
        for item in items:
            text = _entry_text(item)
            if text and scan_unsupported_perf_claims(text):
                quarantined.append(item if isinstance(item, str) else json.dumps(item))
            else:
                kept.append(item)
        result[key] = kept
    concern = obj.get("current_concern")
    if isinstance(concern, str) and scan_unsupported_perf_claims(concern):
        quarantined.append(concern)
        result["current_concern"] = None
    return result, quarantined


def sanitize_job_memory(
    store: JobStore, job_id: str, *, forward: dict[str, Any] | None
) -> dict[str, Any]:
    """Remove unsupported performance claims from durable memory before the agent
    reads it next wake — breaking the confabulation-poisoning propagation chain.

    No-op unless the job has NO forward telemetry (`is_forward_empty`): while
    forward data exists a performance claim may be legitimately supported, so we
    do not touch it. Quarantined content is appended to `memory_quarantine.jsonl`
    and a `memory_quarantined` journal event is emitted; nothing is deleted. The
    operation is idempotent — a second clean wake finds nothing to remove.
    """
    summary = {"active": False, "md": 0, "json": 0}
    if not is_forward_empty(forward):
        return summary
    summary["active"] = True
    root = store.job_dir(job_id)
    removed: list[dict[str, str]] = []

    md_path = root / "memory.md"
    if md_path.exists():
        cleaned, quarantined = sanitize_memory_markdown(
            md_path.read_text(encoding="utf-8")
        )
        if quarantined:
            md_path.write_text(cleaned, encoding="utf-8")
            removed.extend({"source": "memory.md", "text": t} for t in quarantined)
            summary["md"] = len(quarantined)

    mem_json = store.read_json(job_id, "memory.json", default=None)
    if isinstance(mem_json, dict):
        cleaned_json, quarantined = sanitize_memory_json(mem_json)
        if quarantined:
            store.write_json(job_id, "memory.json", cleaned_json)
            removed.extend({"source": "memory.json", "text": t} for t in quarantined)
            summary["json"] = len(quarantined)

    if removed:
        stamp = utc_now_iso()
        with (root / "memory_quarantine.jsonl").open("a", encoding="utf-8") as fh:
            for entry in removed:
                fh.write(
                    json.dumps(
                        {**entry, "quarantined_at": stamp, "reason": QUARANTINE_REASON}
                    )
                    + "\n"
                )
        store.append_journal(
            job_id,
            {
                "type": "memory_quarantined",
                "count": len(removed),
                "md": summary["md"],
                "json": summary["json"],
            },
        )
    return summary
