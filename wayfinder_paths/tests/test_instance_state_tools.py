from __future__ import annotations

import json

import httpx
import pytest

from wayfinder_paths.mcp.tools import instance_state


@pytest.mark.asyncio
async def test_visual_get_frontend_context_passes_include_health(monkeypatch) -> None:
    monkeypatch.setattr(instance_state, "is_opencode_instance", lambda: True)

    captured: dict[str, object] = {}

    async def fake_get_state(*, include_health: bool = False) -> dict[str, object]:
        captured["include_health"] = include_health
        return {
            "frontend_context": {},
            "chart_workspace": {"health": {"status": "ok"}},
        }

    monkeypatch.setattr(
        instance_state.INSTANCE_STATE_CLIENT, "get_state", fake_get_state
    )

    result = await instance_state.visual_get_frontend_context(include_health=True)

    assert result["ok"] is True
    assert captured["include_health"] is True
    assert result["result"]["chart_workspace"]["health"]["status"] == "ok"


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


@pytest.mark.asyncio
async def test_visual_preview_series_returns_compact_summary(monkeypatch) -> None:
    monkeypatch.setattr(instance_state, "is_opencode_instance", lambda: True)

    captured: dict[str, object] = {}

    async def fake_resolve(payload: dict[str, object]) -> dict[str, object]:
        captured["payload"] = payload
        return {
            "series": [
                {
                    "id": "aero_ratio_eth",
                    "label": "AERO ratio ETH",
                    "unit": "ratio",
                    "points": [
                        {"x": "2026-07-01", "y": 1.0},
                        {"x": "2026-07-02", "y": 4.0},
                        {"x": "2026-07-03", "y": 2.0},
                        {"x": "2026-07-04", "y": 3.0},
                    ],
                }
            ]
        }

    monkeypatch.setattr(
        instance_state.INSTANCE_STATE_CLIENT, "resolve_chart_data", fake_resolve
    )

    result = await instance_state.visual_preview_series(
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
        lookback_days=30,
    )

    assert result["ok"] is True
    # The same display normalization as visual_create_chart applies, so the
    # preview matches what the chart would render.
    payload = captured["payload"]
    sent_series = payload["series"][0]  # type: ignore[index]
    assert sent_series["unit"] == "%"
    assert payload["lookback_days"] == 30  # type: ignore[index]

    summary = result["result"]["series"][0]
    assert summary["points"] == 4
    assert summary["unit"] == "ratio"
    assert summary["y_first"] == 1.0
    assert summary["y_last"] == 3.0
    assert summary["y_min"] == 1.0
    assert summary["y_max"] == 4.0
    assert summary["first_x"] == "2026-07-01"
    assert summary["last_x"] == "2026-07-04"
    assert [p["x"] for p in summary["sample_head"]] == [
        "2026-07-01",
        "2026-07-02",
        "2026-07-03",
    ]
    assert [p["x"] for p in summary["sample_tail"]] == ["2026-07-04"]


@pytest.mark.asyncio
async def test_visual_set_chart_indicators_reports_applied_list(monkeypatch) -> None:
    monkeypatch.setattr(instance_state, "is_opencode_instance", lambda: True)

    captured: dict[str, object] = {}

    async def fake_set(chart_id: str, indicators: list[dict[str, object]]):
        captured["chart_id"] = chart_id
        captured["indicators"] = indicators
        return {
            "chart_workspace": {
                "activeChartId": "aero-eth",
                "version": 9,
                "defaultIndicators": {
                    "aero-eth": [
                        {
                            "id": "ema",
                            "name": "Moving Average Exponential",
                            "forceOverlay": True,
                            "inputs": {"length": 21},
                        }
                    ]
                },
            }
        }

    monkeypatch.setattr(
        instance_state.INSTANCE_STATE_CLIENT, "set_chart_indicators", fake_set
    )

    result = await instance_state.visual_set_chart_indicators(
        "aero-eth", [{"name": "ema", "inputs": {"length": 21}}]
    )

    assert result["ok"] is True
    assert captured["chart_id"] == "aero-eth"
    assert result["result"]["indicators"][0]["name"] == "Moving Average Exponential"
    assert result["result"]["chart_workspace"]["version"] == 9


@pytest.mark.asyncio
async def test_visual_create_chart_compacts_response_and_applies_indicators(
    monkeypatch,
) -> None:
    monkeypatch.setattr(instance_state, "is_opencode_instance", lambda: True)

    captured: dict[str, object] = {}

    async def fake_upsert(chart: dict[str, object]) -> dict[str, object]:
        return {
            "frontend_context": {"huge": "state echo"},
            "chart_workspace": {"activeChartId": chart["id"], "version": 12},
            "chart_validation": {
                "chart_id": chart["id"],
                "kind": "line",
                "series": [{"id": "s1", "points": 30, "y_min": 1.0, "y_max": 2.0}],
            },
        }

    async def fake_set(chart_id: str, indicators: list[dict[str, object]]):
        captured["indicator_chart_id"] = chart_id
        captured["indicators"] = indicators
        return {"chart_workspace": {}}

    monkeypatch.setattr(
        instance_state.INSTANCE_STATE_CLIENT, "upsert_workspace_chart", fake_upsert
    )
    monkeypatch.setattr(
        instance_state.INSTANCE_STATE_CLIENT, "set_chart_indicators", fake_set
    )

    result = await instance_state.visual_create_chart(
        chart_id="aero-eth",
        title="AERO/ETH",
        kind="line",
        series=[{"id": "s1", "source": {"type": "inline", "points": []}}],
        indicators=[{"name": "bollinger"}],
    )

    assert result["ok"] is True
    body = result["result"]
    assert "frontend_context" not in body
    assert body["chart"] == {
        "id": "aero-eth",
        "title": "AERO/ETH",
        "kind": "line",
        "series_count": 1,
        "lookback_days": None,
        "limit": None,
    }
    assert body["chart_workspace"] == {"activeChartId": "aero-eth", "version": 12}
    assert body["chart_validation"]["series"][0]["y_max"] == 2.0
    assert body["indicators"] == [{"name": "bollinger"}]
    assert captured["indicator_chart_id"] == "aero-eth"


@pytest.mark.asyncio
async def test_visual_create_chart_surfaces_indicator_failure(monkeypatch) -> None:
    monkeypatch.setattr(instance_state, "is_opencode_instance", lambda: True)

    async def fake_upsert(chart: dict[str, object]) -> dict[str, object]:
        return {"chart_workspace": {"activeChartId": chart["id"], "version": 2}}

    request = httpx.Request("PATCH", "http://backend/chart_workspace")
    response = httpx.Response(
        400,
        json={"error": "unsupported indicator 'ichimoku'; supported: atr, bollinger"},
        request=request,
    )

    async def fake_set(chart_id: str, indicators: list[dict[str, object]]):
        raise httpx.HTTPStatusError("400", request=request, response=response)

    monkeypatch.setattr(
        instance_state.INSTANCE_STATE_CLIENT, "upsert_workspace_chart", fake_upsert
    )
    monkeypatch.setattr(
        instance_state.INSTANCE_STATE_CLIENT, "set_chart_indicators", fake_set
    )

    result = await instance_state.visual_create_chart(
        chart_id="c1",
        title="C1",
        kind="line",
        series=[{"id": "s1", "source": {"type": "inline", "points": []}}],
        indicators=[{"name": "ichimoku"}],
    )

    # Chart creation succeeded; the indicator failure is reported, not fatal.
    assert result["ok"] is True
    assert "unsupported indicator" in result["result"]["indicators_error"]["message"]
    assert "indicators" not in result["result"]


@pytest.mark.asyncio
async def test_visual_import_chart_spec_imports_safe_spec(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(instance_state, "is_opencode_instance", lambda: True)
    monkeypatch.setattr(instance_state, "repo_root", lambda: tmp_path)

    spec_path = tmp_path / ".wayfinder_runs" / "visual_specs" / "virtual.json"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text(
        json.dumps(
            {
                "id": "virtual-yield",
                "title": "VIRTUAL yield",
                "kind": "line",
                "series": [
                    {
                        "id": "moonwell-virtual",
                        "label": "Moonwell VIRTUAL",
                        "source": {
                            "type": "dataset_series",
                            "dataset_id": "delta_lab.asset.lending",
                            "params": {
                                "symbol": "VIRTUAL",
                                "series": "lending",
                                "market_id": 17694,
                                "asset_id": 163,
                            },
                        },
                        "x": "ts",
                        "y": "supply_apr",
                    }
                ],
                "lookback_days": 30,
            }
        )
    )

    captured: dict[str, object] = {}

    async def fake_upsert(chart: dict[str, object]) -> dict[str, object]:
        captured["chart"] = chart
        return {
            "chart_workspace": {"activeChartId": chart["id"], "version": 4},
            "chart_validation": {"series": [{"id": "moonwell-virtual"}]},
        }

    monkeypatch.setattr(
        instance_state.INSTANCE_STATE_CLIENT,
        "upsert_workspace_chart",
        fake_upsert,
    )

    result = await instance_state.visual_import_chart_spec(
        ".wayfinder_runs/visual_specs/virtual.json"
    )

    assert result["ok"] is True
    chart = captured["chart"]
    series = chart["series"][0]  # type: ignore[index]
    assert series["unit"] == "%"
    assert series["transforms"] == [
        {"type": "scale", "factor": 100, "unit": "%", "label_suffix": "(%)"}
    ]
    assert result["result"] == {
        "chart": {
            "id": "virtual-yield",
            "title": "VIRTUAL yield",
            "kind": "line",
            "path": ".wayfinder_runs/visual_specs/virtual.json",
            "series_count": 1,
            "lookback_days": 30,
            "limit": None,
        },
        "chart_workspace": {"activeChartId": "virtual-yield", "version": 4},
        "chart_validation": {"series": [{"id": "moonwell-virtual"}]},
    }


@pytest.mark.asyncio
async def test_visual_import_chart_spec_accepts_symlinked_wayfinder_runs(
    monkeypatch,
    tmp_path,
) -> None:
    """Shells mounts .wayfinder_runs as a symlink out of the repo root
    (/wf/user_vault/scripts). The resolved spec path escapes the root but is
    still under the resolved visual_specs dir — validation must accept it."""
    repo = tmp_path / "sdk"
    repo.mkdir()
    real_runs = tmp_path / "user_vault" / "scripts"
    (real_runs / "visual_specs").mkdir(parents=True)
    (repo / ".wayfinder_runs").symlink_to(real_runs, target_is_directory=True)

    monkeypatch.setattr(instance_state, "is_opencode_instance", lambda: True)
    monkeypatch.setattr(instance_state, "repo_root", lambda: repo)

    spec_path = real_runs / "visual_specs" / "chart.json"
    spec_path.write_text(
        json.dumps({"id": "c1", "title": "C1", "kind": "line", "series": []})
    )

    async def fake_upsert(chart: dict[str, object]) -> dict[str, object]:
        return {"chart_workspace": {"activeChartId": chart["id"], "version": 1}}

    monkeypatch.setattr(
        instance_state.INSTANCE_STATE_CLIENT,
        "upsert_workspace_chart",
        fake_upsert,
    )

    result = await instance_state.visual_import_chart_spec(
        ".wayfinder_runs/visual_specs/chart.json"
    )

    assert result["ok"] is True, result
    assert result["result"]["chart"]["id"] == "c1"


@pytest.mark.asyncio
async def test_visual_add_workspace_chart_series_surfaces_error_body(
    monkeypatch,
) -> None:
    """Backend 400s must carry their body through — 'HTTP 400' with no details
    left the visual agent guessing at validation failures."""
    monkeypatch.setattr(instance_state, "is_opencode_instance", lambda: True)

    request = httpx.Request("POST", "http://backend/chart-series")
    response = httpx.Response(
        400,
        json={"error": "series id already exists on chart"},
        request=request,
    )

    async def fake_add(chart_id: str, series: dict[str, object]):
        raise httpx.HTTPStatusError("400", request=request, response=response)

    monkeypatch.setattr(
        instance_state.INSTANCE_STATE_CLIENT,
        "add_workspace_chart_series",
        fake_add,
    )

    result = await instance_state.visual_add_workspace_chart_series(
        "chart-1", {"id": "s1"}
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "chart_workspace_http_error"
    assert "series id already exists on chart" in result["error"]["message"]
    assert result["error"]["details"] == {"error": "series id already exists on chart"}


@pytest.mark.asyncio
async def test_visual_import_chart_spec_rejects_paths_outside_visual_specs(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(instance_state, "is_opencode_instance", lambda: True)
    monkeypatch.setattr(instance_state, "repo_root", lambda: tmp_path)

    outside_path = tmp_path / ".wayfinder_runs" / "other" / "chart.json"
    outside_path.parent.mkdir(parents=True)
    outside_path.write_text("{}")

    result = await instance_state.visual_import_chart_spec(str(outside_path))

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_chart_spec_path"


@pytest.mark.asyncio
async def test_visual_import_chart_spec_rejects_invalid_json(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(instance_state, "is_opencode_instance", lambda: True)
    monkeypatch.setattr(instance_state, "repo_root", lambda: tmp_path)

    spec_path = tmp_path / ".wayfinder_runs" / "visual_specs" / "bad.json"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("{")

    result = await instance_state.visual_import_chart_spec(
        ".wayfinder_runs/visual_specs/bad.json"
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_chart_spec_json"


@pytest.mark.asyncio
async def test_visual_import_chart_spec_rejects_non_object_json(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(instance_state, "is_opencode_instance", lambda: True)
    monkeypatch.setattr(instance_state, "repo_root", lambda: tmp_path)

    spec_path = tmp_path / ".wayfinder_runs" / "visual_specs" / "array.json"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("[]")

    result = await instance_state.visual_import_chart_spec(
        ".wayfinder_runs/visual_specs/array.json"
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_chart_spec"
