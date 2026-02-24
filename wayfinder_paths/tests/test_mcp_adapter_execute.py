from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from wayfinder_paths.mcp.tools.adapter_execute import (
    _extract_mcp_methods,
    _import_entrypoint,
    adapter_execute,
)
from wayfinder_paths.mcp.utils import read_yaml, repo_root


def _discover_mcp_adapters() -> list[tuple[str, dict]]:
    """Find all adapters that declare mcp_methods in their manifest."""
    adapters_root = repo_root() / "wayfinder_paths" / "adapters"
    results = []
    for child in sorted(adapters_root.iterdir()):
        manifest_path = child / "manifest.yaml"
        if not manifest_path.exists():
            continue
        manifest = read_yaml(manifest_path)
        methods = _extract_mcp_methods(manifest)
        if methods:
            results.append((child.name, manifest))
    return results


_MCP_ADAPTERS = _discover_mcp_adapters()


@pytest.mark.parametrize(
    "adapter_name,manifest",
    _MCP_ADAPTERS,
    ids=[name for name, _ in _MCP_ADAPTERS],
)
def test_mcp_methods_exist_on_adapter_class(adapter_name: str, manifest: dict):
    """Every method listed in mcp_methods must exist and be callable on the adapter class."""
    entrypoint = manifest["entrypoint"]
    cls = _import_entrypoint(entrypoint)
    methods = _extract_mcp_methods(manifest)
    for method_name in methods:
        assert hasattr(cls, method_name), (
            f"{adapter_name}: manifest lists '{method_name}' but {cls.__name__} has no such attribute"
        )
        assert callable(getattr(cls, method_name)), (
            f"{adapter_name}: {cls.__name__}.{method_name} is not callable"
        )


def _discover_mcp_adapter_methods() -> list[tuple[str, dict, str]]:
    """Yield (adapter_name, manifest, method_name) for every allowlisted method."""
    results = []
    for name, manifest in _MCP_ADAPTERS:
        for method_name in _extract_mcp_methods(manifest):
            results.append((name, manifest, method_name))
    return results


_MCP_ADAPTER_METHODS = _discover_mcp_adapter_methods()

_DUMMY_WALLET = {
    "address": "0x000000000000000000000000000000000000dEaD",
    "private_key_hex": "0x" + "11" * 32,
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter_name,manifest",
    _MCP_ADAPTERS,
    ids=[name for name, _ in _MCP_ADAPTERS],
)
async def test_adapter_execute_rejects_non_allowlisted_method(
    adapter_name: str,
    manifest: dict,
    tmp_path: Path,
    monkeypatch,  # noqa: ARG001
):
    """Calling a method not in mcp_methods must return not_allowed for every adapter."""
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))
    with patch(
        "wayfinder_paths.mcp.tools.adapter_execute.find_wallet_by_label",
        return_value=_DUMMY_WALLET,
    ):
        out = await adapter_execute(
            adapter=adapter_name,
            method="nonexistent_method_xyz",
            wallet_label="main",
        )
    assert out["ok"] is False
    assert out["error"]["code"] == "not_allowed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter_name,manifest,method_name",
    _MCP_ADAPTER_METHODS,
    ids=[f"{name}.{method}" for name, _, method in _MCP_ADAPTER_METHODS],
)
async def test_adapter_execute_calls_allowlisted_method(
    adapter_name: str,
    manifest: dict,
    method_name: str,
    tmp_path: Path,
    monkeypatch,
):
    """Every allowlisted method must be callable through adapter_execute."""
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))
    cls = _import_entrypoint(manifest["entrypoint"])
    mock_method = AsyncMock(return_value=(True, "mock_ok"))
    with (
        patch(
            "wayfinder_paths.mcp.tools.adapter_execute.find_wallet_by_label",
            return_value=_DUMMY_WALLET,
        ),
        patch.object(cls, method_name, mock_method),
    ):
        out = await adapter_execute(
            adapter=adapter_name,
            method=method_name,
            wallet_label="main",
        )
    assert out["ok"] is True, (
        f"Expected ok=True for {adapter_name}.{method_name}, got: {out}"
    )
    assert out["result"]["adapter"] == adapter_name
    assert out["result"]["method"] == method_name
    assert mock_method.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter_name,manifest",
    _MCP_ADAPTERS,
    ids=[name for name, _ in _MCP_ADAPTERS],
)
async def test_adapter_execute_forbids_sensitive_kwargs(
    adapter_name: str, manifest: dict, tmp_path: Path, monkeypatch
):
    """Forbidden kwargs must be rejected for every adapter."""
    monkeypatch.setenv("WAYFINDER_RUNS_DIR", str(tmp_path / "runs"))
    first_method = next(iter(_extract_mcp_methods(manifest)))
    with patch(
        "wayfinder_paths.mcp.tools.adapter_execute.find_wallet_by_label",
        return_value=_DUMMY_WALLET,
    ):
        out = await adapter_execute(
            adapter=adapter_name,
            method=first_method,
            wallet_label="main",
            kwargs={"signing_callback": "nope", "amount": 1},
        )
    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_request"
