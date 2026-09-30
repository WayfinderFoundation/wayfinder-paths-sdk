"""Decode a portfolio's presentation wrapper without changing its contents."""

import json
from typing import Any


def parse_proposal_response(text: str) -> dict[str, Any]:
    """Allow leading commentary and one JSON fence; reject partial/extra payloads.

    This only decodes data. The caller must still validate the complete proposal
    and observed market evidence before publishing it.
    """
    text = text.strip()
    start = text.find("{")
    prefix = text[:start]
    error = "Return one complete JSON object, optionally in a single JSON code fence"
    if start < 0 or prefix.lstrip().startswith("["):
        raise ValueError(error)
    fences = prefix.split("```")
    if len(fences) == 2:
        if fences[1].strip() not in {"", "json"} or not text.endswith("```"):
            raise ValueError(error)
        text = text[:-3].rstrip()
    elif len(fences) > 2:
        raise ValueError(error)
    proposal = json.loads(text[start:])
    if not isinstance(proposal, dict):
        raise ValueError(error)
    return proposal
