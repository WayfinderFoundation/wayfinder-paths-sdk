from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

from wayfinder_paths.mcp.tools.polymarket import polymarket_read


def _load_cases(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("fixture must be a JSON list")
    return [case for case in data if isinstance(case, dict)]


def _candidate_rank(candidates: list[dict[str, Any]], target_id: str) -> int | None:
    for idx, candidate in enumerate(candidates, 1):
        if candidate.get("slug") == target_id:
            return idx
    return None


def _event_rank(result: dict[str, Any], target_id: str) -> int | None:
    for idx, group in enumerate(result.get("eventGroups") or [], 1):
        if group.get("eventSlug") == target_id:
            return idx
    return None


def _candidate_event_rank(candidates: list[dict[str, Any]], target_id: str) -> int | None:
    for idx, candidate in enumerate(candidates, 1):
        if candidate.get("eventSlug") == target_id or candidate.get("slug") == target_id:
            return idx
    return None


def _prefix_rank(result: dict[str, Any], prefix: str) -> int | None:
    candidates = result.get("candidates") or []
    for idx, candidate in enumerate(candidates, 1):
        if str(candidate.get("eventSlug") or "").startswith(prefix) or str(candidate.get("slug") or "").startswith(prefix):
            return idx
    for idx, group in enumerate(result.get("eventGroups") or [], 1):
        if str(group.get("eventSlug") or "").startswith(prefix):
            return idx
    return None


def _pass_for_case(case: dict[str, Any], result: dict[str, Any]) -> tuple[bool, int | None, str]:
    target_id = str(case["id"])
    candidates = result.get("candidates") or []
    match = case.get("match") if isinstance(case.get("match"), dict) else {}
    event_prefix = str(match.get("event_slug_prefix") or "")
    if event_prefix:
        rank = _prefix_rank(result, event_prefix)
        if rank is not None and rank <= 5:
            return True, rank, "rolling_event_family"
        return False, rank, "missing_rolling_event_family"

    if case.get("id_type") == "market":
        rank = _candidate_rank(candidates, target_id)
        if rank is not None and rank <= 5:
            return True, rank, "candidate"
        return False, rank, "missing_market_top5"

    group_rank = _event_rank(result, target_id)
    if group_rank is not None and group_rank <= 3:
        return True, group_rank, "event_group"
    candidate_rank = _candidate_event_rank(candidates, target_id)
    if candidate_rank is not None and candidate_rank <= 5:
        return True, candidate_rank, "candidate_event"
    return False, group_rank or candidate_rank, "missing_event_top3_or_candidate_top5"


async def _run_query(case: dict[str, Any], query: str, *, candidate_limit: int) -> dict[str, Any]:
    started = time.perf_counter()
    out = await polymarket_read(
        "search",
        query=query,
        limit=10,
        candidate_limit=candidate_limit,
        summary=True,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    if not out.get("ok"):
        return {
            "caseId": case["id"],
            "idType": case["id_type"],
            "label": case.get("label"),
            "query": query,
            "passed": False,
            "rank": None,
            "hitSource": "tool_error",
            "elapsedMs": elapsed_ms,
            "error": out.get("error"),
            "relevance": {},
        }

    result = out["result"]
    passed, rank, hit_source = _pass_for_case(case, result)
    relevance = result.get("relevance") or {}
    return {
        "caseId": case["id"],
        "idType": case["id_type"],
        "label": case.get("label"),
        "query": query,
        "passed": passed,
        "rank": rank,
        "hitSource": hit_source,
        "elapsedMs": elapsed_ms,
        "candidateSlugs": [c.get("slug") for c in (result.get("candidates") or [])[:5]],
        "eventGroups": [g.get("eventSlug") for g in (result.get("eventGroups") or [])[:3]],
        "relevance": relevance,
        "queryCount": len(relevance.get("queriesTried") or []),
        "directHydrationCount": len(relevance.get("directHydrations") or []),
        "eventHydrationCount": len(relevance.get("eventHydrations") or []),
        "mode": relevance.get("mode"),
        "failureReason": None if passed else hit_source,
    }


def _pct(num: int, den: int) -> float:
    return round((100.0 * num / den), 2) if den else 0.0


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    passed = [row for row in rows if row["passed"]]
    ranks = [int(row["rank"]) for row in passed if row.get("rank") is not None]
    elapsed = [float(row["elapsedMs"]) for row in rows]
    query_counts = [int(row.get("queryCount") or 0) for row in rows]
    direct_counts = [int(row.get("directHydrationCount") or 0) for row in rows]
    event_counts = [int(row.get("eventHydrationCount") or 0) for row in rows]
    by_type: dict[str, dict[str, Any]] = {}
    for id_type in sorted({str(row["idType"]) for row in rows}):
        subset = [row for row in rows if row["idType"] == id_type]
        subset_passed = [row for row in subset if row["passed"]]
        by_type[id_type] = {
            "total": len(subset),
            "passed": len(subset_passed),
            "recallPct": _pct(len(subset_passed), len(subset)),
        }
    return {
        "totalQueries": total,
        "passed": len(passed),
        "recallPct": _pct(len(passed), total),
        "recallAt1Pct": _pct(sum(1 for rank in ranks if rank <= 1), total),
        "recallAt3Pct": _pct(sum(1 for rank in ranks if rank <= 3), total),
        "recallAt5Pct": _pct(sum(1 for rank in ranks if rank <= 5), total),
        "byType": by_type,
        "avgElapsedMs": round(statistics.mean(elapsed), 2) if elapsed else 0.0,
        "p95ElapsedMs": round(statistics.quantiles(elapsed, n=20)[18], 2) if len(elapsed) >= 20 else max(elapsed, default=0.0),
        "avgQueryCount": round(statistics.mean(query_counts), 2) if query_counts else 0.0,
        "p95QueryCount": statistics.quantiles(query_counts, n=20)[18] if len(query_counts) >= 20 else max(query_counts, default=0),
        "avgDirectHydrations": round(statistics.mean(direct_counts), 2) if direct_counts else 0.0,
        "avgEventHydrations": round(statistics.mean(event_counts), 2) if event_counts else 0.0,
        "modes": {
            mode: sum(1 for row in rows if row.get("mode") == mode)
            for mode in sorted({str(row.get("mode")) for row in rows})
        },
        "worstMisses": [
            {
                "caseId": row["caseId"],
                "label": row.get("label"),
                "query": row["query"],
                "reason": row.get("failureReason"),
                "candidateSlugs": row.get("candidateSlugs"),
                "eventGroups": row.get("eventGroups"),
                "mode": row.get("mode"),
            }
            for row in rows
            if not row["passed"]
        ][:20],
    }


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Polymarket relevance search.")
    parser.add_argument("--fixture", default="evals/fixtures/polymarket_relevance_cases.json")
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--candidate-limit", type=int, default=10)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    cases = _load_cases(Path(args.fixture))
    if args.max_cases is not None:
        cases = cases[: args.max_cases]

    rows: list[dict[str, Any]] = []
    for case in cases:
        for query in case.get("queries") or []:
            rows.append(await _run_query(case, str(query), candidate_limit=args.candidate_limit))

    payload = {"summary": _summary(rows), "rows": rows}
    print(json.dumps(payload["summary"], indent=2))
    if args.output:
        Path(args.output).write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(_main())
