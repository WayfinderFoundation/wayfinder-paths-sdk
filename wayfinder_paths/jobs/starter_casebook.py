"""Compact, reusable lessons from audited starter-strategy research.

The full research history does not belong in every worker prompt.  This
loader validates the shipped casebook once per process and retrieves a small,
tag-matched set for an evolution campaign's immutable context.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

CASEBOOK_PATH = Path(__file__).parent / "prompts" / "starter_research_cases.json"
MAX_PROMPT_CASES = 5


@lru_cache(maxsize=1)
def load_starter_casebook() -> tuple[dict[str, Any], ...]:
    payload = json.loads(CASEBOOK_PATH.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "1.0":
        raise ValueError("unsupported starter research casebook schema")
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError("starter research casebook cases must be a list")
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for raw in cases:
        if not isinstance(raw, dict):
            raise ValueError("starter research case must be an object")
        case_id = str(raw.get("id") or "")
        lesson = str(raw.get("lesson") or "")
        if not case_id or not lesson or case_id in seen:
            raise ValueError(f"invalid or duplicate starter research case {case_id!r}")
        seen.add(case_id)
        validated.append(
            {
                "id": case_id,
                "tags": sorted({str(tag) for tag in raw.get("tags") or []}),
                "lesson": lesson,
                "source": str(raw.get("source") or ""),
            }
        )
    return tuple(validated)


def select_starter_cases(
    tags: set[str] | None = None, *, limit: int = MAX_PROMPT_CASES
) -> list[dict[str, Any]]:
    """Return a deterministic, bounded subset; relevant cases rank first."""
    wanted = {str(tag).lower() for tag in (tags or set())}

    def score(case: dict[str, Any]) -> tuple[int, str]:
        overlap = len(wanted.intersection(str(tag).lower() for tag in case["tags"]))
        return (-overlap, str(case["id"]))

    return [dict(case) for case in sorted(load_starter_casebook(), key=score)[:limit]]
