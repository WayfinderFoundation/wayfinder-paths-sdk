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
