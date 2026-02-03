from __future__ import annotations

from pathlib import Path

import pytest

from wayfinder_paths.mcp.tools.run_script import run_script


@pytest.mark.asyncio
async def test_run_script_rejects_outside_runs_dir(tmp_path: Path, monkeypatch):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(runs_root))
    monkeypatch.setenv("WAYFINDER_MCP_STATE_PATH", str(tmp_path / "mcp.sqlite3"))

    outside = tmp_path / "outside.py"
    outside.write_text("print('nope')\n")

    out = await run_script(script_path=str(outside))
    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_run_script_executes(tmp_path: Path, monkeypatch):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(runs_root))
    monkeypatch.setenv("WAYFINDER_MCP_STATE_PATH", str(tmp_path / "mcp.sqlite3"))

    script = runs_root / "hello.py"
    script.write_text("import os\nprint('PWD=' + os.getcwd())\n")

    out1 = await run_script(script_path=str(script), timeout_s=30)
    assert out1["ok"] is True
    assert out1["result"]["exit_code"] == 0
