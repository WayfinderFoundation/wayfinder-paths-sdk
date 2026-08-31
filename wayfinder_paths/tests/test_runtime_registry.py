from __future__ import annotations

import io
import json

from wayfinder_paths.paths import runtime_registry
from wayfinder_paths.paths.runtime_registry import PublishedPackage


def test_published_package_reads_available_release_versions(monkeypatch) -> None:
    payload = {
        "info": {"version": "0.11.1"},
        "releases": {
            "0.11.0": [{"filename": "wayfinder_paths-0.11.0.whl"}],
            "0.11.1": [{"filename": "wayfinder_paths-0.11.1.whl"}],
            "0.12.0": [],
        },
    }
    monkeypatch.setattr(
        runtime_registry.request,
        "urlopen",
        lambda request, timeout: io.BytesIO(json.dumps(payload).encode()),
    )
    runtime_registry.published_package.cache_clear()

    try:
        package = runtime_registry.published_package("wayfinder-paths")
    finally:
        runtime_registry.published_package.cache_clear()

    assert package == PublishedPackage(
        latest="0.11.1",
        versions=frozenset({"0.11.0", "0.11.1"}),
    )


def test_published_package_returns_none_when_registry_is_unavailable(
    monkeypatch,
) -> None:
    def unavailable(*args, **kwargs):
        raise runtime_registry.error.URLError("offline")

    monkeypatch.setattr(runtime_registry.request, "urlopen", unavailable)
    runtime_registry.published_package.cache_clear()

    try:
        package = runtime_registry.published_package("wayfinder-paths")
    finally:
        runtime_registry.published_package.cache_clear()

    assert package is None


def test_runtime_version_uses_installed_version(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_registry, "installed_package_version", lambda package: "0.11.1"
    )

    assert runtime_registry.installed_runtime_package_version() == "0.11.1"


def test_runtime_version_has_a_deterministic_missing_package_fallback(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runtime_registry, "installed_package_version", lambda package: None
    )

    assert runtime_registry.installed_runtime_package_version() == "0.0.0"
