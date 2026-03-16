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
_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SKILL_MAX_NAME_LENGTH = 64
_SKILL_MAX_DESCRIPTION_LENGTH = 1024
_SKILL_SOURCES = {"generated", "provided"}


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


def _ensure_object(
    raw_obj: Any, *, name: str, required: bool = False
) -> dict[str, Any] | None:
    if raw_obj is None:
        if required:
            raise PackManifestError(f"{name} must be an object")
        return None
    if not isinstance(raw_obj, dict):
        raise PackManifestError(f"{name} must be an object")
    return raw_obj


def _parse_string_list(raw_obj: Any, *, name: str) -> list[str]:
    if raw_obj is None:
        return []
    if not isinstance(raw_obj, list):
        raise PackManifestError(f"{name} must be a list")
    values = [str(item).strip() for item in raw_obj if str(item).strip()]
    return values


def _parse_bool(raw_obj: Any, *, name: str) -> bool | None:
    if raw_obj is None:
        return None
    if not isinstance(raw_obj, bool):
        raise PackManifestError(f"{name} must be a boolean")
    return raw_obj


@dataclass(frozen=True)
class PackAppletConfig:
    build_dir: str
    manifest_path: str


@dataclass(frozen=True)
class PackSkillClaudeConfig:
    disable_model_invocation: bool | None
    allowed_tools: list[str]


@dataclass(frozen=True)
class PackSkillCodexConfig:
    allow_implicit_invocation: bool | None


@dataclass(frozen=True)
class PackSkillOpenClawConfig:
    user_invocable: bool | None
    requires: dict[str, Any]
    install: list[dict[str, Any]]


@dataclass(frozen=True)
class PackSkillPortableConfig:
    python: str | None
    package: str | None


@dataclass(frozen=True)
class PackSkillConfig:
    enabled: bool
    source: str
    name: str
    description: str
    instructions_path: str | None
    claude: PackSkillClaudeConfig | None
    codex: PackSkillCodexConfig | None
    openclaw: PackSkillOpenClawConfig | None
    portable: PackSkillPortableConfig | None
    raw: dict[str, Any]


def _parse_claude_skill_config(raw_obj: Any) -> PackSkillClaudeConfig | None:
    obj = _ensure_object(raw_obj, name="wfpack.yaml skill.claude")
    if obj is None:
        return None
    return PackSkillClaudeConfig(
        disable_model_invocation=_parse_bool(
            obj.get("disable_model_invocation"),
            name="wfpack.yaml skill.claude.disable_model_invocation",
        ),
        allowed_tools=_parse_string_list(
            obj.get("allowed_tools"),
            name="wfpack.yaml skill.claude.allowed_tools",
        ),
    )


def _parse_codex_skill_config(raw_obj: Any) -> PackSkillCodexConfig | None:
    obj = _ensure_object(raw_obj, name="wfpack.yaml skill.codex")
    if obj is None:
        return None
    return PackSkillCodexConfig(
        allow_implicit_invocation=_parse_bool(
            obj.get("allow_implicit_invocation"),
            name="wfpack.yaml skill.codex.allow_implicit_invocation",
        )
    )


def _parse_openclaw_skill_config(raw_obj: Any) -> PackSkillOpenClawConfig | None:
    obj = _ensure_object(raw_obj, name="wfpack.yaml skill.openclaw")
    if obj is None:
        return None

    requires = (
        _ensure_object(
            obj.get("requires"),
            name="wfpack.yaml skill.openclaw.requires",
        )
        or {}
    )

    install_raw = obj.get("install") or []
    if not isinstance(install_raw, list):
        raise PackManifestError("wfpack.yaml skill.openclaw.install must be a list")
    install: list[dict[str, Any]] = []
    for idx, item in enumerate(install_raw):
        if not isinstance(item, dict):
            raise PackManifestError(
                f"wfpack.yaml skill.openclaw.install[{idx}] must be an object"
            )
        install.append(dict(item))

    return PackSkillOpenClawConfig(
        user_invocable=_parse_bool(
            obj.get("user_invocable"),
            name="wfpack.yaml skill.openclaw.user_invocable",
        ),
        requires=dict(requires),
        install=install,
    )


def _parse_portable_skill_config(raw_obj: Any) -> PackSkillPortableConfig | None:
    obj = _ensure_object(raw_obj, name="wfpack.yaml skill.portable")
    if obj is None:
        return None
    python = str(obj.get("python", "")).strip() or None
    package = str(obj.get("package", "")).strip() or None
    return PackSkillPortableConfig(python=python, package=package)


def _parse_skill_config(raw_obj: Any) -> PackSkillConfig | None:
    obj = _ensure_object(raw_obj, name="wfpack.yaml skill")
    if obj is None:
        return None

    enabled = _parse_bool(obj.get("enabled"), name="wfpack.yaml skill.enabled")
    enabled = bool(enabled)

    source = str(obj.get("source", "")).strip()
    name = str(obj.get("name", "")).strip()
    description = str(obj.get("description", "")).strip()
    instructions_path = str(obj.get("instructions", "")).strip() or None

    if enabled:
        if source not in _SKILL_SOURCES:
            raise PackManifestError(
                "wfpack.yaml skill.source must be one of: generated, provided"
            )
        if not name:
            raise PackManifestError("wfpack.yaml skill.name is required")
        if len(name) > _SKILL_MAX_NAME_LENGTH or not _SKILL_NAME_RE.fullmatch(name):
            raise PackManifestError(
                "wfpack.yaml skill.name must be lowercase letters/numbers/hyphens and <= 64 chars"
            )
        if not description:
            raise PackManifestError("wfpack.yaml skill.description is required")
        if len(description) > _SKILL_MAX_DESCRIPTION_LENGTH:
            raise PackManifestError(
                "wfpack.yaml skill.description must be <= 1024 chars"
            )
        if source == "generated" and not instructions_path:
            raise PackManifestError(
                "wfpack.yaml skill.instructions is required for generated skills"
            )

    elif source and source not in _SKILL_SOURCES:
        raise PackManifestError(
            "wfpack.yaml skill.source must be one of: generated, provided"
        )

    return PackSkillConfig(
        enabled=enabled,
        source=source or "generated",
        name=name,
        description=description,
        instructions_path=instructions_path,
        claude=_parse_claude_skill_config(obj.get("claude")),
        codex=_parse_codex_skill_config(obj.get("codex")),
        openclaw=_parse_openclaw_skill_config(obj.get("openclaw")),
        portable=_parse_portable_skill_config(obj.get("portable")),
        raw=dict(obj),
    )


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
    skill: PackSkillConfig | None
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

        skill = _parse_skill_config(raw_obj.get("skill"))

        return PackManifest(
            schema_version=schema_version,
            slug=slug,
            name=name,
            version=version,
            summary=summary,
            primary_kind=primary_kind,
            tags=tags,
            applet=applet,
            skill=skill,
            raw=raw_obj,
        )
