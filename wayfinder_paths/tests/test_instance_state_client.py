from __future__ import annotations

import pytest

from wayfinder_paths.core.clients.InstanceStateClient import InstanceStateClient


@pytest.mark.asyncio
async def test_add_annotation_resolves_current_default_chart_alias(monkeypatch) -> None:
    client = InstanceStateClient()
    captured: dict[str, object] = {}

    async def fake_get_state() -> dict:
        return {
            "frontend_context": {
                "chart": {
                    "id": "hl-perp-zec",
                    "market_id": "hl-perp-zec",
                    "symbol": "ZEC-USDC",
                    "feed_id": "ZEC",
                }
            },
            "chart_workspace": {
                "version": 7,
                "activeChartId": None,
                "charts": [],
                "defaultAnnotations": {},
            },
        }

    async def fake_patch_chart_workspace(workspace: dict) -> dict:
        captured["workspace"] = workspace
        return {"chart_workspace": workspace}

    monkeypatch.setattr(client, "get_state", fake_get_state)
    monkeypatch.setattr(client, "patch_chart_workspace", fake_patch_chart_workspace)

    await client.add_workspace_chart_annotation(
        "ZEC",
        "vertical_line",
        {"time": "2026-05-21", "label": "SEC closes investigation"},
        annotation_id="zec-sec-close",
    )

    workspace = captured["workspace"]
    assert workspace["version"] == 8
    assert workspace["defaultAnnotations"] == {
        "hl-perp-zec": [
            {
                "id": "zec-sec-close",
                "type": "annotation",
                "annotation": {
                    "type": "vertical_line",
                    "config": {
                        "time": "2026-05-21",
                        "label": "SEC closes investigation",
                    },
                },
            }
        ]
    }


@pytest.mark.asyncio
async def test_add_overlay_keeps_workspace_chart_id(monkeypatch) -> None:
    client = InstanceStateClient()
    captured: dict[str, object] = {}

    async def fake_get_state() -> dict:
        return {
            "frontend_context": {"chart": {"id": "hl-perp-zec", "symbol": "ZEC-USDC"}},
            "chart_workspace": {
                "version": 2,
                "activeChartId": "zec_events",
                "charts": [{"id": "zec_events", "overlays": []}],
                "defaultAnnotations": {},
            },
        }

    async def fake_patch_chart_workspace(workspace: dict) -> dict:
        captured["workspace"] = workspace
        return {"chart_workspace": workspace}

    monkeypatch.setattr(client, "get_state", fake_get_state)
    monkeypatch.setattr(client, "patch_chart_workspace", fake_patch_chart_workspace)

    overlay = {"id": "event", "type": "event_markers", "data": []}
    await client.add_workspace_chart_overlay("zec_events", overlay)

    workspace = captured["workspace"]
    assert workspace["charts"][0]["overlays"] == [overlay]
    assert workspace["defaultAnnotations"] == {}


@pytest.mark.asyncio
async def test_event_markers_overlay_accepts_legacy_markers_key(monkeypatch) -> None:
    client = InstanceStateClient()
    captured: dict[str, object] = {}

    async def fake_get_state() -> dict:
        return {
            "frontend_context": {
                "chart": {
                    "id": "hl-perp-zec",
                    "market_id": "hl-perp-zec",
                    "symbol": "ZEC-USDC",
                    "feed_id": "ZEC",
                }
            },
            "chart_workspace": {
                "version": 4,
                "activeChartId": None,
                "charts": [],
                "defaultAnnotations": {},
            },
        }

    async def fake_patch_chart_workspace(workspace: dict) -> dict:
        captured["workspace"] = workspace
        return {"chart_workspace": workspace}

    monkeypatch.setattr(client, "get_state", fake_get_state)
    monkeypatch.setattr(client, "patch_chart_workspace", fake_patch_chart_workspace)

    await client.add_workspace_chart_overlay(
        "hl-perp-zec",
        {
            "id": "zec-catalysts",
            "type": "event_markers",
            "markers": [
                {
                    "time": "2026-05-21T06:00:00Z",
                    "price": 690,
                    "text": "ZEC hits $690",
                }
            ],
        },
    )

    annotations = captured["workspace"]["defaultAnnotations"]  # type: ignore[index]
    assert annotations["hl-perp-zec"] == [
        {
            "id": "zec-catalysts",
            "type": "event_markers",
            "data": [
                {
                    "time": "2026-05-21T06:00:00Z",
                    "price": 690,
                    "text": "ZEC hits $690",
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_set_chart_indicators_resolves_live_chart_alias(monkeypatch) -> None:
    client = InstanceStateClient()
    captured: dict[str, object] = {}

    async def fake_get_state() -> dict:
        return {
            "frontend_context": {
                "chart": {
                    "id": "hl-perp-zec",
                    "market_id": "hl-perp-zec",
                    "symbol": "ZEC-USDC",
                    "feed_id": "ZEC",
                }
            },
            "chart_workspace": {
                "version": 3,
                "activeChartId": None,
                "charts": [],
                "defaultAnnotations": {},
            },
        }

    async def fake_patch_chart_workspace(workspace: dict) -> dict:
        captured["workspace"] = workspace
        return {"chart_workspace": workspace}

    monkeypatch.setattr(client, "get_state", fake_get_state)
    monkeypatch.setattr(client, "patch_chart_workspace", fake_patch_chart_workspace)

    indicators = [{"name": "ema", "inputs": {"length": 21}}, {"name": "rsi"}]
    await client.set_chart_indicators("ZEC", indicators)

    workspace = captured["workspace"]
    assert workspace["version"] == 4
    assert workspace["defaultIndicators"] == {"hl-perp-zec": indicators}


@pytest.mark.asyncio
async def test_set_chart_indicators_replaces_and_clears(monkeypatch) -> None:
    client = InstanceStateClient()
    captured: dict[str, object] = {}

    async def fake_get_state() -> dict:
        return {
            "frontend_context": {"chart": {"id": "hl-perp-btc"}},
            "chart_workspace": {
                "version": 5,
                "activeChartId": "aero-eth",
                "charts": [{"id": "aero-eth", "series": []}],
                "defaultAnnotations": {},
                "defaultIndicators": {"aero-eth": [{"name": "sma"}]},
            },
        }

    async def fake_patch_chart_workspace(workspace: dict) -> dict:
        captured["workspace"] = workspace
        return {"chart_workspace": workspace}

    monkeypatch.setattr(client, "get_state", fake_get_state)
    monkeypatch.setattr(client, "patch_chart_workspace", fake_patch_chart_workspace)

    await client.set_chart_indicators("aero-eth", [{"name": "bollinger"}])
    assert captured["workspace"]["defaultIndicators"] == {  # type: ignore[index]
        "aero-eth": [{"name": "bollinger"}]
    }

    await client.set_chart_indicators("aero-eth", [])
    assert captured["workspace"]["defaultIndicators"] == {}  # type: ignore[index]
