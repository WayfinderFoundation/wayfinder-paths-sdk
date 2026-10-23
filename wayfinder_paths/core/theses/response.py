"""Select and decode portfolio responses without changing their contents."""

import json
from typing import Any


def select_response_message(
    messages: list[dict[str, Any]], message_id: str | None = None
) -> dict[str, Any]:
    """Follow native compaction only when the requested turn has no final answer.

    A completed direct answer wins over unsolicited post-answer continuations.
    Summaries and responses to other real user turns can never finish this turn.
    """
    compactions = set()
    continuations = set()
    requests = []
    for index, message in enumerate(messages):
        info = message["info"]
        if info.get("role") != "user":
            continue
        parts = message.get("parts", [])
        if any(p.get("type") == "compaction" for p in parts):
            compactions.add(info["id"])
        elif any(
            p.get("synthetic") and p.get("metadata", {}).get("compaction_continue")
            for p in parts
        ):
            continuations.add(info["id"])
        else:
            requests.append((index, info["id"]))

    selected = next(
        (
            (i, key)
            for i, key in reversed(requests)
            if message_id is None or key == message_id
        ),
        None,
    )
    start, parent_id = selected if selected is not None else (-1, message_id)
    end = (
        next((i for i, _ in requests if i > start), len(messages))
        if selected is not None
        else len(messages)
    )
    current = messages[start + 1 : end]
    parents = {parent_id} | {
        m["info"]["id"]
        for m in current
        if selected is not None and m["info"].get("id") in continuations
    }
    answers = [
        m
        for m in current
        if m["info"].get("role") == "assistant"
        and not m["info"].get("summary")
        and m["info"].get("agent") != "compaction"
        and m["info"].get("mode") != "compaction"
        and m["info"].get("parentID") not in compactions
        and (parent_id is None or m["info"].get("parentID") in parents)
    ]
    direct = next(
        (
            m
            for m in reversed(answers)
            if m["info"].get("parentID") not in continuations
        ),
        {},
    )
    info = direct.get("info", {})
    if (
        info.get("error")
        or info.get("finish") in {"stop", "length"}
        or isinstance(info.get("structured"), dict)
    ):
        return direct
    return answers[-1] if answers else {}


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
