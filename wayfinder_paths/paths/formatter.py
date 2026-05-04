from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.paths.manifest import PathManifest, PathManifestError
from wayfinder_paths.paths.renderer import PathSkillRenderError, render_skill_exports


class PathFormatError(Exception):
    pass


@dataclass(frozen=True)
class PathFormatReport:
    changed_files: list[str]


def _write_if_changed(path: Path, content: str) -> bool:
    normalized = content.rstrip() + "\n"
    current = path.read_text(encoding="utf-8") if path.exists() else None
    if current == normalized:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normalized, encoding="utf-8")
    return True


def _skill_dict(manifest: PathManifest) -> dict[str, Any] | None:
    skill = manifest.skill
    if not skill:
        return None

    data: dict[str, Any] = {
        "enabled": skill.enabled,
        "source": skill.source,
    }
    if skill.enabled:
        data["name"] = skill.name
        data["description"] = skill.description
        if skill.source == "generated" and skill.instructions_path:
            data["instructions"] = skill.instructions_path

    if skill.claude:
        claude: dict[str, Any] = {}
        if skill.claude.disable_model_invocation is not None:
            claude["disable_model_invocation"] = skill.claude.disable_model_invocation
        if skill.claude.allowed_tools:
            claude["allowed_tools"] = skill.claude.allowed_tools
        if claude:
            data["claude"] = claude

    if skill.codex:
        codex: dict[str, Any] = {}
        if skill.codex.allow_implicit_invocation is not None:
            codex["allow_implicit_invocation"] = skill.codex.allow_implicit_invocation
        if codex:
            data["codex"] = codex

    if skill.openclaw:
        openclaw: dict[str, Any] = {}
        if skill.openclaw.user_invocable is not None:
            openclaw["user_invocable"] = skill.openclaw.user_invocable
        if skill.openclaw.disable_model_invocation is not None:
            openclaw["disable_model_invocation"] = (
                skill.openclaw.disable_model_invocation
            )
        if skill.openclaw.requires:
            openclaw["requires"] = skill.openclaw.requires
        if skill.openclaw.install:
            openclaw["install"] = skill.openclaw.install
        if openclaw:
            data["openclaw"] = openclaw

    if skill.runtime:
        runtime: dict[str, Any] = {}
        if skill.runtime.mode:
            runtime["mode"] = skill.runtime.mode
        if skill.runtime.package:
            runtime["package"] = skill.runtime.package
        if skill.runtime.version:
            runtime["version"] = skill.runtime.version
        if skill.runtime.python:
            runtime["python"] = skill.runtime.python
        if skill.runtime.component:
            runtime["component"] = skill.runtime.component
        if skill.runtime.bootstrap:
            runtime["bootstrap"] = skill.runtime.bootstrap
        if skill.runtime.fallback_bootstrap:
            runtime["fallback_bootstrap"] = skill.runtime.fallback_bootstrap
        if skill.runtime.prefer_existing_runtime is not None:
            runtime["prefer_existing_runtime"] = skill.runtime.prefer_existing_runtime
        if skill.runtime.require_api_key is not None:
            runtime["require_api_key"] = skill.runtime.require_api_key
        if skill.runtime.api_key_env:
            runtime["api_key_env"] = skill.runtime.api_key_env
        if skill.runtime.config_path_env:
            runtime["config_path_env"] = skill.runtime.config_path_env
        if runtime:
            data["runtime"] = runtime
    elif skill.portable:
        portable: dict[str, Any] = {}
        if skill.portable.python:
            portable["python"] = skill.portable.python
        if skill.portable.package:
            portable["package"] = skill.portable.package
        if portable:
            data["portable"] = portable

    return data


def _pipeline_dict(manifest: PathManifest) -> dict[str, Any] | None:
    pipeline = manifest.pipeline
    if not pipeline:
        return None
    data: dict[str, Any] = {}
    if pipeline.archetype:
        data["archetype"] = pipeline.archetype
    if pipeline.graph_path:
        data["graph"] = pipeline.graph_path
    data["artifacts_dir"] = pipeline.artifacts_dir
    if pipeline.entry_command:
        data["entry_command"] = pipeline.entry_command
    data["primary_hosts"] = list(pipeline.primary_hosts)
    if pipeline.output_contract:
        data["output_contract"] = list(pipeline.output_contract)
    return data


def _inputs_dict(manifest: PathManifest) -> dict[str, Any] | None:
    if not manifest.inputs:
        return None
    slots: dict[str, Any] = {}
    for slot in manifest.inputs:
        slot_data: dict[str, Any] = {
            "type": slot.slot_type,
            "path": slot.path,
            "required": slot.required,
        }
        if slot.schema:
            slot_data["schema"] = slot.schema
        if slot.description:
            slot_data["description"] = slot.description
        slots[slot.name] = slot_data
    return {"slots": slots}


def _agents_list(manifest: PathManifest) -> list[dict[str, Any]] | None:
    if not manifest.agents:
        return None
    agents: list[dict[str, Any]] = []
    for agent in manifest.agents:
        entry: dict[str, Any] = {
            "id": agent.agent_id,
            "phase": agent.phase,
            "description": agent.description,
            "tools": list(agent.tools),
            "output": agent.output,
        }
        if agent.host_mode:
            entry["host_mode"] = agent.host_mode
        agents.append(entry)
    return agents


def _host_target_dict(target: Any) -> dict[str, Any] | None:
    if target is None:
        return None
    data: dict[str, Any] = {}
    if target.rules_file:
        data["rules_file"] = target.rules_file
    if target.skill_dir:
        data["skill_dir"] = target.skill_dir
    if target.agent_dir:
        data["agent_dir"] = target.agent_dir
    if target.settings_file:
        data["settings_file"] = target.settings_file
    if target.config_file:
        data["config_file"] = target.config_file
    if target.command_dir:
        data["command_dir"] = target.command_dir
    if target.plugin_dir:
        data["plugin_dir"] = target.plugin_dir
    if target.tool_dir:
        data["tool_dir"] = target.tool_dir
    return data or None


def _host_dict(manifest: PathManifest) -> dict[str, Any] | None:
    host = manifest.host
    if not host:
        return None
    data: dict[str, Any] = {}
    for name in ("claude", "opencode", "codex", "openclaw", "portable"):
        rendered = _host_target_dict(getattr(host, name))
        if rendered:
            data[name] = rendered
    return data or None


def _manifest_dict(manifest: PathManifest) -> dict[str, Any]:
    raw = dict(manifest.raw)
    ordered: dict[str, Any] = {
        "schema_version": manifest.schema_version,
        "slug": manifest.slug,
        "name": manifest.name,
        "version": manifest.version,
    }
    if manifest.summary:
        ordered["summary"] = manifest.summary
    ordered["primary_kind"] = manifest.primary_kind
    ordered["tags"] = manifest.tags

    components = raw.get("components")
    if components is not None:
        ordered["components"] = components

    if manifest.applet:
        ordered["applet"] = {
            "build_dir": manifest.applet.build_dir,
            "manifest": manifest.applet.manifest_path,
        }

    skill = _skill_dict(manifest)
    if skill is not None:
        ordered["skill"] = skill

    pipeline = _pipeline_dict(manifest)
    if pipeline is not None:
        ordered["pipeline"] = pipeline

    inputs = _inputs_dict(manifest)
    if inputs is not None:
        ordered["inputs"] = inputs

    agents = _agents_list(manifest)
    if agents is not None:
        ordered["agents"] = agents

    host = _host_dict(manifest)
    if host is not None:
        ordered["host"] = host

    for key, value in raw.items():
        if key not in ordered:
            ordered[key] = value

    return ordered


def format_path(*, path_dir: Path) -> PathFormatReport:
    path_dir = path_dir.resolve()
    manifest_path = path_dir / "wfpath.yaml"
    if not manifest_path.exists():
        raise PathFormatError(f"Missing wfpath.yaml in {path_dir}")

    try:
        manifest = PathManifest.load(manifest_path)
    except PathManifestError as exc:
        raise PathFormatError(str(exc)) from exc

    changed: list[str] = []

    manifest_text = yaml.safe_dump(
        _manifest_dict(manifest),
        sort_keys=False,
        allow_unicode=False,
    )
    if _write_if_changed(manifest_path, manifest_text):
        changed.append("wfpath.yaml")

    if manifest.applet:
        applet_manifest_path = path_dir / manifest.applet.manifest_path
        if applet_manifest_path.exists():
            parsed = json.loads(applet_manifest_path.read_text(encoding="utf-8"))
            formatted = json.dumps(parsed, indent=2, sort_keys=False) + "\n"
            if _write_if_changed(applet_manifest_path, formatted):
                changed.append(manifest.applet.manifest_path)

    try:
        render_report = render_skill_exports(path_dir=path_dir)
    except PathSkillRenderError as exc:
        raise PathFormatError(str(exc)) from exc
    changed.extend(render_report.written_files)

    return PathFormatReport(changed_files=sorted(set(changed)))
