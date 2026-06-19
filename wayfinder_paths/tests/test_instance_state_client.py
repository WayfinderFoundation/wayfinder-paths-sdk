from __future__ import annotations

from typing import Any

import pytest

from wayfinder_paths.core.clients.InstanceStateClient import InstanceStateClient


def _state(workspace: dict[str, Any], chart: dict[str, Any] | None = None) -> dict:
    return {
        "frontend_context": {
            "chart": chart
            or {
                "id": "hl-perp-zec",
                "market_id": "hl-perp-zec",
                "symbol": "ZEC-USDC",
                "feed_id": "ZEC",
            }
        },
        "chart_workspace": workspace,
    }


def _stub_state(
    monkeypatch: pytest.MonkeyPatch,
    client: InstanceStateClient,
    state: dict[str, Any],
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def fake_get_state() -> dict:
        return state

    async def fake_patch_chart_workspace(workspace: dict) -> dict:
        captured["workspace"] = workspace
        return {"chart_workspace": workspace}

    monkeypatch.setattr(client, "get_state", fake_get_state)
    monkeypatch.setattr(client, "patch_chart_workspace", fake_patch_chart_workspace)
    return captured


@pytest.mark.asyncio
async def test_add_annotation_resolves_current_default_chart_alias(monkeypatch) -> None:
    client = InstanceStateClient()
    captured = _stub_state(
        monkeypatch,
        client,
        _state(
            {
                "version": 7,
                "activeChartId": None,
                "charts": [],
                "defaultAnnotations": {},
            }
        ),
    )

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
    captured = _stub_state(
        monkeypatch,
        client,
        _state(
            {
                "version": 2,
                "activeChartId": "zec_events",
                "charts": [{"id": "zec_events", "overlays": []}],
                "defaultAnnotations": {},
            }
        ),
    )

    overlay = {"id": "event", "type": "event_markers", "data": []}
    await client.add_workspace_chart_overlay("zec_events", overlay)

    workspace = captured["workspace"]
    assert workspace["charts"][0]["overlays"] == [overlay]
    assert workspace["defaultAnnotations"] == {}


@pytest.mark.asyncio
async def test_add_overlay_replaces_matching_overlay_id(monkeypatch) -> None:
    client = InstanceStateClient()
    captured = _stub_state(
        monkeypatch,
        client,
        _state(
            {
                "version": 2,
                "activeChartId": "zec_events",
                "charts": [
                    {
                        "id": "zec_events",
                        "overlays": [
                            {"id": "event", "type": "event_markers", "data": []}
                        ],
                    }
                ],
                "defaultAnnotations": {},
            }
        ),
    )

    overlay = {
        "id": "event",
        "type": "event_markers",
        "data": [{"time": "2026-05-21T06:00:00Z", "label": "NU7 testnet"}],
    }
    await client.add_workspace_chart_overlay("zec_events", overlay)

    workspace = captured["workspace"]
    assert workspace["version"] == 3
    assert workspace["charts"][0]["overlays"] == [overlay]


@pytest.mark.asyncio
async def test_add_overlay_without_id_still_appends(monkeypatch) -> None:
    client = InstanceStateClient()
    captured = _stub_state(
        monkeypatch,
        client,
        _state(
            {
                "version": 2,
                "activeChartId": "zec_events",
                "charts": [
                    {
                        "id": "zec_events",
                        "overlays": [{"type": "event_markers", "data": []}],
                    }
                ],
                "defaultAnnotations": {},
            }
        ),
    )

    overlay = {"type": "event_markers", "data": []}
    await client.add_workspace_chart_overlay("zec_events", overlay)

    workspace = captured["workspace"]
    assert workspace["charts"][0]["overlays"] == [
        {"type": "event_markers", "data": []},
        overlay,
    ]


@pytest.mark.asyncio
async def test_event_markers_overlay_accepts_legacy_markers_key(monkeypatch) -> None:
    client = InstanceStateClient()
    captured = _stub_state(
        monkeypatch,
        client,
        _state(
            {
                "version": 4,
                "activeChartId": None,
                "charts": [],
                "defaultAnnotations": {},
            }
        ),
    )

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

    annotations = captured["workspace"]["defaultAnnotations"]
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
async def test_event_markers_overlay_returns_timestamp_warning(monkeypatch) -> None:
    client = InstanceStateClient()
    _stub_state(
        monkeypatch,
        client,
        _state(
            {
                "version": 4,
                "activeChartId": None,
                "charts": [],
                "defaultAnnotations": {},
            }
        ),
    )

    result = await client.add_workspace_chart_overlay(
        "hl-perp-zec",
        {
            "id": "zec-catalysts",
            "type": "event_markers",
            "data": [{"time": 1747807200, "label": "May 21: high"}],
        },
    )

    assert "warnings" in result
    assert "numeric Unix timestamps" in result["warnings"][0]
    assert "prefer ISO-8601" in result["warnings"][0]
