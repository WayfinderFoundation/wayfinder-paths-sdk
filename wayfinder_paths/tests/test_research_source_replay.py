from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from evals.fixtures.research_source_verification.server import ReplayResearchClient
from wayfinder_paths.mcp.tools import research_gateway

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_replay_uses_real_tools_without_live_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ReplayResearchClient()
    live = AsyncMock(side_effect=AssertionError("replay must not use the network"))
    monkeypatch.setattr(client, "_authed_request", live)
    monkeypatch.setattr(research_gateway, "RESEARCH_CLIENT", client)
    fetch = inspect.unwrap(research_gateway.core_web_fetch)
    search = inspect.unwrap(research_gateway.core_web_search)
    result = await fetch(
        urls="https://netnet.example/official-channels,https://fern.example,https://unknown.example"
    )
    assert result["ok"] is True
    assert len(result["result"]["results"]) == 1
    assert "impersonator" in result["result"]["results"][0]["contentExcerpt"]
    assert [s["status"] for s in result["result"]["statuses"]] == [
        "success",
        "unavailable",
        "unavailable",
    ]
    result = await search(query="MOTH dashboard", numResults=10)
    assert {r["url"] for r in result["result"]["results"]} == {
        "https://moth.example",
        "https://marketwire.example/moth-dashboard",
    }
    result = await search(query="MOTH dashboard", includeDomains="moth.example")
    assert [r["url"] for r in result["result"]["results"]] == ["https://moth.example"]
    with pytest.raises(ValueError, match="fallback is disabled"):
        await client._post_gateway("unexpected-route", {})
    live.assert_not_called()
    await client.aclose()


def test_two_scenarios_compare_same_surfaces_and_disable_execution() -> None:
    config = yaml.safe_load(
        (ROOT / "evals/stations/research_source_verification.yaml").read_text()
    )
    assert [q["id"] for q in config["questions"]] == [
        "misleading_reward",
        "trenches_gut_check",
    ]
    assert {v["id"] for v in config["variants"]} == {
        "desktop_before",
        "desktop_after",
        "mobile_before",
        "mobile_after",
    }
    assert config["require_session_metrics"] is True
    runtime = json.loads(
        (ROOT / "evals/fixtures/research_source_verification/runtime.json").read_text()
    )
    assert runtime["permission"] == "deny"
    for agent in ("wayfinder", "wayfinder-mobile"):
        assert runtime["agent"][agent]["permission"] == {
            "*": "deny",
            "wayfinder_core_web_search": "allow",
            "wayfinder_core_web_fetch": "allow",
        }
