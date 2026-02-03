from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from wayfinder_paths.core.engine.manifest import load_strategy_manifest


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict), f"manifest must be a mapping: {path}"
    return data


def _import_entrypoint(entrypoint: str) -> None:
    assert isinstance(entrypoint, str) and entrypoint.strip()
    module_path, symbol = entrypoint.rsplit(".", 1)

    last_exc: Exception | None = None
    for candidate in (module_path, f"wayfinder_paths.{module_path}"):
        try:
            mod = importlib.import_module(candidate)
            if not hasattr(mod, symbol):
                raise AttributeError(f"{candidate} missing {symbol}")
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc

    raise AssertionError(f"Failed to import entrypoint: {entrypoint}") from last_exc


def test_all_adapters_have_manifest_yaml_and_entrypoint_imports():
    adapters_dir = Path(__file__).parent.parent / "adapters"
    if not adapters_dir.exists():
        pytest.skip("Adapters directory not found")

    missing: list[str] = []
    for adapter_dir in sorted(adapters_dir.iterdir()):
        if not adapter_dir.is_dir() or adapter_dir.name.startswith("_"):
            continue
        if not (adapter_dir / "adapter.py").exists():
            continue

        manifest_path = adapter_dir / "manifest.yaml"
        if not manifest_path.exists():
            missing.append(adapter_dir.name)
            continue

        manifest = _load_yaml(manifest_path)
        assert manifest.get("schema_version"), (
            f"Missing schema_version: {manifest_path}"
        )
        entrypoint = manifest.get("entrypoint")
        assert entrypoint, f"Missing entrypoint: {manifest_path}"
        _import_entrypoint(str(entrypoint))

        caps = manifest.get("capabilities")
        assert isinstance(caps, list), f"capabilities must be a list: {manifest_path}"

    if missing:
        pytest.fail(f"Adapters missing manifest.yaml: {', '.join(missing)}")


def test_all_strategies_have_manifest_yaml_and_validate():
    strategies_dir = Path(__file__).parent.parent / "strategies"
    if not strategies_dir.exists():
        pytest.skip("Strategies directory not found")

    missing: list[str] = []
    for strat_dir in sorted(strategies_dir.iterdir()):
        if not strat_dir.is_dir() or strat_dir.name.startswith("_"):
            continue
        if not (strat_dir / "strategy.py").exists():
            continue

        manifest_path = strat_dir / "manifest.yaml"
        if not manifest_path.exists():
            missing.append(strat_dir.name)
            continue

        # Use the canonical validator.
        manifest = load_strategy_manifest(str(manifest_path))
        assert manifest.schema_version
        assert manifest.entrypoint

        _import_entrypoint(manifest.entrypoint)

    if missing:
        pytest.fail(f"Strategies missing manifest.yaml: {', '.join(missing)}")
