from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.paths.manifest import (
    PathManifest,
    PathManifestError,
    PathSkillConfig,
    resolve_skill_runtime,
)
from wayfinder_paths.paths.pipeline import (
    STANDARD_OUTPUT_CONTRACT,
    PipelineGraphError,
    get_pipeline_archetype,
    load_pipeline_graph,
    validate_pipeline_graph,
)
from wayfinder_paths.paths.renderer import PathSkillRenderError, render_skill_exports
from wayfinder_paths.paths.scaffold import slugify

_ROOT_ASSET_RE = re.compile(r"""(?:src|href)=["']/(assets|_next)/""")
_SERVICE_WORKER_RE = re.compile(r"serviceWorker", re.IGNORECASE)
_REPO_NATIVE_CMD_RE = re.compile(r"\b(poetry run|python -m wayfinder_paths)\b")
_PATH_ESCAPE_RE = re.compile(r"(^|[\s`])(\.\./|/Users/|/home/)")
_RUNTIME_EXCLUDED_PREFIXES = (
    "applet/",
    "skill/",
    ".build/",
    "dist/",
    ".git/",
    ".venv/",
    "node_modules/",
    ".wayfinder/",
)
_MARKDOWN_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


class PathDoctorError(Exception):
    pass


@dataclass(frozen=True)
class DoctorIssue:
    level: str
    message: str
    path: str | None = None


@dataclass(frozen=True)
class PathDoctorReport:
    ok: bool
    slug: str | None
    version: str | None
    primary_kind: str | None
    errors: list[DoctorIssue]
    warnings: list[DoctorIssue]
    created_files: list[str]


def _read_template(relative_path: str) -> str:
    root = resources.files("wayfinder_paths.paths")
    template_path = root.joinpath("templates").joinpath(relative_path)
    return template_path.read_text(encoding="utf-8")


def _render_template(text: str, context: dict[str, Any]) -> str:
    rendered = text
    for key, value in context.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
    return rendered


def _write_if_missing(path: Path, content: str, *, overwrite: bool) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        return False
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return True


def _component_path(manifest: PathManifest) -> str:
    components = manifest.raw.get("components")
    if isinstance(components, list) and components:
        first = components[0]
        if isinstance(first, dict):
            path_raw = str(first.get("path") or "").strip()
            if path_raw:
                return path_raw
    return "strategy.py" if manifest.primary_kind == "strategy" else "scripts/main.py"


def _path_context(manifest: PathManifest) -> dict[str, Any]:
    return {
        "slug": manifest.slug,
        "name": manifest.name,
        "version": manifest.version,
        "summary": manifest.summary.strip() or "TODO: describe what this path does.",
        "primary_kind": manifest.primary_kind,
        "component_path": _component_path(manifest),
    }


def _record_issue(
    issues: list[DoctorIssue], *, level: str, message: str, path: Path | None = None
) -> None:
    issues.append(
        DoctorIssue(level=level, message=message, path=str(path) if path else None)
    )


def _validate_components(
    *,
    path_dir: Path,
    manifest: PathManifest,
    warnings: list[DoctorIssue],
) -> None:
    components = manifest.raw.get("components")
    if components is None:
        return
    if not isinstance(components, list):
        _record_issue(
            warnings,
            level="warning",
            message="wfpath.yaml components must be a list",
        )
        return

    for item in components:
        if not isinstance(item, dict):
            _record_issue(
                warnings,
                level="warning",
                message="components entry must be an object",
            )
            continue
        path_raw = str(item.get("path") or "").strip()
        if not path_raw:
            continue
        target = path_dir / path_raw
        if not target.exists():
            _record_issue(
                warnings,
                level="warning",
                message=f"Component file not found: {path_raw}",
                path=target,
            )


def _validate_skill(
    *,
    path_dir: Path,
    manifest: PathManifest,
    ctx: dict[str, Any],
    fix: bool,
    overwrite: bool,
    errors: list[DoctorIssue],
    warnings: list[DoctorIssue],
    created_files: list[str],
) -> None:
    skill = manifest.skill
    if not skill or not skill.enabled:
        return

    if skill.source == "generated":
        _validate_generated_skill(
            path_dir=path_dir,
            skill=skill,
            ctx=ctx,
            fix=fix,
            overwrite=overwrite,
            errors=errors,
            warnings=warnings,
            created_files=created_files,
        )
        return

    provided_skill_path = path_dir / "skill" / "SKILL.md"
    if not provided_skill_path.exists():
        _record_issue(
            errors,
            level="error",
            message="Missing skill/SKILL.md for skill.source=provided",
            path=provided_skill_path,
        )


def _is_safe_artifact_output(output: str, artifacts_dir: str) -> bool:
    normalized = output.strip()
    if not normalized.startswith(f"{artifacts_dir}/"):
        return False
    return ".." not in normalized


def _validate_pipeline(
    *,
    path_dir: Path,
    manifest: PathManifest,
    errors: list[DoctorIssue],
    warnings: list[DoctorIssue],
) -> None:
    pipeline = manifest.pipeline
    if pipeline is None:
        return

    if not pipeline.archetype:
        _record_issue(
            errors,
            level="error",
            message="wfpath.yaml pipeline.archetype is required",
            path=path_dir / "wfpath.yaml",
        )
        return

    try:
        archetype = get_pipeline_archetype(pipeline.archetype)
    except PipelineGraphError as exc:
        _record_issue(
            errors,
            level="error",
            message=str(exc),
            path=path_dir / "wfpath.yaml",
        )
        return

    missing_output_fields = set(STANDARD_OUTPUT_CONTRACT) - set(
        pipeline.output_contract or STANDARD_OUTPUT_CONTRACT
    )
    if missing_output_fields:
        _record_issue(
            errors,
            level="error",
            message=(
                "pipeline.output_contract is missing standard fields: "
                + ", ".join(sorted(missing_output_fields))
            ),
            path=path_dir / "wfpath.yaml",
        )

    if not pipeline.graph_path:
        _record_issue(
            errors,
            level="error",
            message="wfpath.yaml pipeline.graph is required",
            path=path_dir / "wfpath.yaml",
        )
    else:
        graph_path = path_dir / pipeline.graph_path
        if not graph_path.exists():
            _record_issue(
                errors,
                level="error",
                message="Missing pipeline graph file",
                path=graph_path,
            )
        else:
            try:
                graph = load_pipeline_graph(graph_path)
                validate_pipeline_graph(graph, archetype=pipeline.archetype)
            except PipelineGraphError as exc:
                _record_issue(
                    errors,
                    level="error",
                    message=str(exc),
                    path=graph_path,
                )

    policy_path = path_dir / "policy" / "default.yaml"
    if not policy_path.exists():
        _record_issue(
            errors,
            level="error",
            message="Missing policy/default.yaml for pipeline path",
            path=policy_path,
        )
    else:
        try:
            policy = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
        except Exception:
            policy = None
        if not isinstance(policy, dict):
            _record_issue(
                errors,
                level="error",
                message="policy/default.yaml must be a YAML object",
                path=policy_path,
            )
        else:
            missing_sections = [
                key
                for key in archetype.required_policy_sections
                if key not in policy or policy.get(key) in (None, "", [], {})
            ]
            if missing_sections:
                _record_issue(
                    errors,
                    level="error",
                    message=(
                        "policy/default.yaml is missing required sections: "
                        + ", ".join(missing_sections)
                    ),
                    path=policy_path,
                )

    if not manifest.inputs:
        _record_issue(
            errors,
            level="error",
            message="Pipeline paths must declare inputs.slots",
            path=path_dir / "wfpath.yaml",
        )
    for slot in manifest.inputs:
        slot_path = path_dir / slot.path
        if not slot_path.exists():
            _record_issue(
                errors,
                level="error",
                message=f"Missing input slot file: {slot.name}",
                path=slot_path,
            )
        if not slot.schema:
            _record_issue(
                errors,
                level="error",
                message=f"Input slot '{slot.name}' must declare a schema",
                path=path_dir / "wfpath.yaml",
            )
        else:
            schema_path = path_dir / slot.schema
            if not schema_path.exists():
                _record_issue(
                    errors,
                    level="error",
                    message=f"Missing schema for input slot: {slot.name}",
                    path=schema_path,
                )

    if not manifest.agents:
        _record_issue(
            errors,
            level="error",
            message="Pipeline paths must declare agents",
            path=path_dir / "wfpath.yaml",
        )
    seen_outputs: set[str] = set()
    for agent in manifest.agents:
        agent_doc = path_dir / "skill" / "agents" / f"{agent.agent_id}.md"
        if not agent_doc.exists():
            _record_issue(
                errors,
                level="error",
                message=f"Missing skill/agents/{agent.agent_id}.md",
                path=agent_doc,
            )
        if agent.output in seen_outputs:
            _record_issue(
                errors,
                level="error",
                message=f"Duplicate agent output path: {agent.output}",
                path=path_dir / "wfpath.yaml",
            )
        seen_outputs.add(agent.output)
        if not _is_safe_artifact_output(agent.output, pipeline.artifacts_dir):
            _record_issue(
                errors,
                level="error",
                message=(
                    "Agent outputs must stay under the configured artifacts_dir: "
                    f"{agent.agent_id}"
                ),
                path=path_dir / "wfpath.yaml",
            )

    fixtures_dir = path_dir / "tests" / "fixtures"
    fixture_files = sorted(
        [
            path
            for path in fixtures_dir.glob("*")
            if path.is_file() and path.suffix in {".yaml", ".yml", ".json"}
        ]
    )
    if len(fixture_files) < 3:
        _record_issue(
            errors,
            level="error",
            message="Pipeline paths must ship at least 3 fixtures",
            path=fixtures_dir,
        )

    evals_dir = path_dir / "tests" / "evals"
    eval_files = sorted(
        [
            path
            for path in evals_dir.glob("*")
            if path.is_file() and path.suffix in {".yaml", ".yml", ".json", ".md"}
        ]
    )
    if len(eval_files) < 3:
        _record_issue(
            errors,
            level="error",
            message="Pipeline paths must ship at least 3 evals",
            path=evals_dir,
        )

    if manifest.host is None:
        _record_issue(
            warnings,
            level="warning",
            message="Pipeline paths should declare host adapter metadata",
            path=path_dir / "wfpath.yaml",
        )


def _validate_runtime_skill_contract(
    *,
    path_dir: Path,
    manifest: PathManifest,
    errors: list[DoctorIssue],
    warnings: list[DoctorIssue],
) -> None:
    skill = manifest.skill
    if not skill or not skill.enabled:
        return

    runtime = resolve_skill_runtime(manifest)
    if runtime.mode != "thin":
        _record_issue(
            errors,
            level="error",
            message="Only skill.runtime.mode=thin is supported for host skill exports",
            path=path_dir / "wfpath.yaml",
        )
        return

    if skill.uses_portable_alias:
        _record_issue(
            warnings,
            level="warning",
            message="skill.portable is deprecated; use skill.runtime instead",
            path=path_dir / "wfpath.yaml",
        )

    try:
        component = manifest.resolve_component(runtime.component)
    except PathManifestError as exc:
        _record_issue(
            errors,
            level="error",
            message=str(exc),
            path=path_dir / "wfpath.yaml",
        )
        return

    component_path = str(component.get("path") or "").strip()
    if any(component_path.startswith(prefix) for prefix in _RUNTIME_EXCLUDED_PREFIXES):
        _record_issue(
            errors,
            level="error",
            message=(
                "The configured runtime component is excluded from thin skill exports "
                f"and cannot be executed: {component_path}"
            ),
            path=path_dir / component_path,
        )

    skill_doc_path = (
        path_dir / "skill" / "SKILL.md"
        if skill.source == "provided"
        else path_dir / (skill.instructions_path or "")
    )
    if not skill_doc_path.exists():
        return

    body = skill_doc_path.read_text(encoding="utf-8", errors="ignore")
    if _REPO_NATIVE_CMD_RE.search(body):
        _record_issue(
            warnings,
            level="warning",
            message="Skill docs contain repo-native commands; use export-local runtime commands instead",
            path=skill_doc_path,
        )

    if _PATH_ESCAPE_RE.search(body):
        _record_issue(
            errors,
            level="error",
            message="Skill docs reference paths outside the exported skill root",
            path=skill_doc_path,
        )

    if (
        runtime.require_api_key
        and runtime.api_key_env
        and runtime.api_key_env not in body
    ):
        _record_issue(
            warnings,
            level="warning",
            message=(
                "Skill runtime requires an API key, but the instructions do not mention "
                f"{runtime.api_key_env}"
            ),
            path=skill_doc_path,
        )


def _validate_generated_skill(
    *,
    path_dir: Path,
    skill: PathSkillConfig,
    ctx: dict[str, Any],
    fix: bool,
    overwrite: bool,
    errors: list[DoctorIssue],
    warnings: list[DoctorIssue],
    created_files: list[str],
) -> None:
    if not skill.instructions_path:
        _record_issue(
            errors,
            level="error",
            message="wfpath.yaml skill.instructions is required for generated skills",
            path=path_dir / "wfpath.yaml",
        )
        return

    instructions_path = path_dir / skill.instructions_path
    if not instructions_path.exists():
        resolved = False
        if fix:
            rendered = _render_template(
                _read_template("skill/instructions.md.tmpl"),
                ctx,
            )
            if _write_if_missing(instructions_path, rendered, overwrite=overwrite):
                created_files.append(skill.instructions_path)
                resolved = True
            else:
                resolved = instructions_path.exists()
        if not resolved:
            _record_issue(
                errors,
                level="error",
                message="Missing generated skill instructions",
                path=instructions_path,
            )

    provided_skill_path = path_dir / "skill" / "SKILL.md"
    if provided_skill_path.exists():
        _record_issue(
            warnings,
            level="warning",
            message=(
                "skill/SKILL.md is ignored when skill.source=generated; "
                "run `wayfinder path render-skill` to produce host exports"
            ),
            path=provided_skill_path,
        )


def _validate_applet(
    *,
    path_dir: Path,
    manifest: PathManifest,
    ctx: dict[str, Any],
    fix: bool,
    overwrite: bool,
    errors: list[DoctorIssue],
    warnings: list[DoctorIssue],
    created_files: list[str],
) -> None:
    if not manifest.applet:
        return

    applet_manifest_path = path_dir / manifest.applet.manifest_path
    if not applet_manifest_path.exists():
        resolved = False
        if fix:
            rendered = _render_template(
                _read_template("applet/applet.manifest.json.tmpl"),
                ctx,
            )
            if _write_if_missing(applet_manifest_path, rendered, overwrite=overwrite):
                created_files.append(manifest.applet.manifest_path)
                resolved = True
            else:
                resolved = applet_manifest_path.exists()
        if not resolved:
            _record_issue(
                errors,
                level="error",
                message="Missing applet.manifest.json (declared in wfpath.yaml)",
                path=applet_manifest_path,
            )

    build_dir = path_dir / manifest.applet.build_dir
    if not build_dir.exists():
        if fix:
            build_dir.mkdir(parents=True, exist_ok=True)
        if not build_dir.exists():
            _record_issue(
                errors,
                level="error",
                message=(
                    "Applet build_dir does not exist (run your applet build step or "
                    "scaffold a static UI)"
                ),
                path=build_dir,
            )

    entry = "index.html"
    if applet_manifest_path.exists():
        try:
            parsed = json.loads(applet_manifest_path.read_text(encoding="utf-8"))
        except Exception:
            parsed = None

        if isinstance(parsed, dict):
            entry_val = str(parsed.get("entry") or "").strip()
            if entry_val:
                entry = entry_val
            if not str(parsed.get("readySelector") or "").strip():
                _record_issue(
                    warnings,
                    level="warning",
                    message="applet.manifest.json missing readySelector",
                    path=applet_manifest_path,
                )
        else:
            _record_issue(
                warnings,
                level="warning",
                message="applet.manifest.json must be a JSON object",
                path=applet_manifest_path,
            )

    entry_path = build_dir / entry
    if not entry_path.exists():
        resolved = False
        if fix and entry == "index.html":
            rendered = _render_template(
                _read_template("applet/dist/index.html.tmpl"),
                ctx,
            )
            if _write_if_missing(entry_path, rendered, overwrite=overwrite):
                created_files.append(f"{manifest.applet.build_dir}/{entry}")
                resolved = True
            else:
                resolved = entry_path.exists()
        if not resolved:
            _record_issue(
                errors,
                level="error",
                message=f"Applet entry not found: {manifest.applet.build_dir}/{entry}",
                path=entry_path,
            )

    if entry_path.exists():
        entry_text = entry_path.read_text(encoding="utf-8", errors="ignore")
        if _ROOT_ASSET_RE.search(entry_text):
            _record_issue(
                errors,
                level="error",
                message=(
                    "Applet entry uses root-absolute asset URLs (/assets or /_next). "
                    "Use relative asset URLs."
                ),
                path=entry_path,
            )
        if _SERVICE_WORKER_RE.search(entry_text):
            _record_issue(
                warnings,
                level="warning",
                message=(
                    "Applet entry references service workers; avoid service workers "
                    "for MVP path applets."
                ),
                path=entry_path,
            )

    js_path = build_dir / "assets" / "app.js"
    if fix and entry_path.exists() and not js_path.exists():
        rendered = _render_template(
            _read_template("applet/dist/assets/app.js.tmpl"),
            ctx,
        )
        if _write_if_missing(js_path, rendered, overwrite=overwrite):
            created_files.append(f"{manifest.applet.build_dir}/assets/app.js")


def _parse_markdown_frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    match = _MARKDOWN_FRONTMATTER_RE.match(text)
    if match is None:
        return {}
    try:
        parsed = yaml.safe_load(match.group(1)) or {}
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _has_explicit_opencode_model(
    manifest: PathManifest, model_override: str | None
) -> bool:
    if model_override:
        return True
    host = manifest.host.opencode if manifest.host else None
    return bool(host and host.model)


def _opencode_tool_uses_supported_result_contract(tool_text: str) -> bool:
    return "output: JSON.stringify(" in tool_text and "jsonOutput(" in tool_text


def _opencode_tool_returns_bare_object(tool_text: str) -> bool:
    compact = " ".join(tool_text.split())
    return "return { ok:" in compact or "return {ok:" in compact


def _validate_opencode_tool_contract(
    *,
    tool_path: Path,
    label: str,
    errors: list[DoctorIssue],
) -> None:
    if not tool_path.exists():
        return
    tool_text = tool_path.read_text(encoding="utf-8", errors="ignore")
    if _opencode_tool_returns_bare_object(
        tool_text
    ) or not _opencode_tool_uses_supported_result_contract(tool_text):
        _record_issue(
            errors,
            level="error",
            message=f"{label} must return a string or an object with an output field",
            path=tool_path,
        )


def _required_opencode_skills(manifest: PathManifest) -> list[str]:
    skill = manifest.skill
    if skill and skill.dependencies:
        return [
            dependency.host_names.get("opencode") or dependency.name
            for dependency in skill.dependencies
        ]
    if not manifest.pipeline or not manifest.pipeline.archetype:
        return []
    try:
        archetype = get_pipeline_archetype(manifest.pipeline.archetype)
    except Exception:
        return []
    return list(archetype.required_skills)


def _validate_opencode_export(
    *,
    path_dir: Path,
    manifest: PathManifest,
    errors: list[DoctorIssue],
    warnings: list[DoctorIssue],
    model_override: str | None,
) -> None:
    skill = manifest.skill
    if not skill or not skill.enabled:
        return

    try:
        render_report = render_skill_exports(
            path_dir=path_dir,
            hosts=["opencode"],
            opencode_model_override=model_override,
        )
    except PathSkillRenderError as exc:
        _record_issue(errors, level="error", message=str(exc), path=path_dir)
        return

    export_info = render_report.exports.get("opencode")
    if export_info is None:
        _record_issue(
            errors,
            level="error",
            message="OpenCode export was not rendered",
            path=path_dir,
        )
        return

    export_dir = export_info.export_dir
    command_name = (
        manifest.pipeline.entry_command
        if manifest.pipeline and manifest.pipeline.entry_command
        else skill.name
    )
    orchestrator_path = (
        export_dir
        / "install"
        / ".opencode"
        / "agents"
        / f"{skill.name}-orchestrator.md"
    )
    command_path = (
        export_dir / "install" / ".opencode" / "commands" / f"{command_name}.md"
    )
    opencode_config_path = export_dir / "install" / "opencode.json"
    agents_md_path = export_dir / "install" / "AGENTS.md"
    artifact_gate_path = (
        export_dir / "install" / ".opencode" / "tools" / "wayfinder_artifact_gate.ts"
    )
    compile_job_path = export_dir / "install" / ".opencode" / "tools" / "compile_job.ts"
    validate_order_path = (
        export_dir / "install" / ".opencode" / "tools" / "validate_order.ts"
    )

    for required_path, label in (
        (orchestrator_path, "OpenCode orchestrator"),
        (command_path, "OpenCode command"),
        (opencode_config_path, "opencode.json"),
        (agents_md_path, "AGENTS.md"),
        (compile_job_path, "OpenCode compile_job tool"),
        (validate_order_path, "OpenCode validate_order tool"),
    ):
        if not required_path.exists():
            _record_issue(
                errors,
                level="error",
                message=f"Missing {label} in rendered OpenCode export",
                path=required_path,
            )

    if manifest.pipeline and not artifact_gate_path.exists():
        _record_issue(
            errors,
            level="error",
            message="Missing OpenCode artifact gate tool for pipeline export",
            path=artifact_gate_path,
        )

    if orchestrator_path.exists():
        frontmatter = _parse_markdown_frontmatter(orchestrator_path)
        mode = str(frontmatter.get("mode") or "").strip()
        if mode not in {"all", "subagent"}:
            _record_issue(
                errors,
                level="error",
                message="OpenCode orchestrator must use mode all or subagent",
                path=orchestrator_path,
            )
        permission = frontmatter.get("permission") or {}
        task_permission = permission.get("task") if isinstance(permission, dict) else {}
        if not isinstance(task_permission, dict):
            task_permission = {}
        for blocked in ("general", "explore"):
            if task_permission.get(blocked) == "allow":
                _record_issue(
                    errors,
                    level="error",
                    message=f"OpenCode orchestrator must not allow task {blocked}",
                    path=orchestrator_path,
                )
        for agent in manifest.agents:
            worker_name = f"{skill.name}-{agent.agent_id}"
            if task_permission.get(worker_name) != "allow":
                _record_issue(
                    errors,
                    level="error",
                    message=f"OpenCode orchestrator must allow worker {worker_name}",
                    path=orchestrator_path,
                )
        if (
            not _has_explicit_opencode_model(manifest, model_override)
            and "model" in frontmatter
        ):
            _record_issue(
                errors,
                level="error",
                message="OpenCode exports must not hardcode a model unless explicitly configured",
                path=orchestrator_path,
            )

    if command_path.exists():
        frontmatter = _parse_markdown_frontmatter(command_path)
        if str(frontmatter.get("agent") or "").strip() != f"{skill.name}-orchestrator":
            _record_issue(
                errors,
                level="error",
                message="OpenCode command must route to the orchestrator agent",
                path=command_path,
            )
        if frontmatter.get("subtask") is not True:
            _record_issue(
                errors,
                level="error",
                message="OpenCode command must set subtask: true",
                path=command_path,
            )
        command_text = command_path.read_text(encoding="utf-8", errors="ignore")
        if "@inputs/" in command_text:
            _record_issue(
                errors,
                level="error",
                message="OpenCode command must not hardcode @inputs file references",
                path=command_path,
            )
        if (
            not _has_explicit_opencode_model(manifest, model_override)
            and "model" in frontmatter
        ):
            _record_issue(
                errors,
                level="error",
                message="OpenCode command must not hardcode a model unless explicitly configured",
                path=command_path,
            )

    for agent in manifest.agents:
        worker_path = (
            export_dir
            / "install"
            / ".opencode"
            / "agents"
            / f"{skill.name}-{agent.agent_id}.md"
        )
        if not worker_path.exists():
            _record_issue(
                errors,
                level="error",
                message=f"Missing OpenCode worker export for {agent.agent_id}",
                path=worker_path,
            )
            continue
        frontmatter = _parse_markdown_frontmatter(worker_path)
        if str(frontmatter.get("mode") or "").strip() != "subagent":
            _record_issue(
                errors,
                level="error",
                message=f"OpenCode worker {agent.agent_id} must use mode subagent",
                path=worker_path,
            )
        if frontmatter.get("hidden") is not True:
            _record_issue(
                errors,
                level="error",
                message=f"OpenCode worker {agent.agent_id} must be hidden",
                path=worker_path,
            )
        if (
            not _has_explicit_opencode_model(manifest, model_override)
            and "model" in frontmatter
        ):
            _record_issue(
                errors,
                level="error",
                message="OpenCode worker exports must not hardcode a model unless explicitly configured",
                path=worker_path,
            )

    if opencode_config_path.exists():
        try:
            config = json.loads(opencode_config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            _record_issue(
                errors,
                level="error",
                message="Generated opencode.json must be valid JSON",
                path=opencode_config_path,
            )
            config = {}
        if isinstance(config, dict):
            instructions = config.get("instructions") or []
            if "AGENTS.md" not in instructions:
                _record_issue(
                    errors,
                    level="error",
                    message="Generated opencode.json must include AGENTS.md in instructions",
                    path=opencode_config_path,
                )

    if agents_md_path.exists():
        agents_md = agents_md_path.read_text(encoding="utf-8", errors="ignore")
        if f"Wayfinder path: {manifest.slug}" not in agents_md:
            _record_issue(
                warnings,
                level="warning",
                message="Generated AGENTS.md is missing the Wayfinder path header",
                path=agents_md_path,
            )

    for tool_path, label in (
        (artifact_gate_path, "OpenCode artifact gate tool"),
        (compile_job_path, "OpenCode compile_job tool"),
        (validate_order_path, "OpenCode validate_order tool"),
    ):
        _validate_opencode_tool_contract(
            tool_path=tool_path,
            label=label,
            errors=errors,
        )


def _validate_opencode_activation_root(
    *,
    manifest: PathManifest,
    activated_root: Path,
    errors: list[DoctorIssue],
) -> None:
    skill = manifest.skill
    if not skill or not skill.enabled:
        return
    command_name = (
        manifest.pipeline.entry_command
        if manifest.pipeline and manifest.pipeline.entry_command
        else skill.name
    )
    required_paths = [
        activated_root / ".opencode" / "skills" / skill.name / "SKILL.md",
        activated_root / ".opencode" / "agents" / f"{skill.name}-orchestrator.md",
        activated_root / ".opencode" / "commands" / f"{command_name}.md",
        activated_root / "AGENTS.md",
        activated_root / "opencode.json",
    ]
    if manifest.pipeline:
        required_paths.append(
            activated_root / ".opencode" / "tools" / "wayfinder_artifact_gate.ts"
        )
    required_paths.extend(
        [
            activated_root / ".opencode" / "tools" / "compile_job.ts",
            activated_root / ".opencode" / "tools" / "validate_order.ts",
        ]
    )
    for dependency_name in _required_opencode_skills(manifest):
        required_paths.append(
            activated_root / ".opencode" / "skills" / dependency_name / "SKILL.md"
        )
    for required_path in required_paths:
        if not required_path.exists():
            _record_issue(
                errors,
                level="error",
                message="Missing activated OpenCode file",
                path=required_path,
            )

    for tool_path, label in (
        (
            activated_root / ".opencode" / "tools" / "wayfinder_artifact_gate.ts",
            "Activated OpenCode artifact gate tool",
        ),
        (
            activated_root / ".opencode" / "tools" / "compile_job.ts",
            "Activated OpenCode compile_job tool",
        ),
        (
            activated_root / ".opencode" / "tools" / "validate_order.ts",
            "Activated OpenCode validate_order tool",
        ),
    ):
        _validate_opencode_tool_contract(
            tool_path=tool_path,
            label=label,
            errors=errors,
        )


def run_doctor(
    *,
    path_dir: Path,
    fix: bool = False,
    overwrite: bool = False,
    host: str | None = None,
    activated_root: Path | None = None,
    model_override: str | None = None,
    validation_mode: str = "full",
) -> PathDoctorReport:
    path_dir = path_dir.resolve()
    if not path_dir.exists():
        raise PathDoctorError(f"Path directory not found: {path_dir}")
    if not path_dir.is_dir():
        raise PathDoctorError(f"Path directory must be a directory: {path_dir}")

    manifest_path = path_dir / "wfpath.yaml"
    if not manifest_path.exists():
        raise PathDoctorError(
            "Missing wfpath.yaml (run `wayfinder path init <slug>` to scaffold one)"
        )

    errors: list[DoctorIssue] = []
    warnings: list[DoctorIssue] = []
    created_files: list[str] = []
    installed_host_mode = validation_mode == "installed_host"

    manifest: PathManifest | None = None
    try:
        manifest = PathManifest.load(manifest_path)
    except PathManifestError as exc:
        _record_issue(errors, level="error", message=str(exc), path=manifest_path)

    ctx: dict[str, Any] = {}
    if manifest:
        expected_slug = slugify(manifest.slug)
        if expected_slug != manifest.slug:
            _record_issue(
                errors,
                level="error",
                message=f"wfpath.yaml slug must be URL-safe (suggested: {expected_slug})",
                path=manifest_path,
            )

        if not installed_host_mode:
            ctx = _path_context(manifest)
            _validate_components(
                path_dir=path_dir, manifest=manifest, warnings=warnings
            )

            readme_path = path_dir / "README.md"
            if not readme_path.exists():
                _record_issue(
                    warnings,
                    level="warning",
                    message="Missing README.md",
                    path=readme_path,
                )
                if fix:
                    rendered = _render_template(_read_template("README.md.tmpl"), ctx)
                    if _write_if_missing(readme_path, rendered, overwrite=overwrite):
                        created_files.append("README.md")

            _validate_skill(
                path_dir=path_dir,
                manifest=manifest,
                ctx=ctx,
                fix=fix,
                overwrite=overwrite,
                errors=errors,
                warnings=warnings,
                created_files=created_files,
            )
            _validate_pipeline(
                path_dir=path_dir,
                manifest=manifest,
                errors=errors,
                warnings=warnings,
            )
            _validate_runtime_skill_contract(
                path_dir=path_dir,
                manifest=manifest,
                errors=errors,
                warnings=warnings,
            )

            _validate_applet(
                path_dir=path_dir,
                manifest=manifest,
                ctx=ctx,
                fix=fix,
                overwrite=overwrite,
                errors=errors,
                warnings=warnings,
                created_files=created_files,
            )
            if host == "opencode":
                _validate_opencode_export(
                    path_dir=path_dir,
                    manifest=manifest,
                    errors=errors,
                    warnings=warnings,
                    model_override=model_override,
                )
        if host == "opencode" and activated_root is not None:
            _validate_opencode_activation_root(
                manifest=manifest,
                activated_root=activated_root.resolve(),
                errors=errors,
            )

    return PathDoctorReport(
        ok=len(errors) == 0,
        slug=getattr(manifest, "slug", None),
        version=getattr(manifest, "version", None),
        primary_kind=getattr(manifest, "primary_kind", None),
        errors=errors,
        warnings=warnings,
        created_files=created_files,
    )
