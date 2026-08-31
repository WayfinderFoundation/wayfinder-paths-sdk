from __future__ import annotations

from wayfinder_paths.paths import runtime_registry
from wayfinder_paths.paths.runtime_registry import PublishedPackage


def test_runtime_version_uses_installed_version_when_published(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_registry, "installed_package_version", lambda package: "0.11.1"
    )
    monkeypatch.setattr(
        runtime_registry,
        "published_package",
        lambda package: PublishedPackage(
            latest="0.11.1", versions=frozenset({"0.11.0", "0.11.1"})
        ),
    )

    assert runtime_registry.published_runtime_package_version() == "0.11.1"


def test_runtime_version_replaces_unpublished_installed_version(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_registry, "installed_package_version", lambda package: "0.11.1"
    )
    monkeypatch.setattr(
        runtime_registry,
        "published_package",
        lambda package: PublishedPackage(
            latest="0.11.0", versions=frozenset({"0.11.0"})
        ),
    )

    assert runtime_registry.published_runtime_package_version() == "0.11.0"


def test_runtime_version_uses_known_release_when_registry_is_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runtime_registry, "installed_package_version", lambda package: "0.11.1"
    )
    monkeypatch.setattr(runtime_registry, "published_package", lambda package: None)

    assert runtime_registry.published_runtime_package_version() == "0.11.0"


def test_versionless_runtime_keeps_installed_version(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_registry, "installed_package_version", lambda package: "0.11.1"
    )

    assert runtime_registry.installed_runtime_package_version() == "0.11.1"
