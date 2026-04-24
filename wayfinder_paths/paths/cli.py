from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import click

from wayfinder_paths.paths.builder import PathBuilder, PathBuildError, _sha256_file
from wayfinder_paths.paths.client import PathsApiClient, PathsApiError
from wayfinder_paths.paths.doctor import PathDoctorError, PathDoctorReport, run_doctor
from wayfinder_paths.paths.evaluator import PathEvalError, run_path_eval
from wayfinder_paths.paths.formatter import PathFormatError, format_path
from wayfinder_paths.paths.hooks import PathHooksError, install_path_hooks
from wayfinder_paths.paths.manifest import (
    PathManifest,
    PathManifestError,
    PathSkillDependencyConfig,
    resolve_skill_dependencies,
)
from wayfinder_paths.paths.preview import (
    PathPreviewError,
    inspect_preview_path,
    preview_path,
)
from wayfinder_paths.paths.renderer import (
    PathSkillRenderError,
    PathSkillRenderReport,
    render_skill_exports,
)
from wayfinder_paths.paths.scaffold import PathScaffoldError, init_path, slugify

_INSTALL_DIRNAME = "paths"
_LEGACY_INSTALL_DIRNAME = "packs"
_LOCKFILE_NAME = "paths.lock.json"
_LEGACY_LOCKFILE_NAME = "packs.lock.json"
_OPENCODE_TOOL_RESULT_HELPER = "\n".join(
    [
        "function jsonOutput(payload) {",
        "  return JSON.stringify(payload, null, 2)",
        "}",
    ]
)
_LEGACY_OPENCODE_TOOL_RESULT_RE = re.compile(
    r"function\s+jsonOutput\s*\(\s*payload\s*\)\s*\{\s*"
    r"return\s*\{\s*output\s*:\s*JSON\.stringify\s*\(\s*payload\s*,\s*null\s*,\s*2\s*\)\s*,?\s*\}\s*"
    r"\}",
    re.MULTILINE,
)


@dataclass(frozen=True)
class _ActivationTarget:
    host: str
    scope: str
    source: str


def _echo_json(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, default=str))


def _iso_utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _path_install_venue(*, runtime: str) -> str:
    return str(os.environ.get("WAYFINDER_PATHS_INSTALL_VENUE") or runtime).strip()


def _sdk_root() -> Path | None:
    env_root = str(os.environ.get("WAYFINDER_SDK_ROOT") or "").strip()
    if env_root:
        candidate = Path(env_root).expanduser().resolve()
        if candidate.exists():
            return candidate

    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / ".claude" / "skills").is_dir():
            return parent
    return None


def _sdk_skill_source_dir(skill_name: str, *, host: str) -> Path | None:
    normalized = str(skill_name or "").strip()
    if not normalized:
        return None

    sdk_root = _sdk_root()
    if sdk_root is None:
        return None

    if host == "openclaw":
        override_dir = sdk_root / "openclaw" / "skills" / normalized
        if (override_dir / "SKILL.md").is_file():
            return override_dir

    base_dir = sdk_root / ".claude" / "skills" / normalized
    if (base_dir / "SKILL.md").is_file():
        return base_dir

    override_dir = sdk_root / "openclaw" / "skills" / normalized
    if (override_dir / "SKILL.md").is_file():
        return override_dir

    return None


def _canonical_install_root(install_dir: str | Path) -> Path:
    base = Path(install_dir).expanduser()
    if base.name == _LEGACY_INSTALL_DIRNAME:
        return base.with_name(_INSTALL_DIRNAME)
    return base


def _state_dir_for_install_root(install_root: Path) -> Path:
    if install_root.name in {_INSTALL_DIRNAME, _LEGACY_INSTALL_DIRNAME}:
        return install_root.parent
    return install_root


def _load_install_lock(state_dir: Path) -> tuple[dict[str, Any], Path]:
    lock_path = state_dir / _LOCKFILE_NAME
    source_path = lock_path
    if not source_path.exists():
        source_path = state_dir / _LEGACY_LOCKFILE_NAME

    raw: dict[str, Any] = {}
    if source_path.exists():
        try:
            parsed = json.loads(source_path.read_text()) or {}
        except Exception:
            parsed = {}
        if isinstance(parsed, dict):
            raw = parsed

    paths = raw.get("paths")
    if not isinstance(paths, dict):
        legacy_paths = raw.get("packs")
        paths = legacy_paths if isinstance(legacy_paths, dict) else {}

    normalized = {
        key: value for key, value in raw.items() if key not in {"packs", "paths"}
    }
    normalized["schemaVersion"] = raw.get("schemaVersion") or "0.1"
    normalized["paths"] = paths
    return normalized, lock_path


def _write_install_lock(lock_path: Path, lock: dict[str, Any]) -> None:
    normalized = {key: value for key, value in lock.items() if key != "packs"}
    normalized["schemaVersion"] = normalized.get("schemaVersion") or "0.1"
    normalized["generatedAt"] = _iso_utc_now()
    normalized["paths"] = normalized.get("paths") or {}
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(normalized, indent=2, default=str) + "\n")


def _lock_paths_map(lock: dict[str, Any]) -> dict[str, Any]:
    paths_map = lock.get("paths")
    if isinstance(paths_map, dict):
        return paths_map
    paths_map = {}
    lock["paths"] = paths_map
    return paths_map


def _lock_path_entry(lock: dict[str, Any], slug: str) -> dict[str, Any] | None:
    entry = _lock_paths_map(lock).get(slug)
    return entry if isinstance(entry, dict) else None


def _update_lock_path_entry(
    lock: dict[str, Any],
    lock_path: Path,
    *,
    slug: str,
    entry: dict[str, Any],
) -> None:
    paths_map = _lock_paths_map(lock)
    paths_map[slug] = entry
    lock["paths"] = paths_map
    _write_install_lock(lock_path, lock)


def _doctor_result_payload(report: PathDoctorReport) -> dict[str, Any]:
    return {
        "slug": report.slug,
        "version": report.version,
        "primary_kind": report.primary_kind,
        "errors": [{"message": i.message, "path": i.path} for i in report.errors],
        "warnings": [{"message": i.message, "path": i.path} for i in report.warnings],
        "created_files": report.created_files,
    }


def _raise_for_doctor_errors(report: PathDoctorReport) -> None:
    if report.ok:
        return
    details = "\n".join(
        f"- {issue.message}" + (f" ({issue.path})" if issue.path else "")
        for issue in report.errors
    )
    raise click.ClickException(f"Path doctor found errors\n{details}")


def _skill_export_warning_strings(report: PathDoctorReport) -> list[str]:
    return [
        issue.message + (f" ({issue.path})" if issue.path else "")
        for issue in report.warnings
    ]


def _zip_skill_export_dir(export_dir: Path) -> bytes:
    buf = io.BytesIO()
    with ZipFile(buf, "w") as zf:
        for path in sorted(export_dir.rglob("*")):
            if not path.is_file():
                continue
            arcname = Path("skill") / path.relative_to(export_dir)
            zf.write(path, arcname.as_posix())
    return buf.getvalue()


def _collect_skill_export_uploads(
    render_report: PathSkillRenderReport,
    doctor_report: PathDoctorReport,
) -> tuple[dict[str, Any] | None, dict[str, bytes]]:
    if not render_report.rendered_hosts:
        return None, {}

    skill_exports: dict[str, bytes] = {}
    exports_detail: dict[str, Any] = {}
    for host in render_report.rendered_hosts:
        info = render_report.exports.get(host)
        if info is None:
            raise click.ClickException(
                f"Missing rendered export metadata for host '{host}'"
            )
        skill_exports[host] = _zip_skill_export_dir(info.export_dir)
        exports_detail[host] = {
            "filename": info.filename,
            "mode": info.mode,
            "runtime": info.runtime_manifest,
            "export": info.export_manifest,
        }

    exports_manifest = {
        "targets": render_report.rendered_hosts,
        "doctor": {
            "status": "warn" if doctor_report.warnings else "ok",
            "warnings": _skill_export_warning_strings(doctor_report),
        },
        "exports": exports_detail,
    }
    return exports_manifest, skill_exports


def _load_applet_meta(path_dir: Path, manifest: PathManifest) -> dict[str, Any]:
    if manifest.applet is None:
        return {}
    applet_manifest_path = path_dir / manifest.applet.manifest_path
    if not applet_manifest_path.exists():
        return {}
    try:
        parsed = json.loads(applet_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        "build_dir": manifest.applet.build_dir,
        "applet_manifest": manifest.applet.manifest_path,
        **parsed,
    }


def _prepare_path_for_build(
    path_dir: Path,
) -> tuple[PathDoctorReport, PathSkillRenderReport]:
    try:
        doctor_report = run_doctor(path_dir=path_dir, fix=False, overwrite=False)
    except PathDoctorError as exc:
        raise click.ClickException(str(exc)) from exc
    _raise_for_doctor_errors(doctor_report)

    try:
        render_report = render_skill_exports(path_dir=path_dir)
    except PathSkillRenderError as exc:
        raise click.ClickException(str(exc)) from exc

    return doctor_report, render_report


def _load_path_manifest(path_dir: Path) -> PathManifest:
    try:
        return PathManifest.load((path_dir / "wfpath.yaml").resolve())
    except PathManifestError as exc:
        raise click.ClickException(str(exc)) from exc


def _manifest_skill_dependencies(
    manifest: PathManifest,
    *,
    host: str,
) -> list[dict[str, Any]]:
    dependencies: tuple[PathSkillDependencyConfig, ...] = resolve_skill_dependencies(
        manifest
    )
    return [
        {
            "name": dependency.name,
            "path_slug": dependency.path_slug,
            "required": dependency.required,
            "skill_name": dependency.host_names.get(host) or dependency.name,
        }
        for dependency in dependencies
    ]


def _run_host_doctor(
    *,
    path_dir: Path,
    host: str,
    activated_root: Path | None = None,
    model: str | None = None,
) -> PathDoctorReport:
    if host != "opencode":
        return PathDoctorReport(
            ok=True,
            slug=None,
            version=None,
            primary_kind=None,
            errors=[],
            warnings=[],
            created_files=[],
        )
    try:
        report = run_doctor(
            path_dir=path_dir,
            fix=False,
            overwrite=False,
            host=host,
            activated_root=activated_root,
            model_override=model,
            validation_mode="installed_host",
        )
    except PathDoctorError as exc:
        raise click.ClickException(str(exc)) from exc
    _raise_for_doctor_errors(report)
    return report


def _install_required_dependencies_for_path(
    *,
    path_dir: Path,
    host: str,
    scope: str,
    install_dir: str,
    force: bool,
    no_verify: bool,
    api_url: str | None,
    model: str | None,
    activate: bool,
    visited: set[str],
) -> list[dict[str, Any]]:
    manifest = _load_path_manifest(path_dir)
    dependency_results: list[dict[str, Any]] = []
    for dependency in _manifest_skill_dependencies(manifest, host=host):
        if not dependency["required"]:
            continue
        dependency_slug = str(dependency["path_slug"] or "").strip()
        dependency_skill_name = str(
            dependency.get("skill_name") or dependency.get("name") or dependency_slug
        ).strip()
        bundled_skill_dir = _sdk_skill_source_dir(dependency_slug, host=host)
        if bundled_skill_dir is not None:
            destination_root = _host_skill_directory(host, scope, cwd=Path.cwd())
            destination = destination_root / dependency_skill_name
            _copy_export_tree(bundled_skill_dir, destination)
            dependency_results.append(
                {
                    "slug": dependency_slug,
                    "skill_name": dependency_skill_name,
                    "source": "sdk-bundled",
                    "dest": str(destination),
                    "applied": [str(destination)],
                    "activated": True,
                }
            )
            continue
        dependency_results.append(
            _install_path_with_options(
                slug=dependency_slug,
                path_version=None,
                install_dir=install_dir,
                force=force,
                no_verify=no_verify,
                api_url=api_url,
                host=host,
                scope=scope,
                model=model,
                activate=activate,
                include_dependencies=True,
                _visited=visited,
            )
        )
    return dependency_results


def _resolve_component_execution_target(
    manifest: PathManifest,
    *,
    component_id: str | None = None,
) -> tuple[str, str]:
    component = manifest.resolve_component(component_id)
    component_id_value = str(component.get("id") or "").strip() or "main"
    component_path = str(component.get("path") or "").strip()
    if not component_path:
        raise click.ClickException(f"Component '{component_id_value}' is missing path")
    return component_id_value, component_path


def _run_path_component(
    *,
    path_dir: Path,
    component_id: str | None,
    args: tuple[str, ...] | list[str],
) -> int:
    manifest = _load_path_manifest(path_dir)
    resolved_component_id, component_path = _resolve_component_execution_target(
        manifest,
        component_id=component_id,
    )
    target = (path_dir / component_path).resolve()
    if not target.exists():
        raise click.ClickException(
            f"Component path not found for '{resolved_component_id}': {target}"
        )

    env = os.environ.copy()
    current_pythonpath = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = (
        str(path_dir)
        if not current_pythonpath
        else str(path_dir) + os.pathsep + current_pythonpath
    )
    cmd = [sys.executable, str(target), *list(args)]
    return subprocess.call(cmd, cwd=str(path_dir), env=env)


def _export_single_skill(
    *,
    path_dir: Path,
    host: str,
    model: str | None = None,
) -> tuple[PathDoctorReport | None, PathSkillRenderReport]:
    try:
        doctor_report = run_doctor(path_dir=path_dir, fix=False, overwrite=False)
    except PathDoctorError as exc:
        raise click.ClickException(str(exc)) from exc
    _raise_for_doctor_errors(doctor_report)

    try:
        render_report = render_skill_exports(
            path_dir=path_dir,
            hosts=[host],
            opencode_model_override=model if host == "opencode" else None,
        )
    except PathSkillRenderError as exc:
        raise click.ClickException(str(exc)) from exc
    return doctor_report, render_report


def _render_installed_skill(
    *,
    path_dir: Path,
    host: str,
    model: str | None = None,
) -> PathSkillRenderReport:
    try:
        return render_skill_exports(
            path_dir=path_dir,
            hosts=[host],
            opencode_model_override=model if host == "opencode" else None,
        )
    except PathSkillRenderError as exc:
        raise click.ClickException(str(exc)) from exc


def _copy_export_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)


def _activate_destination(host: str, scope: str, *, cwd: Path) -> Path:
    if host == "claude":
        if scope == "project":
            return cwd / ".claude" / "skills"
        if scope == "personal":
            return Path.home() / ".claude" / "skills"
    elif host == "codex":
        if scope == "repo":
            return cwd / ".agents" / "skills"
        if scope == "user":
            return Path.home() / ".agents" / "skills"
        if scope == "admin":
            return Path("/etc/codex/skills")
    elif host == "openclaw":
        if scope == "workspace":
            return cwd / "skills"
        if scope == "shared":
            return Path.home() / ".openclaw" / "skills"

    raise click.ClickException(f"Unsupported host/scope combination: {host}/{scope}")


def _activate_install_root(host: str, scope: str, *, cwd: Path) -> Path:
    if host == "claude":
        if scope == "project":
            return cwd
        if scope == "personal":
            return Path.home()
    if host == "opencode":
        if scope == "project":
            return cwd
        if scope in {"user", "personal"}:
            return Path.home() / ".config" / "opencode"
    raise click.ClickException(f"Unsupported host/scope combination: {host}/{scope}")


def _read_export_manifest(source_dir: Path) -> dict[str, Any]:
    export_manifest_path = source_dir / "runtime" / "export.json"
    if not export_manifest_path.exists():
        return {}
    try:
        payload = json.loads(export_manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        elif (
            key in merged and isinstance(merged[key], list) and isinstance(value, list)
        ):
            combined: list[Any] = []
            for item in [*merged[key], *value]:
                if item not in combined:
                    combined.append(item)
            merged[key] = combined
        else:
            merged[key] = value
    return merged


def _normalize_host_scope(host: str, scope: str) -> str:
    normalized_host = host.lower()
    normalized_scope = scope.strip().lower()
    if normalized_host == "opencode" and normalized_scope == "personal":
        return "user"
    return normalized_scope


def _host_skill_directory(host: str, scope: str, *, cwd: Path) -> Path:
    normalized_host = host.lower()
    normalized_scope = _normalize_host_scope(host, scope)
    if normalized_host == "opencode":
        return (
            _activate_install_root(normalized_host, normalized_scope, cwd=cwd)
            / ".opencode"
            / "skills"
        )
    return _activate_destination(normalized_host, normalized_scope, cwd=cwd)


def _merge_json_file(dest_path: Path, patch_path: Path) -> None:
    current: dict[str, Any] = {}
    if dest_path.exists():
        try:
            parsed = json.loads(dest_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                current = parsed
        except Exception:
            current = {}
    patch_payload = json.loads(patch_path.read_text(encoding="utf-8"))
    if not isinstance(patch_payload, dict):
        raise click.ClickException(f"Install patch must be a JSON object: {patch_path}")
    merged = _deep_merge(current, patch_payload)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")


def _merge_markdown_file(dest_path: Path, patch_path: Path, *, section_id: str) -> None:
    begin = f"<!-- {section_id}:start -->"
    end = f"<!-- {section_id}:end -->"
    patch_text = patch_path.read_text(encoding="utf-8").strip()
    section = f"{begin}\n{patch_text}\n{end}\n"
    current = dest_path.read_text(encoding="utf-8") if dest_path.exists() else ""
    if begin in current and end in current:
        start_idx = current.index(begin)
        end_idx = current.index(end) + len(end)
        updated = current[:start_idx] + section + current[end_idx:]
    else:
        updated = current.rstrip() + ("\n\n" if current.strip() else "") + section
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(updated.rstrip() + "\n", encoding="utf-8")


def _remove_markdown_section(dest_path: Path, *, section_id: str) -> bool:
    if not dest_path.exists():
        return False
    begin = f"<!-- {section_id}:start -->"
    end = f"<!-- {section_id}:end -->"
    current = dest_path.read_text(encoding="utf-8")
    if begin not in current or end not in current:
        return False
    start_idx = current.index(begin)
    end_idx = current.index(end) + len(end)
    updated = current[:start_idx] + current[end_idx:]
    updated = updated.replace("\n\n\n", "\n\n").strip()
    if updated:
        dest_path.write_text(updated + "\n", encoding="utf-8")
    else:
        dest_path.unlink()
    return True


def _deep_remove(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    updated = dict(base)
    for key, value in patch.items():
        if key not in updated:
            continue
        current = updated[key]
        if isinstance(current, dict) and isinstance(value, dict):
            nested = _deep_remove(current, value)
            if nested:
                updated[key] = nested
            else:
                updated.pop(key, None)
            continue
        if isinstance(current, list) and isinstance(value, list):
            remaining = [item for item in current if item not in value]
            if remaining:
                updated[key] = remaining
            else:
                updated.pop(key, None)
            continue
        if current == value:
            updated.pop(key, None)
    return updated


def _remove_json_patch(dest_path: Path, patch_path: Path) -> bool:
    if not dest_path.exists():
        return False
    try:
        parsed = json.loads(dest_path.read_text(encoding="utf-8"))
        patch_payload = json.loads(patch_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(parsed, dict) or not isinstance(patch_payload, dict):
        return False
    updated = _deep_remove(parsed, patch_payload)
    if updated == parsed:
        return False
    if updated:
        dest_path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    else:
        dest_path.unlink()
    return True


def _prune_empty_parents(path: Path, *, stop_at: Path) -> None:
    stop = stop_at.expanduser().resolve()
    current = path.expanduser().resolve()
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _remove_existing_path(path: Path, *, root: Path) -> bool:
    target = path.expanduser().resolve()
    root_resolved = root.expanduser().resolve()
    if not target.exists():
        return False
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    _prune_empty_parents(target.parent, stop_at=root_resolved)
    return True


def _copy_install_path(source: Path, destination: Path) -> None:
    if source.is_dir():
        _copy_export_tree(source, destination)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if _is_opencode_tool_path(destination):
        source_text = source.read_text(encoding="utf-8")
        destination.write_text(
            _normalize_opencode_tool_result_contract(source_text),
            encoding="utf-8",
        )
        return
    shutil.copy2(source, destination)


def _is_opencode_tool_path(path: Path) -> bool:
    parts = path.parts
    return ".opencode" in parts and "tools" in parts and path.suffix in {".js", ".ts"}


def _normalize_opencode_tool_result_contract(tool_text: str) -> str:
    return _LEGACY_OPENCODE_TOOL_RESULT_RE.sub(
        _OPENCODE_TOOL_RESULT_HELPER,
        tool_text,
    )


def _should_merge_json_install_target(*, op: str, src: Path, dest: Path) -> bool:
    return op == "merge_json" or (
        op == "copy_file" and dest.name == "opencode.json" and src.suffix == ".json"
    )


def _apply_install_targets(source_dir: Path, destination_root: Path) -> list[str]:
    export_manifest = _read_export_manifest(source_dir)
    install_targets = export_manifest.get("install_targets") or []
    if not isinstance(install_targets, list):
        return []
    applied: list[str] = []
    for target in install_targets:
        if not isinstance(target, dict):
            continue
        op = str(target.get("op") or "").strip()
        src = source_dir / str(target.get("source") or "").strip()
        dest = destination_root / str(target.get("destination") or "").strip()
        if _should_merge_json_install_target(op=op, src=src, dest=dest):
            _merge_json_file(dest, src)
            applied.append(str(dest))
            continue
        if op in {"copy_tree", "copy_file"}:
            _copy_install_path(src, dest)
            applied.append(str(dest))
            continue
        if op == "merge_markdown":
            section_id = str(target.get("section_id") or "").strip()
            if not section_id:
                raise click.ClickException(
                    f"merge_markdown target missing section_id: {src}"
                )
            _merge_markdown_file(dest, src, section_id=section_id)
            applied.append(str(dest))
            continue
    return applied


def _remove_install_targets(source_dir: Path, destination_root: Path) -> list[str]:
    export_manifest = _read_export_manifest(source_dir)
    install_targets = export_manifest.get("install_targets") or []
    if not isinstance(install_targets, list):
        return []
    removed: list[str] = []
    for target in reversed(install_targets):
        if not isinstance(target, dict):
            continue
        op = str(target.get("op") or "").strip()
        src = source_dir / str(target.get("source") or "").strip()
        dest = destination_root / str(target.get("destination") or "").strip()
        if _should_merge_json_install_target(op=op, src=src, dest=dest):
            if _remove_json_patch(dest, src):
                removed.append(str(dest))
            continue
        if op in {"copy_tree", "copy_file"}:
            if _remove_existing_path(dest, root=destination_root):
                removed.append(str(dest))
            continue
        if op == "merge_markdown":
            section_id = str(target.get("section_id") or "").strip()
            if section_id and _remove_markdown_section(dest, section_id=section_id):
                removed.append(str(dest))
            continue
    return removed


def _activation_root_from_result(*, mode: str, dest: Path) -> Path:
    if mode == "install":
        return dest
    return dest.parent


def _activation_record(
    *,
    host: str,
    scope: str,
    mode: str,
    root: Path,
    dest: Path,
    applied: list[str],
    model: str | None = None,
    include_dependencies: bool | None = None,
    dependencies: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "host": host,
        "scope": scope,
        "mode": mode,
        "root": str(root),
        "dest": str(dest),
        "applied": list(applied),
        "updated_at": _iso_utc_now(),
    }
    if model:
        payload["model"] = model
    if include_dependencies is not None:
        payload["include_dependencies"] = include_dependencies
    if dependencies:
        payload["dependencies"] = dependencies
    return payload


def _activation_target_from_entry(entry: dict[str, Any]) -> _ActivationTarget | None:
    activation = entry.get("activation")
    if not isinstance(activation, dict):
        return None
    host = str(activation.get("host") or "").strip().lower()
    scope = _normalize_host_scope(host, str(activation.get("scope") or ""))
    if not host or not scope:
        return None
    return _ActivationTarget(host=host, scope=scope, source="lockfile")


def _activation_options_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
    activation = entry.get("activation")
    if not isinstance(activation, dict):
        return {}
    dependencies = activation.get("dependencies")
    return {
        "model": str(activation.get("model") or "").strip() or None,
        "include_dependencies": bool(activation.get("include_dependencies"))
        if "include_dependencies" in activation
        else None,
        "dependencies": (dependencies if isinstance(dependencies, list) else []),
    }


def _activation_root_from_entry(
    entry: dict[str, Any],
    *,
    target: _ActivationTarget | None = None,
) -> Path | None:
    activation = entry.get("activation")
    if not isinstance(activation, dict):
        return None

    host = str(activation.get("host") or "").strip().lower()
    scope = _normalize_host_scope(host, str(activation.get("scope") or ""))
    if target is not None and (host != target.host or scope != target.scope):
        return None

    raw_root = str(activation.get("root") or "").strip()
    if raw_root:
        return Path(raw_root).expanduser().resolve()

    raw_dest = str(activation.get("dest") or "").strip()
    mode = str(activation.get("mode") or "").strip().lower()
    if not raw_dest or mode not in {"install", "copy"}:
        return None

    dest = Path(raw_dest).expanduser().resolve()
    return _activation_root_from_result(mode=mode, dest=dest)


def _infer_activation_target(*, cwd: Path) -> _ActivationTarget | None:
    candidates: list[_ActivationTarget] = []
    markers = (
        (cwd / ".claude", "claude", "project"),
        (cwd / "opencode.json", "opencode", "project"),
        (cwd / ".agents", "codex", "repo"),
        (cwd / "skills", "openclaw", "workspace"),
    )
    for marker, host, scope in markers:
        if marker.exists():
            candidates.append(
                _ActivationTarget(host=host, scope=scope, source="default")
            )
    if len(candidates) != 1:
        return None
    return candidates[0]


def _manual_activate_command(*, path_dir: Path) -> str:
    return (
        "wayfinder path activate --host <host> --scope <scope> "
        f"--path {shlex.quote(str(path_dir))}"
    )


def _installed_path_dir(
    *,
    base: Path,
    slug: str,
    version: str,
    entry: dict[str, Any],
) -> Path:
    raw_path = str(entry.get("path") or "").strip()
    if raw_path:
        candidate = Path(raw_path).expanduser()
        if candidate.is_absolute() or candidate.exists():
            return candidate
    return base / slug / version


def _find_state_dir_for_installed_path(path_dir: Path) -> Path | None:
    resolved = path_dir.expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.name in {_INSTALL_DIRNAME, _LEGACY_INSTALL_DIRNAME}:
            return _state_dir_for_install_root(candidate)
    return None


def _resolve_installed_lock_entry(
    path_dir: Path,
) -> tuple[dict[str, Any], Path, str, dict[str, Any]] | None:
    state_dir = _find_state_dir_for_installed_path(path_dir)
    if state_dir is None:
        return None
    lock, lock_path = _load_install_lock(state_dir)
    resolved_path = path_dir.expanduser().resolve()
    for slug, raw_entry in _lock_paths_map(lock).items():
        if not isinstance(slug, str) or not isinstance(raw_entry, dict):
            continue
        entry_path = str(raw_entry.get("path") or "").strip()
        if not entry_path:
            continue
        try:
            candidate = Path(entry_path).expanduser().resolve()
        except Exception:
            continue
        if candidate == resolved_path:
            return lock, lock_path, slug, raw_entry
    return None


def _record_activation_for_entry(
    *,
    lock: dict[str, Any],
    lock_path: Path,
    slug: str,
    entry: dict[str, Any],
    host: str,
    scope: str,
    mode: str,
    root: Path,
    dest: Path,
    applied: list[str],
    model: str | None = None,
    include_dependencies: bool | None = None,
    dependencies: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    updated_entry = dict(entry)
    updated_entry["activation"] = _activation_record(
        host=host,
        scope=scope,
        mode=mode,
        root=root,
        dest=dest,
        applied=applied,
        model=model,
        include_dependencies=include_dependencies,
        dependencies=dependencies,
    )
    _update_lock_path_entry(lock, lock_path, slug=slug, entry=updated_entry)
    return updated_entry


def _activate_export(
    *,
    host: str,
    scope: str,
    path_dir: Path | None = None,
    export_path: Path | None = None,
    model: str | None = None,
    destination_root: Path | None = None,
) -> dict[str, Any]:
    if (path_dir is None) == (export_path is None):
        raise click.ClickException("Provide exactly one of --path or --export-path")

    normalized_host = host.lower()
    normalized_scope = _normalize_host_scope(host, scope)
    source_dir: Path

    if path_dir is not None:
        installed_entry = _resolve_installed_lock_entry(path_dir)
        # Installed bundles are already published artifacts; avoid re-running
        # doctor because newer SDK validators can reject older live bundles.
        if installed_entry is not None:
            render_report = _render_installed_skill(
                path_dir=path_dir,
                host=normalized_host,
                model=model,
            )
        else:
            _, render_report = _export_single_skill(
                path_dir=path_dir,
                host=normalized_host,
                model=model,
            )
        source_dir = render_report.exports[normalized_host].export_dir
        skill_name = render_report.skill_name or source_dir.name
    else:
        source_dir = export_path.expanduser().resolve() if export_path else Path()
        if not (source_dir / "SKILL.md").exists():
            raise click.ClickException(f"Rendered export not found: {source_dir}")
        skill_name = source_dir.name

    export_manifest = _read_export_manifest(source_dir)
    install_targets = export_manifest.get("install_targets") or []
    if install_targets:
        destination_root_path = destination_root or _activate_install_root(
            normalized_host, normalized_scope, cwd=Path.cwd()
        )
        applied = _apply_install_targets(source_dir, destination_root_path)
        dest = destination_root_path
        mode = "install"
    else:
        destination_root_path = destination_root or _activate_destination(
            normalized_host, normalized_scope, cwd=Path.cwd()
        )
        dest = destination_root_path / skill_name
        _copy_export_tree(source_dir, dest)
        applied = [str(dest)]
        mode = "copy"
    root = _activation_root_from_result(mode=mode, dest=dest)
    return {
        "host": normalized_host,
        "scope": normalized_scope,
        "source": str(source_dir),
        "root": str(root),
        "dest": str(dest),
        "mode": mode,
        "applied": applied,
    }


@click.group(name="path", help="Build, publish, and emit signals for Paths.")
def path_cli() -> None:
    pass


@path_cli.command(
    name="init", help="Scaffold a new path folder (wfpath.yaml + optional applet)."
)
@click.argument("slug")
@click.option(
    "--dir",
    "base_dir",
    default=".",
    show_default=True,
    help="Base directory to create the path in.",
)
@click.option("--name", default=None, help="Path display name (defaults from slug).")
@click.option("--version", default="0.1.0", show_default=True)
@click.option("--summary", default="", show_default=True)
@click.option(
    "--kind",
    "primary_kind",
    default="bundle",
    show_default=True,
    type=click.Choice(
        ["bundle", "monitor", "strategy", "script", "contract", "dashboard", "policy"],
        case_sensitive=False,
    ),
)
@click.option("--tag", "tags", multiple=True, help="Tag (repeatable).")
@click.option("--applet/--no-applet", default=False, show_default=True)
@click.option("--skill/--no-skill", default=True, show_default=True)
@click.option(
    "--template",
    default="basic",
    show_default=True,
    type=click.Choice(["basic", "pipeline"], case_sensitive=False),
)
@click.option("--archetype", default=None, help="Pipeline archetype id.")
@click.option(
    "--overwrite", is_flag=True, help="Overwrite scaffolded files if they exist."
)
def init_cmd(
    slug: str,
    base_dir: str,
    name: str | None,
    version: str,
    summary: str,
    primary_kind: str,
    tags: tuple[str, ...],
    applet: bool,
    skill: bool,
    template: str,
    archetype: str | None,
    overwrite: bool,
) -> None:
    safe_slug = slugify(slug)
    path_dir = Path(base_dir).expanduser() / safe_slug
    try:
        result = init_path(
            path_dir=path_dir,
            slug=safe_slug,
            name=name,
            version=version,
            summary=summary,
            primary_kind=primary_kind.lower(),
            tags=list(tags) if tags else None,
            with_applet=applet,
            with_skill=skill,
            template=template.lower(),
            archetype=archetype,
            overwrite=overwrite,
        )
    except PathScaffoldError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json(
        {
            "ok": True,
            "result": {
                "path_dir": str(result.path_dir),
                "manifest": str(result.manifest_path),
                "created": [
                    str(p.relative_to(result.path_dir)) for p in result.created_files
                ],
                "overwritten": [
                    str(p.relative_to(result.path_dir))
                    for p in result.overwritten_files
                ],
                "skipped": [
                    str(p.relative_to(result.path_dir)) for p in result.skipped_files
                ],
            },
        }
    )


@path_cli.command(name="eval", help="Run fixture-driven path evaluation checks.")
@click.option("--path", "path_dir", default=".", show_default=True)
def eval_cmd(path_dir: str) -> None:
    try:
        report = run_path_eval(path_dir=Path(path_dir))
    except PathEvalError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json(
        {
            "ok": report.ok,
            "result": {
                "slug": report.slug,
                "issues": [
                    {
                        "name": issue.name,
                        "passed": issue.passed,
                        "message": issue.message,
                        "path": issue.path,
                    }
                    for issue in report.issues
                ],
            },
        }
    )
    if not report.ok:
        raise click.ClickException("Path eval reported failures")


@path_cli.command(
    name="doctor", help="Validate a path folder and optionally fix common issues."
)
@click.option("--path", "path_dir", default=".", show_default=True)
@click.option(
    "--host",
    default=None,
    type=click.Choice(["opencode"], case_sensitive=False),
    help="Run host-specific validation.",
)
@click.option("--activated", is_flag=True, help="Validate an activated host install.")
@click.option(
    "--installed",
    default=None,
    help="Installed path slug to validate from the lockfile.",
)
@click.option(
    "--dir",
    "install_dir",
    default=".wayfinder/paths",
    show_default=True,
    help="Base install directory used during install.",
)
@click.option("--model", default=None, help="Optional host model override.")
@click.option(
    "--check",
    is_flag=True,
    help="Validation-only mode. Equivalent to the default behavior.",
)
@click.option("--fix", is_flag=True, help="Create missing recommended files.")
@click.option(
    "--overwrite", is_flag=True, help="Overwrite generated files when using --fix."
)
def doctor_cmd(
    path_dir: str,
    host: str | None,
    activated: bool,
    installed: str | None,
    install_dir: str,
    model: str | None,
    check: bool,
    fix: bool,
    overwrite: bool,
) -> None:
    if check and fix:
        raise click.ClickException("--check cannot be used together with --fix")

    resolved_path_dir = Path(path_dir).expanduser().resolve()
    activated_root: Path | None = None
    if installed:
        base = _canonical_install_root(install_dir)
        state_dir = _state_dir_for_install_root(base)
        lock, _lock_path = _load_install_lock(state_dir)
        entry = _lock_path_entry(lock, installed)
        if entry is None:
            raise click.ClickException(f"Path not found in lockfile: {installed}")
        resolved_version = str(entry.get("version") or "").strip()
        if not resolved_version:
            raise click.ClickException(
                f"Installed path is missing a version in the lockfile: {installed}"
            )
        resolved_path_dir = (
            _installed_path_dir(
                base=base,
                slug=installed,
                version=resolved_version,
                entry=entry,
            )
            .expanduser()
            .resolve()
        )
        if activated:
            activation = entry.get("activation")
            if not isinstance(activation, dict):
                raise click.ClickException(
                    f"No activation metadata recorded for installed path: {installed}"
                )
            activation_host = str(activation.get("host") or "").strip().lower()
            activation_scope = str(activation.get("scope") or "").strip().lower()
            if not activation_host or not activation_scope:
                raise click.ClickException(
                    f"Incomplete activation metadata recorded for installed path: {installed}"
                )
            activated_root = _activate_install_root(
                activation_host,
                activation_scope,
                cwd=Path.cwd(),
            )
    elif activated:
        activated_root = resolved_path_dir

    try:
        report = run_doctor(
            path_dir=resolved_path_dir,
            fix=fix,
            overwrite=overwrite,
            host=host,
            activated_root=activated_root,
            model_override=model,
            validation_mode="installed_host" if activated else "full",
        )
    except PathDoctorError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json({"ok": report.ok, "result": _doctor_result_payload(report)})
    _raise_for_doctor_errors(report)


@path_cli.command(name="fmt", help="Format path metadata and generated skill exports.")
@click.option("--path", "path_dir", default=".", show_default=True)
def fmt_cmd(path_dir: str) -> None:
    try:
        report = format_path(path_dir=Path(path_dir))
    except PathFormatError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json({"ok": True, "result": {"changed_files": report.changed_files}})


@path_cli.command(
    name="render-skill", help="Generate host-specific skill exports under .build/."
)
@click.option("--path", "path_dir", default=".", show_default=True)
def render_skill_cmd(path_dir: str) -> None:
    try:
        report = render_skill_exports(path_dir=Path(path_dir))
    except PathSkillRenderError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json(
        {
            "ok": True,
            "result": {
                "output_root": str(report.output_root),
                "rendered_hosts": report.rendered_hosts,
                "written_files": report.written_files,
            },
        }
    )


@path_cli.command(name="version", help="Print the installed wayfinder-paths version.")
def version_cmd() -> None:
    try:
        click.echo(importlib_metadata.version("wayfinder-paths"))
    except importlib_metadata.PackageNotFoundError:
        click.echo("0.0.0")


@path_cli.command(name="exec", help="Execute a path component from a path directory.")
@click.option(
    "--path-dir",
    "path_dir",
    required=True,
    help="Path to the exported or local path directory.",
)
@click.option(
    "--component",
    default=None,
    help="Component id (defaults to the runtime/default component).",
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def exec_cmd(path_dir: str, component: str | None, args: tuple[str, ...]) -> None:
    rc = _run_path_component(
        path_dir=Path(path_dir).expanduser().resolve(),
        component_id=component,
        args=args,
    )
    raise SystemExit(rc)


@path_cli.command(
    name="export-skill", help="Generate a single thin skill export for one host."
)
@click.option("--path", "path_dir", default=".", show_default=True)
@click.option(
    "--host",
    required=True,
    type=click.Choice(
        ["claude", "opencode", "codex", "openclaw", "portable"],
        case_sensitive=False,
    ),
)
def export_skill_cmd(path_dir: str, host: str) -> None:
    doctor_report, render_report = _export_single_skill(
        path_dir=Path(path_dir),
        host=host.lower(),
    )
    info = render_report.exports[host.lower()]
    _echo_json(
        {
            "ok": True,
            "result": {
                "host": host.lower(),
                "export_dir": str(info.export_dir),
                "filename": info.filename,
                "mode": info.mode,
                "runtime": info.runtime_manifest,
                "warnings": _skill_export_warning_strings(doctor_report),
            },
        }
    )


@path_cli.command(
    name="activate", help="Install a rendered skill export into a host skill directory."
)
@click.option(
    "--host",
    required=True,
    type=click.Choice(
        ["claude", "opencode", "codex", "openclaw"], case_sensitive=False
    ),
)
@click.option(
    "--scope",
    required=True,
    help="Host scope (e.g. project, personal, repo, user, admin, workspace, shared).",
)
@click.option(
    "--slug", default=None, help="Installed path slug to activate from the lockfile."
)
@click.option(
    "--version", "path_version", default=None, help="Installed path version override."
)
@click.option(
    "--dir",
    "install_dir",
    default=".wayfinder/paths",
    show_default=True,
    help="Base install directory used during install.",
)
@click.option(
    "--path", "path_dir", default=None, help="Local path directory to render from."
)
@click.option(
    "--export-path", default=None, help="Existing rendered skill export directory."
)
@click.option("--model", default=None, help="Optional host model override.")
def activate_cmd(
    host: str,
    scope: str,
    slug: str | None,
    path_version: str | None,
    install_dir: str,
    path_dir: str | None,
    export_path: str | None,
    model: str | None,
) -> None:
    if slug and (path_dir or export_path):
        raise click.ClickException("--slug cannot be used with --path or --export-path")
    source_path: Path | None
    if slug:
        base = _canonical_install_root(install_dir)
        state_dir = _state_dir_for_install_root(base)
        lock, _lock_path = _load_install_lock(state_dir)
        entry = _lock_path_entry(lock, slug)
        if entry is None:
            raise click.ClickException(f"Path not found in lockfile: {slug}")
        resolved_version = path_version or str(entry.get("version") or "").strip()
        if not resolved_version:
            raise click.ClickException(
                f"Installed path is missing a version in the lockfile: {slug}"
            )
        source_path = (
            _installed_path_dir(
                base=base,
                slug=slug,
                version=resolved_version,
                entry=entry,
            )
            .expanduser()
            .resolve()
        )
    else:
        source_path = Path(path_dir).expanduser().resolve() if path_dir else None
    rendered_export_path = (
        Path(export_path).expanduser().resolve() if export_path else None
    )
    result = _activate_export(
        host=host,
        scope=scope,
        path_dir=source_path,
        export_path=rendered_export_path,
        model=model,
    )

    activation_recorded = False
    if source_path is not None:
        resolved_entry = _resolve_installed_lock_entry(source_path)
        if resolved_entry is not None:
            lock, lock_path, slug, entry = resolved_entry
            _record_activation_for_entry(
                lock=lock,
                lock_path=lock_path,
                slug=slug,
                entry=entry,
                host=str(result["host"]),
                scope=str(result["scope"]),
                mode=str(result["mode"]),
                root=Path(
                    str(
                        result.get("root")
                        or _activation_root_from_result(
                            mode=str(result["mode"]),
                            dest=Path(str(result["dest"])),
                        )
                    )
                ),
                dest=Path(str(result["dest"])),
                applied=[str(item) for item in result["applied"]],
                model=model,
            )
            activation_recorded = True

    result["activation_recorded"] = activation_recorded
    _echo_json({"ok": True, "result": result})


@path_cli.command(
    name="preview", help="Serve a local parent-shell preview for this path's applet."
)
@click.option("--path", "path_dir", default=".", show_default=True)
@click.option(
    "--check",
    is_flag=True,
    help="Validate preview prerequisites without starting local servers.",
)
@click.option("--parent-port", default=3333, show_default=True, type=int)
@click.option("--applet-port", default=3334, show_default=True, type=int)
def preview_cmd(
    path_dir: str,
    check: bool,
    parent_port: int,
    applet_port: int,
) -> None:
    try:
        if check:
            inspection = inspect_preview_path(path_dir=Path(path_dir))
            _echo_json(
                {
                    "ok": True,
                    "result": {
                        "slug": inspection.slug,
                        "name": inspection.name,
                        "applet_root": str(inspection.applet_root),
                        "entry": inspection.entry,
                        "entry_path": str(inspection.entry_path),
                    },
                }
            )
            return

        preview_path(
            path_dir=Path(path_dir),
            parent_port=parent_port,
            applet_port=applet_port,
        )
    except PathPreviewError as exc:
        raise click.ClickException(str(exc)) from exc


@path_cli.group(name="hooks", help="Install local git hook automation for a path.")
def hooks_group() -> None:
    pass


@hooks_group.command(name="install", help="Write or update .pre-commit-config.yaml.")
@click.option("--path", "path_dir", default=".", show_default=True)
def hooks_install_cmd(path_dir: str) -> None:
    try:
        report = install_path_hooks(path_dir=Path(path_dir))
    except PathHooksError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json(
        {
            "ok": True,
            "result": {
                "config_path": str(report.config_path),
                "changed": report.changed,
                "hooks": report.hooks,
            },
        }
    )


@path_cli.command(name="build", help="Create a bundle.zip from a path directory.")
@click.option("--path", "path_dir", default=".", show_default=True)
@click.option("--out", "out_path", default="dist/bundle.zip", show_default=True)
def build_cmd(path_dir: str, out_path: str) -> None:
    path_dir = Path(path_dir)
    doctor_report, render_report = _prepare_path_for_build(path_dir)

    try:
        built = PathBuilder.build(path_dir=path_dir, out_path=Path(out_path))
    except PathBuildError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json(
        {
            "ok": True,
            "result": {
                "slug": built.manifest.slug,
                "version": built.manifest.version,
                "bundle_path": str(built.bundle_path),
                "bundle_sha256": built.bundle_sha256,
                "warnings": len(doctor_report.warnings),
                "rendered_hosts": render_report.rendered_hosts,
            },
        }
    )


@path_cli.command(
    name="publish", help="Build and publish a path bundle to the Paths API."
)
@click.option("--path", "path_dir", default=".", show_default=True)
@click.option("--out", "out_path", default="dist/bundle.zip", show_default=True)
@click.option("--api-url", "api_url", default=None, help="Override Paths API base URL.")
@click.option(
    "--source", "source_path", default=None, help="Optional source.zip to upload."
)
@click.option("--bonded/--unbonded", default=False, show_default=True)
@click.option(
    "--owner-wallet",
    default=None,
    help="Owner wallet for bonded publish metadata and contract args.",
)
@click.option(
    "--risk-tier",
    default=None,
    type=click.Choice(["read_only", "interactive", "execution"], case_sensitive=False),
    help="Requested risk tier for bonded publish.",
)
def publish_cmd(
    path_dir: str,
    out_path: str,
    api_url: str | None,
    source_path: str | None,
    bonded: bool,
    owner_wallet: str | None,
    risk_tier: str | None,
) -> None:
    path_dir = Path(path_dir)
    doctor_report, render_report = _prepare_path_for_build(path_dir)

    if bonded and not owner_wallet:
        raise click.ClickException("--owner-wallet is required with --bonded")

    try:
        built = PathBuilder.build(path_dir=path_dir, out_path=Path(out_path))
    except PathBuildError as exc:
        raise click.ClickException(str(exc)) from exc

    resolved_source_path = (
        Path(source_path) if source_path else built.bundle_path.parent / "source.zip"
    )
    try:
        PathBuilder.build_source_archive(
            path_dir=path_dir, out_path=resolved_source_path
        )
    except PathBuildError as exc:
        raise click.ClickException(str(exc)) from exc

    exports_manifest, skill_exports = _collect_skill_export_uploads(
        render_report,
        doctor_report,
    )
    client = PathsApiClient(api_base_url=api_url)
    try:
        resp = client.publish(
            bundle_path=built.bundle_path,
            source_path=resolved_source_path,
            exports_manifest=exports_manifest,
            skill_exports=skill_exports,
            manifest=built.manifest.raw,
            applet_meta=_load_applet_meta(path_dir, built.manifest),
            has_skill=bool(
                built.manifest.skill or (path_dir / "skill" / "SKILL.md").exists()
            ),
            owner_wallet=owner_wallet,
            bonded=bonded,
            risk_tier=risk_tier.lower() if risk_tier else None,
        )
    except PathsApiError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json({"ok": True, "result": resp})
    if resp.get("ownerLinkRequired"):
        manage_url = resp.get("manageUrl", "")
        click.echo(f"\nLink owner wallet and bond at: {manage_url}", err=True)
    elif resp.get("manageUrl"):
        click.echo(f"\nManage at: {resp['manageUrl']}", err=True)
    if resp.get("effectiveRiskTier"):
        click.echo(
            f"Effective risk tier: {resp['effectiveRiskTier']}",
            err=True,
        )
    if resp.get("requiredInitialBond"):
        click.echo(
            f"Required initial bond: {resp['requiredInitialBond']}",
            err=True,
        )
    if resp.get("requiredUpgradePendingBond"):
        click.echo(
            f"Required upgrade pending bond: {resp['requiredUpgradePendingBond']}",
            err=True,
        )
    if resp.get("reservationExpiresAt"):
        click.echo(
            f"Temporary slug reservation expires at: {resp['reservationExpiresAt']}",
            err=True,
        )
    if resp.get("slugPermanent") is True:
        click.echo("Slug reservation is permanent.", err=True)
    elif resp.get("slugPermanent") is False:
        click.echo(
            "Slug reservation is temporary until approval/publication.", err=True
        )


@path_cli.command(name="search", help="Search paths in the registry.")
@click.argument("query", required=False, default="")
@click.option("--tag", default=None, help="Filter by tag.")
@click.option("--owner-wallet", default=None, help="Filter by owner wallet.")
@click.option("--limit", default=25, show_default=True, type=int)
@click.option("--api-url", "api_url", default=None, help="Override Paths API base URL.")
def search_cmd(
    query: str,
    tag: str | None,
    owner_wallet: str | None,
    limit: int,
    api_url: str | None,
) -> None:
    q = (query or "").strip().lower()
    limit = max(1, min(int(limit or 25), 200))

    client = PathsApiClient(api_base_url=api_url)
    try:
        paths = client.list_paths(owner_wallet=owner_wallet, tag=tag)
    except PathsApiError as exc:
        raise click.ClickException(str(exc)) from exc

    if q:

        def matches(p: dict[str, Any]) -> bool:
            blob = " ".join(
                [
                    str(p.get("slug", "")),
                    str(p.get("name", "")),
                    str(p.get("summary", "")),
                    " ".join([str(t) for t in (p.get("tags") or []) if t]),
                ]
            ).lower()
            return q in blob

        paths = [p for p in paths if matches(p)]

    _echo_json(
        {
            "ok": True,
            "result": {"count": len(paths), "paths": paths[:limit]},
        }
    )


@path_cli.command(name="info", help="Fetch path metadata (path + versions).")
@click.option("--slug", required=True, help="Path slug.")
@click.option("--api-url", "api_url", default=None, help="Override Paths API base URL.")
def info_cmd(slug: str, api_url: str | None) -> None:
    client = PathsApiClient(api_base_url=api_url)
    try:
        data = client.get_path(slug=slug)
    except PathsApiError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_json({"ok": True, "result": data})


@path_cli.command(name="fork", help="Fork a path in the registry.")
@click.option("--slug", required=True, help="Parent path slug.")
@click.option(
    "--version", "path_version", default=None, help="Path version (defaults to latest)."
)
@click.option("--new-slug", default=None, help="Slug for the fork (optional).")
@click.option("--name", default=None, help="Name for the fork (optional).")
@click.option("--summary", default=None, help="Summary for the fork (optional).")
@click.option(
    "--owner-wallet", default=None, help="Owner wallet for the fork (optional)."
)
@click.option("--api-url", "api_url", default=None, help="Override Paths API base URL.")
def fork_cmd(
    slug: str,
    path_version: str | None,
    new_slug: str | None,
    name: str | None,
    summary: str | None,
    owner_wallet: str | None,
    api_url: str | None,
) -> None:
    client = PathsApiClient(api_base_url=api_url)
    try:
        resp = client.fork_path(
            slug=slug,
            version=path_version,
            new_slug=new_slug,
            name=name,
            summary=summary,
            owner_wallet=owner_wallet,
        )
    except PathsApiError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json({"ok": True, "result": resp})


def _safe_extract_zip(zip_path: Path, *, dest_dir: Path) -> list[str]:
    extracted: list[str] = []
    dest_dir.mkdir(parents=True, exist_ok=True)
    with ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            rel = Path(name)
            if rel.is_absolute() or ".." in rel.parts:
                continue
            target = (dest_dir / rel).resolve()
            if (
                dest_dir.resolve() not in target.parents
                and target != dest_dir.resolve()
            ):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, target.open("wb") as out:
                out.write(src.read())
            extracted.append(rel.as_posix())
    extracted.sort()
    return extracted


def _read_path_registry_detail(
    client: PathsApiClient, *, slug: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        detail = client.get_path(slug=slug)
    except PathsApiError as exc:
        raise click.ClickException(str(exc)) from exc

    path_obj = detail.get("path") if isinstance(detail, dict) else None
    versions = detail.get("versions") if isinstance(detail, dict) else None
    if not isinstance(path_obj, dict) or not isinstance(versions, list) or not versions:
        raise click.ClickException("Path not found or has no versions")
    normalized_versions = [item for item in versions if isinstance(item, dict)]
    if not normalized_versions:
        raise click.ClickException("Path not found or has no versions")
    return path_obj, normalized_versions


def _resolve_path_version_payload(
    client: PathsApiClient,
    *,
    slug: str,
    versions: list[dict[str, Any]],
    desired_version: str,
) -> dict[str, Any]:
    version_obj = next(
        (v for v in versions if str(v.get("version") or "").strip() == desired_version),
        None,
    )
    if not isinstance(version_obj, dict):
        try:
            version_detail = client.get_path_version(slug=slug, version=desired_version)
        except PathsApiError as exc:
            raise click.ClickException(str(exc)) from exc
        version_obj = (
            version_detail.get("version") if isinstance(version_detail, dict) else None
        )

    if not isinstance(version_obj, dict):
        raise click.ClickException(f"Version not found: {desired_version}")
    return version_obj


def _default_install_version(
    *,
    path_obj: dict[str, Any],
    versions: list[dict[str, Any]],
    path_version: str | None,
) -> str:
    desired_version = (
        path_version or str(path_obj.get("latest_version") or "")
    ).strip()
    if not desired_version:
        desired_version = str(versions[0].get("version") or "").strip()
    if not desired_version:
        raise click.ClickException("Path has no published versions")
    return desired_version


def _default_update_version(
    *,
    path_obj: dict[str, Any],
    path_version: str | None,
) -> str:
    desired_version = (
        path_version or str(path_obj.get("active_bonded_version") or "")
    ).strip()
    if not desired_version:
        raise click.ClickException(
            "Path has no active bonded version. Use --version to install a specific public version."
        )
    return desired_version


def _install_path_version(
    *,
    client: PathsApiClient,
    slug: str,
    desired_version: str,
    version_obj: dict[str, Any],
    install_dir: str,
    force: bool,
    no_verify: bool,
) -> dict[str, Any]:
    venue = _path_install_venue(runtime="sdk-cli")

    expected_sha = str(version_obj.get("bundle_sha256") or "").strip()
    if not expected_sha and not no_verify:
        raise click.ClickException("Version is missing bundle_sha256 (cannot verify)")

    base = _canonical_install_root(install_dir)
    state_dir = _state_dir_for_install_root(base)
    dest = base / slug / desired_version
    bundle_path = dest / "bundle.zip"

    intent_payload: dict[str, Any] | None = None
    intent_signature = ""
    warnings: list[str] = []

    if dest.exists() and any(dest.iterdir()) and not force:
        raise click.ClickException(f"Destination already exists (use --force): {dest}")

    try:
        intent_resp = client.create_install_intent(
            slug=slug,
            version=desired_version,
            runtime="sdk-cli",
            venue=venue,
            install_target=str(dest),
        )
        payload = intent_resp.get("intent")
        signature = intent_resp.get("signature")
        if isinstance(payload, dict) and isinstance(signature, str) and signature:
            intent_payload = payload
            intent_signature = signature
    except PathsApiError as exc:
        warnings.append(f"Could not create install intent: {exc}")

    dest.mkdir(parents=True, exist_ok=True)
    if intent_payload and intent_signature:
        (dest / "install-intent.json").write_text(
            json.dumps(
                {"intent": intent_payload, "signature": intent_signature},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    try:
        client.download_bundle(slug=slug, version=desired_version, out_path=bundle_path)
    except PathsApiError as exc:
        raise click.ClickException(str(exc)) from exc

    actual_sha = _sha256_file(bundle_path)
    if not no_verify and expected_sha and actual_sha.lower() != expected_sha.lower():
        raise click.ClickException(
            f"Bundle SHA-256 mismatch (expected {expected_sha}, got {actual_sha})"
        )

    extracted = _safe_extract_zip(bundle_path, dest_dir=dest)

    lock, lock_path = _load_install_lock(state_dir)
    existing_entry = _lock_path_entry(lock, slug) or {}
    entry = dict(existing_entry)
    entry.update(
        {
            "version": desired_version,
            "bundle_sha256": actual_sha,
            "venue": venue,
            "installed_at": _iso_utc_now(),
            "path": str(dest),
        }
    )
    entry["installation_id"] = None
    entry["heartbeat_token"] = None
    _update_lock_path_entry(lock, lock_path, slug=slug, entry=entry)

    receipt_status = "skipped"
    installation_id = None
    heartbeat_token = None
    if intent_payload and intent_signature:
        try:
            receipt_resp = client.submit_install_receipt(
                slug=slug,
                intent=intent_payload,
                signature=intent_signature,
                runtime="sdk-cli",
                venue=venue,
                install_path=str(dest),
                extracted_files=len(extracted),
            )
            receipt_status = str(receipt_resp.get("status", "recorded"))
            installation_id = receipt_resp.get("installation_id")
            heartbeat_token = receipt_resp.get("heartbeat_token")
        except PathsApiError as exc:
            warnings.append(f"Could not submit install receipt: {exc}")
            receipt_status = "error"

    lock, lock_path = _load_install_lock(state_dir)
    entry = dict(_lock_path_entry(lock, slug) or {})
    entry.update(
        {
            "version": desired_version,
            "bundle_sha256": actual_sha,
            "venue": venue,
            "installed_at": entry.get("installed_at") or _iso_utc_now(),
            "path": str(dest),
            "installation_id": installation_id,
            "heartbeat_token": heartbeat_token,
        }
    )
    _update_lock_path_entry(lock, lock_path, slug=slug, entry=entry)

    return {
        "version": desired_version,
        "bundle_path": str(bundle_path),
        "bundle_sha256": actual_sha,
        "dest": str(dest),
        "extracted_files": len(extracted),
        "lockfile": str(lock_path),
        "install_intent_id": intent_payload.get("intent_id")
        if intent_payload
        else None,
        "installation_id": installation_id,
        "heartbeat_enabled": bool(installation_id and heartbeat_token),
        "verified_install": receipt_status in {"recorded", "duplicate"},
        "install_receipt_status": receipt_status,
        "warnings": warnings,
    }


def _install_path(
    *,
    slug: str,
    path_version: str | None,
    install_dir: str,
    force: bool,
    no_verify: bool,
    api_url: str | None,
) -> dict[str, Any]:
    return _install_path_with_options(
        slug=slug,
        path_version=path_version,
        install_dir=install_dir,
        force=force,
        no_verify=no_verify,
        api_url=api_url,
    )


def _install_path_with_options(
    *,
    slug: str,
    path_version: str | None,
    install_dir: str,
    force: bool,
    no_verify: bool,
    api_url: str | None,
    host: str | None = None,
    scope: str | None = None,
    model: str | None = None,
    activate: bool = False,
    include_dependencies: bool = False,
    _visited: set[str] | None = None,
) -> dict[str, Any]:
    visited = set(_visited or set())
    if slug in visited:
        return {"slug": slug, "skipped": True, "reason": "already_visited"}
    visited.add(slug)

    client = PathsApiClient(api_base_url=api_url)
    path_obj, versions = _read_path_registry_detail(client, slug=slug)
    desired_version = _default_install_version(
        path_obj=path_obj,
        versions=versions,
        path_version=path_version,
    )
    version_obj = _resolve_path_version_payload(
        client,
        slug=slug,
        versions=versions,
        desired_version=desired_version,
    )
    result = _install_path_version(
        client=client,
        slug=slug,
        desired_version=desired_version,
        version_obj=version_obj,
        install_dir=install_dir,
        force=force,
        no_verify=no_verify,
    )
    response: dict[str, Any] = {"slug": slug, **result}
    installed_path = Path(str(result["dest"]))

    dependency_results: list[dict[str, Any]] = []
    normalized_host = str(host or "").strip().lower() or None
    normalized_scope = (
        _normalize_host_scope(normalized_host, scope)
        if normalized_host and scope
        else None
    )
    if normalized_host and normalized_scope and include_dependencies:
        dependency_results = _install_required_dependencies_for_path(
            path_dir=installed_path,
            host=normalized_host,
            scope=normalized_scope,
            install_dir=install_dir,
            force=force,
            no_verify=no_verify,
            api_url=api_url,
            model=model,
            activate=activate,
            visited=visited,
        )
    if dependency_results:
        response["dependencies"] = dependency_results

    if activate and normalized_host and normalized_scope:
        activation_result = _activate_export(
            host=normalized_host,
            scope=normalized_scope,
            path_dir=installed_path,
            model=model,
        )
        _run_host_doctor(
            path_dir=installed_path,
            host=normalized_host,
            activated_root=Path(
                str(
                    activation_result.get("root")
                    or _activation_root_from_result(
                        mode=str(activation_result["mode"]),
                        dest=Path(str(activation_result["dest"])),
                    )
                )
            ),
            model=model,
        )
        base = _canonical_install_root(install_dir)
        state_dir = _state_dir_for_install_root(base)
        lock, lock_path = _load_install_lock(state_dir)
        entry = _lock_path_entry(lock, slug) or {}
        _record_activation_for_entry(
            lock=lock,
            lock_path=lock_path,
            slug=slug,
            entry=entry,
            host=normalized_host,
            scope=normalized_scope,
            mode=str(activation_result["mode"]),
            root=Path(
                str(
                    activation_result.get("root")
                    or _activation_root_from_result(
                        mode=str(activation_result["mode"]),
                        dest=Path(str(activation_result["dest"])),
                    )
                )
            ),
            dest=Path(str(activation_result["dest"])),
            applied=[str(item) for item in activation_result["applied"]],
            model=model,
            include_dependencies=include_dependencies,
            dependencies=[
                {
                    "path_slug": item.get("slug"),
                    "version": item.get("version"),
                }
                for item in dependency_results
                if isinstance(item, dict) and item.get("version")
            ],
        )
        response["activated"] = True
        response["activation"] = activation_result
        response["next_steps"] = (
            [
                "Restart OpenCode if it was already running so new plugins are loaded.",
                f"Run /{_load_path_manifest(installed_path).pipeline.entry_command if _load_path_manifest(installed_path).pipeline and _load_path_manifest(installed_path).pipeline.entry_command else _load_path_manifest(installed_path).skill.name}.",
            ]
            if normalized_host == "opencode"
            else []
        )
    return response


def _explicit_activation_target(
    *, host: str | None, scope: str | None
) -> _ActivationTarget | None:
    host_value = str(host or "").strip().lower()
    scope_value = str(scope or "").strip().lower()
    if not host_value and not scope_value:
        return None
    if not host_value or not scope_value:
        raise click.ClickException("--host and --scope must be provided together")
    return _ActivationTarget(host=host_value, scope=scope_value, source="explicit")


def _resolve_update_activation_target(
    *,
    entry: dict[str, Any],
    host: str | None,
    scope: str | None,
    cwd: Path,
) -> _ActivationTarget | None:
    explicit_target = _explicit_activation_target(host=host, scope=scope)
    if explicit_target is not None:
        return explicit_target
    lock_target = _activation_target_from_entry(entry)
    if lock_target is not None:
        return lock_target
    return _infer_activation_target(cwd=cwd)


def _remove_path_install(
    *,
    slug: str,
    install_dir: str,
    host: str | None,
    scope: str | None,
) -> dict[str, Any]:
    base = _canonical_install_root(install_dir)
    state_dir = _state_dir_for_install_root(base)
    lock, lock_path = _load_install_lock(state_dir)
    if not lock_path.exists() and not (state_dir / _LEGACY_LOCKFILE_NAME).exists():
        raise click.ClickException(f"Lockfile not found: {lock_path}")

    entry = _lock_path_entry(lock, slug)
    if entry is None:
        raise click.ClickException(f"Path not found in lockfile: {slug}")

    version = str(entry.get("version") or "").strip()
    installed_path = _installed_path_dir(
        base=base,
        slug=slug,
        version=version,
        entry=entry,
    )
    result: dict[str, Any] = {
        "slug": slug,
        "version": version or None,
        "removed": False,
        "deactivated": False,
        "activation_source": "none",
        "removed_paths": [],
        "lockfile": str(lock_path),
    }

    activation_target = _resolve_update_activation_target(
        entry=entry,
        host=host,
        scope=scope,
        cwd=Path.cwd(),
    )
    if activation_target is not None and installed_path.exists():
        activation_options = _activation_options_from_entry(entry)
        activation_root = _activation_root_from_entry(entry, target=activation_target)
        if activation_root is not None and activation_root.exists():
            render_report = _render_installed_skill(
                path_dir=installed_path,
                host=activation_target.host,
                model=activation_options.get("model"),
            )
            source_dir = render_report.exports[activation_target.host].export_dir
            removed_paths = _remove_install_targets(source_dir, activation_root)
            result["deactivated"] = bool(removed_paths)
            result["activation_source"] = activation_target.source
            result["removed_paths"] = removed_paths

    if installed_path.exists():
        shutil.rmtree(installed_path)
        result["removed"] = True

    paths_map = _lock_paths_map(lock)
    paths_map.pop(slug, None)
    lock["paths"] = paths_map
    _write_install_lock(lock_path, lock)
    return result


@path_cli.command(name="install", help="Download and unpack a path bundle locally.")
@click.option("--slug", required=True, help="Path slug.")
@click.option(
    "--version", "path_version", default=None, help="Path version (defaults to latest)."
)
@click.option(
    "--dir",
    "install_dir",
    default=".wayfinder/paths",
    show_default=True,
    help="Base install directory.",
)
@click.option("--force", is_flag=True, help="Overwrite existing files.")
@click.option("--no-verify", is_flag=True, help="Skip bundle SHA-256 verification.")
@click.option(
    "--host",
    default=None,
    type=click.Choice(
        ["claude", "opencode", "codex", "openclaw"], case_sensitive=False
    ),
    help="Optional host activation target.",
)
@click.option("--scope", default=None, help="Optional activation scope.")
@click.option(
    "--activate/--no-activate",
    default=None,
    help="Activate after install. Defaults to on when --host and --scope are provided.",
)
@click.option("--model", default=None, help="Optional host model override.")
@click.option(
    "--include-dependencies/--no-include-dependencies",
    default=True,
    help="Install required dependency skills for supported hosts.",
)
@click.option("--api-url", "api_url", default=None, help="Override Paths API base URL.")
def install_cmd(
    slug: str,
    path_version: str | None,
    install_dir: str,
    force: bool,
    no_verify: bool,
    host: str | None,
    scope: str | None,
    activate: bool | None,
    model: str | None,
    include_dependencies: bool,
    api_url: str | None,
) -> None:
    if (host is None) != (scope is None):
        raise click.ClickException("--host and --scope must be provided together")
    should_activate = bool(activate) if activate is not None else bool(host and scope)
    result = _install_path_with_options(
        slug=slug,
        path_version=path_version,
        install_dir=install_dir,
        force=force,
        no_verify=no_verify,
        host=host,
        scope=scope,
        model=model,
        activate=should_activate,
        include_dependencies=include_dependencies,
        api_url=api_url,
    )
    _echo_json({"ok": True, "result": result})


@path_cli.command(
    name="pull", help="Alias for install: download and unpack a path locally."
)
@click.option("--slug", required=True, help="Path slug.")
@click.option(
    "--version", "path_version", default=None, help="Path version (defaults to latest)."
)
@click.option(
    "--dir",
    "install_dir",
    default=".wayfinder/paths",
    show_default=True,
    help="Base install directory.",
)
@click.option("--force", is_flag=True, help="Overwrite existing files.")
@click.option("--no-verify", is_flag=True, help="Skip bundle SHA-256 verification.")
@click.option("--api-url", "api_url", default=None, help="Override Paths API base URL.")
def pull_cmd(
    slug: str,
    path_version: str | None,
    install_dir: str,
    force: bool,
    no_verify: bool,
    api_url: str | None,
) -> None:
    result = _install_path(
        slug=slug,
        path_version=path_version,
        install_dir=install_dir,
        force=force,
        no_verify=no_verify,
        api_url=api_url,
    )
    _echo_json({"ok": True, "result": result})


@path_cli.command(
    name="update", help="Update an installed path to the live bonded version."
)
@click.argument("slug")
@click.option(
    "--version",
    "path_version",
    default=None,
    help="Optional public version override instead of the live bonded version.",
)
@click.option(
    "--dir",
    "install_dir",
    default=".wayfinder/paths",
    show_default=True,
    help="Base install directory.",
)
@click.option("--force", is_flag=True, help="Overwrite an existing target version.")
@click.option("--no-verify", is_flag=True, help="Skip bundle SHA-256 verification.")
@click.option("--api-url", "api_url", default=None, help="Override Paths API base URL.")
@click.option(
    "--host",
    default=None,
    type=click.Choice(
        ["claude", "opencode", "codex", "openclaw"], case_sensitive=False
    ),
    help="Activation host override.",
)
@click.option("--scope", default=None, help="Activation scope override.")
def update_cmd(
    slug: str,
    path_version: str | None,
    install_dir: str,
    force: bool,
    no_verify: bool,
    api_url: str | None,
    host: str | None,
    scope: str | None,
) -> None:
    base = _canonical_install_root(install_dir)
    state_dir = _state_dir_for_install_root(base)
    lock, lock_path = _load_install_lock(state_dir)
    if not lock_path.exists() and not (state_dir / _LEGACY_LOCKFILE_NAME).exists():
        raise click.ClickException(f"Lockfile not found: {lock_path}")

    entry = _lock_path_entry(lock, slug)
    if entry is None:
        raise click.ClickException(f"Path not found in lockfile: {slug}")

    current_version = str(entry.get("version") or "").strip()
    if not current_version:
        raise click.ClickException(
            f"Installed path is missing a version in the lockfile: {slug}"
        )

    client = PathsApiClient(api_base_url=api_url)
    path_obj, versions = _read_path_registry_detail(client, slug=slug)
    target_version = _default_update_version(
        path_obj=path_obj,
        path_version=path_version,
    )
    activation_entry = entry
    installed_path = _installed_path_dir(
        base=base,
        slug=slug,
        version=current_version,
        entry=entry,
    )

    result: dict[str, Any] = {
        "slug": slug,
        "current_version": current_version,
        "target_version": target_version,
        "updated": False,
        "activated": False,
        "activation_source": "none",
        "manual_activate_command": None,
        "warnings": [],
        "lockfile": str(lock_path),
    }

    if current_version != target_version:
        version_obj = _resolve_path_version_payload(
            client,
            slug=slug,
            versions=versions,
            desired_version=target_version,
        )
        install_result = _install_path_version(
            client=client,
            slug=slug,
            desired_version=target_version,
            version_obj=version_obj,
            install_dir=install_dir,
            force=force,
            no_verify=no_verify,
        )
        result["updated"] = True
        result["install"] = install_result
        result["lockfile"] = install_result["lockfile"]
        result["warnings"] = list(install_result.get("warnings") or [])
        installed_path = Path(str(install_result["dest"]))
        lock, lock_path = _load_install_lock(state_dir)
        activation_entry = _lock_path_entry(lock, slug) or activation_entry

    activation_target = _resolve_update_activation_target(
        entry=activation_entry,
        host=host,
        scope=scope,
        cwd=Path.cwd(),
    )
    activation_options = _activation_options_from_entry(activation_entry)
    activation_model = activation_options.get("model")
    include_dependencies = activation_options.get("include_dependencies")
    if include_dependencies is None:
        include_dependencies = False
    if activation_target is None:
        result["manual_activate_command"] = _manual_activate_command(
            path_dir=installed_path
        )
        _echo_json({"ok": True, "result": result})
        return

    activation_root = _activation_root_from_entry(
        activation_entry,
        target=activation_target,
    )
    if activation_root is not None and not activation_root.exists():
        result["warnings"].append(
            f"Recorded activation root no longer exists: {activation_root}. Falling back to the current environment."
        )
        activation_root = None

    if include_dependencies:
        result["dependencies"] = _install_required_dependencies_for_path(
            path_dir=installed_path,
            host=activation_target.host,
            scope=activation_target.scope,
            install_dir=install_dir,
            force=force,
            no_verify=no_verify,
            api_url=api_url,
            model=activation_model,
            activate=False,
            visited={slug},
        )
    activation_result = _activate_export(
        host=activation_target.host,
        scope=activation_target.scope,
        path_dir=installed_path,
        model=activation_model,
        destination_root=activation_root,
    )
    _run_host_doctor(
        path_dir=installed_path,
        host=activation_target.host,
        activated_root=Path(
            str(
                activation_result.get("root")
                or _activation_root_from_result(
                    mode=str(activation_result["mode"]),
                    dest=Path(str(activation_result["dest"])),
                )
            )
        ),
        model=activation_model,
    )
    lock, lock_path = _load_install_lock(state_dir)
    updated_entry = _lock_path_entry(lock, slug) or {}
    _record_activation_for_entry(
        lock=lock,
        lock_path=lock_path,
        slug=slug,
        entry=updated_entry,
        host=activation_target.host,
        scope=activation_target.scope,
        mode=str(activation_result["mode"]),
        root=Path(
            str(
                activation_result.get("root")
                or _activation_root_from_result(
                    mode=str(activation_result["mode"]),
                    dest=Path(str(activation_result["dest"])),
                )
            )
        ),
        dest=Path(str(activation_result["dest"])),
        applied=[str(item) for item in activation_result["applied"]],
        model=activation_model,
        include_dependencies=bool(include_dependencies),
        dependencies=result.get("dependencies"),
    )

    result["activated"] = True
    result["activation_source"] = activation_target.source
    result["activation"] = activation_result
    _echo_json({"ok": True, "result": result})


@path_cli.command(
    name="remove",
    help="Remove an installed path and deactivate it from the selected host scope.",
)
@click.argument("slug")
@click.option(
    "--dir",
    "install_dir",
    default=".wayfinder/paths",
    show_default=True,
    help="Base install directory.",
)
@click.option(
    "--host",
    default=None,
    type=click.Choice(
        ["claude", "opencode", "codex", "openclaw"], case_sensitive=False
    ),
    help="Activation host override.",
)
@click.option("--scope", default=None, help="Activation scope override.")
def remove_cmd(
    slug: str,
    install_dir: str,
    host: str | None,
    scope: str | None,
) -> None:
    result = _remove_path_install(
        slug=slug,
        install_dir=install_dir,
        host=host,
        scope=scope,
    )
    _echo_json({"ok": True, "result": result})


@path_cli.command(
    name="heartbeat-install",
    help="Refresh the active-install heartbeat for an installed path.",
)
@click.option("--slug", required=True, help="Path slug.")
@click.option(
    "--dir",
    "install_dir",
    default=".wayfinder/paths",
    show_default=True,
    help="Base install directory used during install.",
)
@click.option("--status", default="active", show_default=True)
@click.option("--api-url", "api_url", default=None, help="Override Paths API base URL.")
def heartbeat_install_cmd(
    slug: str,
    install_dir: str,
    status: str,
    api_url: str | None,
) -> None:
    base = _canonical_install_root(install_dir)
    state_dir = _state_dir_for_install_root(base)
    lock, lock_path = _load_install_lock(state_dir)
    if not lock_path.exists() and not (state_dir / _LEGACY_LOCKFILE_NAME).exists():
        raise click.ClickException(f"Lockfile not found: {lock_path}")

    paths_map = lock.get("paths") if isinstance(lock, dict) else None
    if not isinstance(paths_map, dict) or slug not in paths_map:
        raise click.ClickException(f"Path not found in lockfile: {slug}")

    entry = paths_map.get(slug) or {}
    installation_id = str(entry.get("installation_id") or "").strip()
    heartbeat_token = str(entry.get("heartbeat_token") or "").strip()
    if not installation_id or not heartbeat_token:
        raise click.ClickException(
            "This install does not have heartbeat credentials. Reinstall the path first."
        )

    client = PathsApiClient(api_base_url=api_url)
    try:
        resp = client.submit_install_heartbeat(
            installation_id=installation_id,
            heartbeat_token=heartbeat_token,
            status=status,
        )
    except PathsApiError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json({"ok": True, "result": resp})


@path_cli.group(name="signal", help="Emit and manage path signals.")
def signal_group() -> None:
    pass


@signal_group.command(name="emit", help="Emit a public signal for a path.")
@click.option("--slug", required=True, help="Path slug.")
@click.option("--version", "path_version", default=None, help="Optional path version.")
@click.option("--title", required=True)
@click.option("--message", default="")
@click.option(
    "--level",
    default="info",
    show_default=True,
    type=click.Choice(["debug", "info", "warning", "error"], case_sensitive=False),
)
@click.option(
    "--metric",
    "metrics",
    multiple=True,
    help="Add a metric key=value (repeatable).",
)
@click.option("--api-url", "api_url", default=None, help="Override Paths API base URL.")
def signal_emit_cmd(
    slug: str,
    path_version: str | None,
    title: str,
    message: str,
    level: str,
    metrics: tuple[str, ...],
    api_url: str | None,
) -> None:
    parsed_metrics: dict[str, float] = {}
    for item in metrics:
        if "=" not in item:
            raise click.ClickException(f"Invalid --metric (expected key=value): {item}")
        k, v = item.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            raise click.ClickException(f"Invalid --metric key: {item}")
        try:
            parsed_metrics[k] = float(v)
        except ValueError as exc:
            raise click.ClickException(
                f"Invalid --metric value (expected number): {item}"
            ) from exc

    client = PathsApiClient(api_base_url=api_url)
    try:
        resp = client.emit_signal(
            slug=slug,
            path_version=path_version,
            title=title,
            message=message,
            level=level.lower(),
            metrics=parsed_metrics,
        )
    except PathsApiError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json({"ok": True, "result": resp})


@path_cli.group(
    name="event",
    help="Emit path runtime events (state_snapshot, decision_snapshot, receipt, heartbeat).",
)
def event_group() -> None:
    pass


@event_group.command(name="emit", help="Emit an event for a path.")
@click.option("--slug", required=True, help="Path slug.")
@click.option(
    "--type", "event_type", required=True, help="Event type (e.g. state_snapshot)."
)
@click.option("--version", "path_version", default=None, help="Optional path version.")
@click.option("--stream-key", default="public", show_default=True)
@click.option(
    "--visibility",
    default="public",
    show_default=True,
    type=click.Choice(["public", "private", "internal"], case_sensitive=False),
)
@click.option(
    "--payload-json",
    default="{}",
    show_default=True,
    help="JSON object payload (inline).",
)
@click.option("--payload-file", default=None, help="Path to a JSON payload file.")
@click.option("--api-url", "api_url", default=None, help="Override Paths API base URL.")
def event_emit_cmd(
    slug: str,
    event_type: str,
    path_version: str | None,
    stream_key: str,
    visibility: str,
    payload_json: str,
    payload_file: str | None,
    api_url: str | None,
) -> None:
    if payload_file:
        try:
            payload_value = json.loads(Path(payload_file).read_text())
        except OSError as exc:
            raise click.ClickException(
                f"Failed to read --payload-file: {payload_file}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise click.ClickException(
                f"Invalid JSON in --payload-file: {payload_file}"
            ) from exc
    else:
        try:
            payload_value = json.loads(payload_json or "{}")
        except json.JSONDecodeError as exc:
            raise click.ClickException("Invalid JSON in --payload-json") from exc

    if payload_value is None:
        payload_value = {}
    if not isinstance(payload_value, dict):
        raise click.ClickException("Payload must be a JSON object")

    client = PathsApiClient(api_base_url=api_url)
    try:
        resp = client.emit_event(
            slug=slug,
            event_type=event_type,
            path_version=path_version,
            payload=payload_value,
            visibility=visibility.lower(),
            stream_key=stream_key.strip() or "public",
        )
    except PathsApiError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json({"ok": True, "result": resp})
