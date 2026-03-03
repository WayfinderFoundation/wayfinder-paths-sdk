from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from packaging.version import InvalidVersion, Version


class PackManifestError(Exception):
    pass


_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    value = re.sub(r"-{2,}", "-", value)
    return value


def _is_valid_version(version: str) -> bool:
    if not version:
        return False
    if len(version) > 32:
        return False
    try:
        Version(version)
        return True
    except InvalidVersion:
        return bool(_SEMVER_RE.fullmatch(version))


@dataclass(frozen=True)
class PackAppletConfig:
    build_dir: str
    manifest_path: str


@dataclass(frozen=True)
class PackManifest:
    schema_version: str
    slug: str
    name: str
    version: str
    summary: str
    primary_kind: str
    tags: list[str]
    applet: PackAppletConfig | None
    raw: dict[str, Any]

    @staticmethod
    def load(path: Path) -> PackManifest:
        try:
            raw_obj = yaml.safe_load(path.read_text()) or {}
        except Exception as exc:
            raise PackManifestError(f"Failed to parse {path.name}") from exc

        if not isinstance(raw_obj, dict):
            raise PackManifestError(f"{path.name} must be an object")

        schema_version = str(raw_obj.get("schema_version", "")).strip() or "0.1"

        slug = str(raw_obj.get("slug", "")).strip()
        if not slug:
            raise PackManifestError("wfpack.yaml missing required field: slug")
        if _slugify(slug) != slug or not _SLUG_RE.fullmatch(slug):
            raise PackManifestError("wfpack.yaml slug must be URL-safe (slugified)")

        name = str(raw_obj.get("name", "")).strip()
        if not name:
            raise PackManifestError("wfpack.yaml missing required field: name")

        version = str(raw_obj.get("version", "")).strip()
        if not version:
            raise PackManifestError("wfpack.yaml missing required field: version")
        if not _is_valid_version(version):
            raise PackManifestError(
                "wfpack.yaml version must be a valid semver/PEP 440 version string"
            )

        summary = str(raw_obj.get("summary", "")).strip()
        primary_kind = str(raw_obj.get("primary_kind", "bundle")).strip() or "bundle"

        tags_raw = raw_obj.get("tags", []) or []
        if not isinstance(tags_raw, list):
            raise PackManifestError("wfpack.yaml tags must be a list")
        tags = [str(t).strip() for t in tags_raw if str(t).strip()]

        applet_obj = raw_obj.get("applet") or None
        applet: PackAppletConfig | None = None
        if applet_obj is not None:
            if not isinstance(applet_obj, dict):
                raise PackManifestError("wfpack.yaml applet must be an object")
            build_dir = str(applet_obj.get("build_dir", "")).strip()
            if not build_dir:
                raise PackManifestError(
                    "wfpack.yaml applet.build_dir is required when applet is present"
                )
            manifest_path = str(
                applet_obj.get("manifest", "applet/applet.manifest.json")
            ).strip()
            applet = PackAppletConfig(build_dir=build_dir, manifest_path=manifest_path)

        return PackManifest(
            schema_version=schema_version,
            slug=slug,
            name=name,
            version=version,
            summary=summary,
            primary_kind=primary_kind,
            tags=tags,
            applet=applet,
            raw=raw_obj,
        )
