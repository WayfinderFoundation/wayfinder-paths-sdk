from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from importlib import metadata as importlib_metadata
from typing import Any
from urllib import error, parse, request

_PYPI_PROJECT_URL = "https://pypi.org/pypi/{package}/json"
_PYPI_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class PublishedPackage:
    latest: str
    versions: frozenset[str]


@cache
def published_package(package: str) -> PublishedPackage | None:
    url = _PYPI_PROJECT_URL.format(package=parse.quote(package, safe=""))
    req = request.Request(url, headers={"User-Agent": "wayfinder-paths/doctor"})
    try:
        with request.urlopen(req, timeout=_PYPI_TIMEOUT_SECONDS) as response:
            payload: Any = json.load(response)
    except (error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None

    if not isinstance(payload, dict):
        return None
    info = payload.get("info")
    releases = payload.get("releases")
    if not isinstance(info, dict) or not isinstance(releases, dict):
        return None

    latest = str(info.get("version") or "").strip()
    versions = frozenset(
        str(version)
        for version, files in releases.items()
        if str(version).strip() and isinstance(files, list) and files
    )
    if not latest or latest not in versions:
        return None
    return PublishedPackage(latest=latest, versions=versions)


def installed_package_version(package: str) -> str | None:
    try:
        return importlib_metadata.version(package)
    except importlib_metadata.PackageNotFoundError:
        return None


def installed_runtime_package_version(package: str = "wayfinder-paths") -> str:
    return installed_package_version(package) or "0.0.0"


def scaffold_runtime_package_version(package: str = "wayfinder-paths") -> str:
    """Prefer the checkout version only when users can install it from PyPI."""
    installed = installed_runtime_package_version(package)
    published = published_package(package)
    if published is not None and installed not in published.versions:
        return published.latest
    return installed
