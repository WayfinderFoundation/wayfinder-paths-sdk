from __future__ import annotations

import pytest

from wayfinder_paths.mcp.tools import instance_state


@pytest.mark.asyncio
async def test_visual_create_chart_normalizes_delta_lab_rate_fields(
    monkeypatch,
) -> None:
    monkeypatch.setattr(instance_state, "is_opencode_instance", lambda: True)

    captured: dict[str, object] = {}

    async def fake_upsert(chart: dict[str, object]) -> dict[str, object]:
        captured["chart"] = chart
        return {"chart_workspace": {"charts": [chart], "activeChartId": chart["id"]}}

    monkeypatch.setattr(
        instance_state.INSTANCE_STATE_CLIENT,
        "upsert_workspace_chart",
        fake_upsert,
    )

    result = await instance_state.visual_create_chart(
        chart_id="ena-yield",
        title="ENA yield",
        kind="line",
        series=[
            {
                "id": "pendle-ena",
                "label": "Pendle ENA",
                "source": {
                    "type": "dataset_series",
                    "dataset_id": "delta_lab.asset.pendle",
                },
                "x": "ts",
                "y": "implied_apy",
                "unit": "raw",
                "transforms": [
                    {"type": "filter", "field": "market_id", "op": "eq", "value": 19720}
                ],
            }
        ],
    )

    assert result["ok"] is True
    chart = captured["chart"]
    series = chart["series"][0]  # type: ignore[index]
    assert series["unit"] == "%"
    assert series["transforms"] == [
        {"type": "filter", "field": "market_id", "op": "eq", "value": 19720},
        {"type": "scale", "factor": 100, "unit": "%", "label_suffix": "(%)"},
    ]


@pytest.mark.asyncio
async def test_visual_create_chart_annualizes_funding_to_percent(monkeypatch) -> None:
    monkeypatch.setattr(instance_state, "is_opencode_instance", lambda: True)

    captured: dict[str, object] = {}

    async def fake_upsert(chart: dict[str, object]) -> dict[str, object]:
        captured["chart"] = chart
        return {"chart_workspace": {"charts": [chart], "activeChartId": chart["id"]}}

    monkeypatch.setattr(
        instance_state.INSTANCE_STATE_CLIENT,
        "upsert_workspace_chart",
        fake_upsert,
    )

    await instance_state.visual_create_chart(
        chart_id="ena-funding",
        title="ENA funding",
        kind="line",
        series=[
            {
                "id": "hl-ena",
                "label": "HL ENA funding",
                "source": {
                    "type": "dataset_series",
                    "dataset_id": "delta_lab.asset.funding",
                },
                "x": "ts",
                "y": "funding_rate",
            }
        ],
    )

    chart = captured["chart"]
    series = chart["series"][0]  # type: ignore[index]
    assert series["unit"] == "%"
    assert series["transforms"] == [
        {
            "type": "scale",
            "factor": 876000,
            "unit": "%",
            "label_suffix": "(annualized %)",
        }
    ]
