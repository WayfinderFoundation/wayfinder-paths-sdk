from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wayfinder_paths.packs.manifest import (
    PackManifest,
    PackManifestError,
    PackSkillConfig,
    PackSkillRuntimeConfig,
    resolve_skill_runtime,
)


class PackSkillRenderError(Exception):
    pass


_HOSTS = ("claude", "codex", "openclaw", "portable")
_CANONICAL_SKILL_SUBDIRS = ("scripts", "references", "assets")
_EXCLUDED_PACK_DIRS = {
    ".build",
    ".git",
    ".runtime",
    ".venv",
    ".wayfinder",
    "__pycache__",
    "applet",
    "dist",
    "node_modules",
    "skill",
}
_EXCLUDED_PACK_FILES = {"bundle.zip", "source.zip"}


@dataclass(frozen=True)
class PackSkillExportInfo:
    host: str
    skill_name: str
    export_dir: Path
    filename: str
    mode: str
    runtime_manifest: dict[str, Any]
    export_manifest: dict[str, Any]


@dataclass(frozen=True)
class PackSkillRenderReport:
    output_root: Path
    skill_name: str | None
    rendered_hosts: list[str]
    written_files: list[str]
    exports: dict[str, PackSkillExportInfo]


def _component_path_from_manifest(
    manifest: PackManifest,
    component_id: str | None = None,
) -> str:
    component = manifest.resolve_component(component_id)
    return str(component.get("path") or "").strip()


def _build_root(pack_dir: Path, output_root: Path | None = None) -> Path:
    return (output_root or pack_dir / ".build" / "skills").resolve()


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _write_text(path: Path, content: str) -> None:
    _ensure_parent(path)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def _copy_optional_dirs(pack_dir: Path, export_dir: Path) -> list[str]:
    written: list[str] = []
    for name in _CANONICAL_SKILL_SUBDIRS:
        src = pack_dir / "skill" / name
        if not src.exists():
            continue
        dest = export_dir / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        for path in sorted(dest.rglob("*")):
            if path.is_file():
                written.append(path.relative_to(export_dir).as_posix())
    return written


def _copy_runtime_pack(pack_dir: Path, export_dir: Path) -> list[str]:
    pack_export_dir = export_dir / "pack"
    written: list[str] = []
    pack_export_dir.mkdir(parents=True, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(pack_dir):
        rel_dir = Path(dirpath).relative_to(pack_dir)
        dirnames[:] = sorted(
            [
                name
                for name in dirnames
                if name not in _EXCLUDED_PACK_DIRS
                and not (rel_dir == Path(".") and name == "dist")
            ]
        )
        for filename in sorted(filenames):
            if filename in _EXCLUDED_PACK_FILES:
                continue
            src = Path(dirpath) / filename
            rel_path = src.relative_to(pack_dir)
            if any(part in _EXCLUDED_PACK_DIRS for part in rel_path.parts):
                continue
            dest = pack_export_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            written.append(dest.relative_to(export_dir).as_posix())
    return written


def _quote_yaml(value: str) -> str:
    return json.dumps(value)


def _yaml_list(key: str, values: list[str], *, indent: int = 0) -> str:
    prefix = " " * indent
    if not values:
        return ""
    lines = [f"{prefix}{key}:"]
    lines.extend([f"{prefix}  - {value}" for value in values])
    return "\n".join(lines)


def _wrap_frontmatter(lines: list[str], body: str) -> str:
    return (
        "---\n"
        + "\n".join([line for line in lines if line])
        + "\n---\n\n"
        + body.strip()
        + "\n"
    )


def _render_claude_skill(
    manifest: PackManifest, skill: PackSkillConfig, body: str
) -> str:
    lines = [
        f"name: {skill.name}",
        f"description: {_quote_yaml(skill.description)}",
    ]
    if skill.claude and skill.claude.disable_model_invocation is not None:
        lines.append(
            f"disable-model-invocation: {str(skill.claude.disable_model_invocation).lower()}"
        )
    allowed = skill.claude.allowed_tools if skill.claude else []
    if allowed:
        lines.append(_yaml_list("allowed-tools", allowed))
    if manifest.tags:
        lines.append("metadata:")
        lines.append(_yaml_list("tags", manifest.tags, indent=2))
    return _wrap_frontmatter(lines, body)


def _render_codex_skill(
    manifest: PackManifest, skill: PackSkillConfig, body: str
) -> str:
    lines = [
        f"name: {skill.name}",
        f"description: {_quote_yaml(skill.description)}",
    ]
    if manifest.tags:
        lines.append("metadata:")
        lines.append(_yaml_list("tags", manifest.tags, indent=2))
    return _wrap_frontmatter(lines, body)


def _render_codex_policy(skill: PackSkillConfig) -> str:
    allow_implicit = False
    if skill.codex and skill.codex.allow_implicit_invocation is not None:
        allow_implicit = skill.codex.allow_implicit_invocation
    return "\n".join(
        [
            f"allow_implicit_invocation: {str(allow_implicit).lower()}",
            "",
        ]
    )


def _render_openclaw_skill(
    manifest: PackManifest,
    skill: PackSkillConfig,
    body: str,
    runtime: PackSkillRuntimeConfig,
) -> str:
    metadata: dict[str, object] = {"tags": manifest.tags}
    if skill.openclaw:
        if skill.openclaw.user_invocable is not None:
            metadata["user-invocable"] = skill.openclaw.user_invocable
        if skill.openclaw.disable_model_invocation is not None:
            metadata["disable-model-invocation"] = (
                skill.openclaw.disable_model_invocation
            )
        if skill.openclaw.requires:
            metadata["requires"] = skill.openclaw.requires
        if skill.openclaw.install:
            metadata["install"] = skill.openclaw.install
    if runtime.require_api_key and runtime.api_key_env:
        metadata["primaryEnv"] = runtime.api_key_env
    lines = [
        f"name: {skill.name}",
        f"description: {_quote_yaml(skill.description)}",
        f"metadata: {json.dumps(metadata, separators=(',', ':'), sort_keys=True)}",
    ]
    return _wrap_frontmatter(lines, body)


def _render_portable_skill(
    manifest: PackManifest,
    skill: PackSkillConfig,
    body: str,
    runtime: PackSkillRuntimeConfig,
) -> str:
    metadata: dict[str, object] = {
        "mode": runtime.mode,
        "package": runtime.package,
        "python": runtime.python,
        "tags": manifest.tags,
        "version": runtime.version,
    }
    lines = [
        f"name: {skill.name}",
        f"description: {_quote_yaml(skill.description)}",
        f"metadata: {json.dumps(metadata, separators=(',', ':'), sort_keys=True)}",
    ]
    return _wrap_frontmatter(lines, body)


def _render_bootstrap_script(runtime_manifest: dict[str, Any]) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env python3",
            "from __future__ import annotations",
            "",
            "import argparse",
            "import json",
            "import os",
            "import shutil",
            "import subprocess",
            "import sys",
            "from importlib import metadata as importlib_metadata",
            "from pathlib import Path",
            "",
            "",
            "SKILL_ROOT = Path(__file__).resolve().parents[1]",
            "RUNTIME_MANIFEST_PATH = SKILL_ROOT / 'runtime' / 'manifest.json'",
            "DEFAULT_RUNTIME_CONFIG_PATH = SKILL_ROOT / '.runtime' / 'config.json'",
            "",
            "",
            "def _load_manifest() -> dict[str, object]:",
            "    return json.loads(RUNTIME_MANIFEST_PATH.read_text(encoding='utf-8'))",
            "",
            "",
            "def _normalized_passthrough(args: list[str]) -> list[str]:",
            "    if args and args[0] == '--':",
            "        return args[1:]",
            "    return args",
            "",
            "",
            "def _runtime_env(manifest: dict[str, object]) -> dict[str, str]:",
            "    env = os.environ.copy()",
            "    cfg_env = str(manifest.get('config_path_env') or 'WAYFINDER_CONFIG_PATH')",
            "    if not env.get(cfg_env) and DEFAULT_RUNTIME_CONFIG_PATH.exists():",
            "        env[cfg_env] = str(DEFAULT_RUNTIME_CONFIG_PATH)",
            "    return env",
            "",
            "",
            "def _config_has_api_key(path_value: str | None) -> bool:",
            "    if not path_value:",
            "        return False",
            "    try:",
            "        payload = json.loads(Path(path_value).expanduser().read_text(encoding='utf-8'))",
            "    except Exception:",
            "        return False",
            "    if not isinstance(payload, dict):",
            "        return False",
            "    system = payload.get('system')",
            "    if not isinstance(system, dict):",
            "        return False",
            "    return bool(str(system.get('api_key') or '').strip())",
            "",
            "",
            "def _ensure_api_key(manifest: dict[str, object], env: dict[str, str]) -> None:",
            "    if not bool(manifest.get('require_api_key')):",
            "        return",
            "    api_env = str(manifest.get('api_key_env') or 'WAYFINDER_API_KEY')",
            "    cfg_env = str(manifest.get('config_path_env') or 'WAYFINDER_CONFIG_PATH')",
            "    if env.get(api_env):",
            "        return",
            "    if _config_has_api_key(env.get(cfg_env)):",
            "        return",
            "    raise SystemExit(",
            "        f'Missing API key. Set {api_env} or configure {cfg_env} before running this skill.'",
            "    )",
            "",
            "",
            "def _call_cli(command: list[str], env: dict[str, str]) -> int:",
            "    return subprocess.call(command, env=env)",
            "",
            "",
            "def _current_runtime_matches(manifest: dict[str, object]) -> bool:",
            "    package = str(manifest.get('package') or 'wayfinder-paths')",
            "    version = str(manifest.get('version') or '').strip()",
            "    if not version:",
            "        return False",
            "    try:",
            "        installed = importlib_metadata.version(package)",
            "    except importlib_metadata.PackageNotFoundError:",
            "        return False",
            "    return installed == version",
            "",
            "",
            "def _wayfinder_binary_matches(manifest: dict[str, object]) -> str | None:",
            "    binary = shutil.which('wayfinder')",
            "    version = str(manifest.get('version') or '').strip()",
            "    if not binary or not version:",
            "        return None",
            "    try:",
            "        proc = subprocess.run(",
            "            [binary, 'pack', 'version'],",
            "            check=True,",
            "            capture_output=True,",
            "            text=True,",
            "        )",
            "    except Exception:",
            "        return None",
            "    resolved = proc.stdout.strip()",
            "    if resolved == version:",
            "        return binary",
            "    return None",
            "",
            "",
            "def _wayfinder_exec_args(manifest: dict[str, object], args: list[str]) -> list[str]:",
            "    pack_dir = SKILL_ROOT / 'pack'",
            "    component = str(manifest.get('component') or 'main')",
            "    return [",
            "        'pack',",
            "        'exec',",
            "        '--pack-dir',",
            "        str(pack_dir),",
            "        '--component',",
            "        component,",
            "        '--',",
            "        *_normalized_passthrough(args),",
            "    ]",
            "",
            "",
            "def _run_with_existing_runtime(manifest: dict[str, object], env: dict[str, str]) -> int | None:",
            "    if not bool(manifest.get('prefer_existing_runtime', True)):",
            "        return None",
            "    exec_args = _wayfinder_exec_args(manifest, [])",
            "    if _current_runtime_matches(manifest):",
            "        return None",
            "    binary = _wayfinder_binary_matches(manifest)",
            "    if binary:",
            "        return _call_cli([binary, *_wayfinder_exec_args(manifest, sys.argv[2:])], env)",
            "    return None",
            "",
            "",
            "def _bootstrap_with_uv(manifest: dict[str, object], env: dict[str, str], args: list[str]) -> int:",
            "    binary = shutil.which('uv')",
            "    if not binary:",
            "        raise FileNotFoundError('uv not found')",
            "    package = str(manifest.get('package') or 'wayfinder-paths')",
            "    version = str(manifest.get('version') or '').strip()",
            "    spec = f'{package}=={version}' if version else package",
            "    cmd = [binary, 'run', '--with', spec, 'wayfinder', *_wayfinder_exec_args(manifest, args)]",
            "    return _call_cli(cmd, env)",
            "",
            "",
            "def _bootstrap_with_pipx(manifest: dict[str, object], env: dict[str, str], args: list[str]) -> int:",
            "    binary = shutil.which('pipx')",
            "    if not binary:",
            "        raise FileNotFoundError('pipx not found')",
            "    package = str(manifest.get('package') or 'wayfinder-paths')",
            "    version = str(manifest.get('version') or '').strip()",
            "    spec = f'{package}=={version}' if version else package",
            "    cmd = [binary, 'run', '--spec', spec, 'wayfinder', *_wayfinder_exec_args(manifest, args)]",
            "    return _call_cli(cmd, env)",
            "",
            "",
            "def _venv_python(venv_dir: Path) -> Path:",
            "    if os.name == 'nt':",
            "        return venv_dir / 'Scripts' / 'python.exe'",
            "    return venv_dir / 'bin' / 'python'",
            "",
            "",
            "def _venv_matches(python_bin: Path, manifest: dict[str, object]) -> bool:",
            "    package = str(manifest.get('package') or 'wayfinder-paths')",
            "    version = str(manifest.get('version') or '').strip()",
            "    if not python_bin.exists() or not version:",
            "        return False",
            "    try:",
            "        proc = subprocess.run(",
            "            [",
            "                str(python_bin),",
            "                '-c',",
            "                (",
            "                    'from importlib import metadata as m; '",
            "                    f'print(m.version({package!r}))'",
            "                ),",
            "            ],",
            "            check=True,",
            "            capture_output=True,",
            "            text=True,",
            "        )",
            "    except Exception:",
            "        return False",
            "    return proc.stdout.strip() == version",
            "",
            "",
            "def _bootstrap_with_local_venv(manifest: dict[str, object], env: dict[str, str], args: list[str]) -> int:",
            "    runtime_dir = SKILL_ROOT / '.runtime'",
            "    venv_dir = runtime_dir / 'venv'",
            "    python_bin = _venv_python(venv_dir)",
            "    if not _venv_matches(python_bin, manifest):",
            "        runtime_dir.mkdir(parents=True, exist_ok=True)",
            "        subprocess.check_call([sys.executable, '-m', 'venv', str(venv_dir)])",
            "        python_bin = _venv_python(venv_dir)",
            "        subprocess.check_call([str(python_bin), '-m', 'pip', 'install', '--upgrade', 'pip'])",
            "        package = str(manifest.get('package') or 'wayfinder-paths')",
            "        version = str(manifest.get('version') or '').strip()",
            "        spec = f'{package}=={version}' if version else package",
            "        subprocess.check_call([str(python_bin), '-m', 'pip', 'install', spec])",
            "    cmd = [",
            "        str(python_bin),",
            "        '-m',",
            "        'wayfinder_paths.mcp.cli',",
            "        *_wayfinder_exec_args(manifest, args),",
            "    ]",
            "    return _call_cli(cmd, env)",
            "",
            "",
            "def run(component: str | None, args: list[str]) -> int:",
            "    manifest = _load_manifest()",
            "    if component:",
            "        manifest['component'] = component",
            "    env = _runtime_env(manifest)",
            "    _ensure_api_key(manifest, env)",
            "",
            "    if _current_runtime_matches(manifest):",
            "        cmd = [",
            "            sys.executable,",
            "            '-m',",
            "            'wayfinder_paths.mcp.cli',",
            "            *_wayfinder_exec_args(manifest, args),",
            "        ]",
            "        return _call_cli(cmd, env)",
            "",
            "    binary = _wayfinder_binary_matches(manifest)",
            "    if binary:",
            "        return _call_cli([binary, *_wayfinder_exec_args(manifest, args)], env)",
            "",
            "    bootstrap_order = [str(manifest.get('bootstrap') or 'uv')]",
            "    fallback = str(manifest.get('fallback_bootstrap') or 'pipx')",
            "    if fallback and fallback not in bootstrap_order:",
            "        bootstrap_order.append(fallback)",
            "    if 'venv' not in bootstrap_order:",
            "        bootstrap_order.append('venv')",
            "",
            "    errors: list[str] = []",
            "    for method in bootstrap_order:",
            "        try:",
            "            if method == 'uv':",
            "                return _bootstrap_with_uv(manifest, env, args)",
            "            if method == 'pipx':",
            "                return _bootstrap_with_pipx(manifest, env, args)",
            "            if method == 'venv':",
            "                return _bootstrap_with_local_venv(manifest, env, args)",
            "        except FileNotFoundError as exc:",
            "            errors.append(str(exc))",
            "        except subprocess.CalledProcessError as exc:",
            "            errors.append(f'{method} failed with exit code {exc.returncode}')",
            "",
            "    raise SystemExit('Failed to bootstrap runtime: ' + '; '.join(errors))",
            "",
            "",
            "def configure(api_key: str, config_path: str | None) -> int:",
            "    path = Path(config_path).expanduser() if config_path else DEFAULT_RUNTIME_CONFIG_PATH",
            "    payload: dict[str, object] = {}",
            "    if path.exists():",
            "        try:",
            "            loaded = json.loads(path.read_text(encoding='utf-8'))",
            "            if isinstance(loaded, dict):",
            "                payload = loaded",
            "        except Exception:",
            "            payload = {}",
            "    system = payload.get('system') if isinstance(payload.get('system'), dict) else {}",
            "    system['api_key'] = api_key",
            "    payload['system'] = system",
            "    path.parent.mkdir(parents=True, exist_ok=True)",
            "    path.write_text(json.dumps(payload, indent=2) + '\\n', encoding='utf-8')",
            "    print(json.dumps({'ok': True, 'result': {'config_path': str(path)}}))",
            "    return 0",
            "",
            "",
            "def main() -> int:",
            "    argv = sys.argv[1:]",
            "    command = 'run'",
            "    if argv and argv[0] in {'run', 'configure'}:",
            "        command = argv.pop(0)",
            "",
            "    if command == 'configure':",
            "        parser = argparse.ArgumentParser(description='Write a local Wayfinder config for this skill.')",
            "        parser.add_argument('--api-key', required=True)",
            "        parser.add_argument('--config-path', default=None)",
            "        parsed = parser.parse_args(argv)",
            "        return configure(parsed.api_key, parsed.config_path)",
            "",
            "    parser = argparse.ArgumentParser(description='Run the exported Wayfinder skill.')",
            "    parser.add_argument('--component', default=None)",
            "    parser.add_argument('args', nargs=argparse.REMAINDER)",
            "    parsed = parser.parse_args(argv)",
            "    return run(parsed.component, parsed.args)",
            "",
            "",
            "if __name__ == '__main__':",
            "    raise SystemExit(main())",
            "",
        ]
    )


def _render_run_script() -> str:
    return "\n".join(
        [
            "#!/usr/bin/env python3",
            "from __future__ import annotations",
            "",
            "import subprocess",
            "import sys",
            "from pathlib import Path",
            "",
            "",
            "def main() -> int:",
            "    bootstrap = Path(__file__).with_name('wf_bootstrap.py')",
            "    return subprocess.call([sys.executable, str(bootstrap), 'run', *sys.argv[1:]])",
            "",
            "",
            "if __name__ == '__main__':",
            "    raise SystemExit(main())",
            "",
        ]
    )


def _provided_skill_path(pack_dir: Path) -> Path:
    return pack_dir / "skill" / "SKILL.md"


def _generated_skill_path(pack_dir: Path, skill: PackSkillConfig) -> Path:
    if not skill.instructions_path:
        raise PackSkillRenderError("Generated skill is missing instructions_path")
    return pack_dir / skill.instructions_path


def _source_markdown(pack_dir: Path, skill: PackSkillConfig) -> str:
    source_path = (
        _generated_skill_path(pack_dir, skill)
        if skill.source == "generated"
        else _provided_skill_path(pack_dir)
    )
    if not source_path.exists():
        raise PackSkillRenderError(f"Skill source not found: {source_path}")
    return source_path.read_text(encoding="utf-8").strip() + "\n"


def _export_dir(output_root: Path, host: str, skill_name: str) -> Path:
    return output_root / host / skill_name


def _reset_export_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _runtime_manifest(
    manifest: PackManifest,
    skill: PackSkillConfig,
    runtime: PackSkillRuntimeConfig,
) -> dict[str, Any]:
    return {
        "slug": manifest.slug,
        "pack_version": manifest.version,
        "skill_name": skill.name,
        "mode": runtime.mode,
        "package": runtime.package,
        "version": runtime.version,
        "python": runtime.python,
        "component": runtime.component,
        "component_path": _component_path_from_manifest(manifest, runtime.component),
        "bootstrap": runtime.bootstrap,
        "fallback_bootstrap": runtime.fallback_bootstrap,
        "prefer_existing_runtime": runtime.prefer_existing_runtime,
        "require_api_key": runtime.require_api_key,
        "api_key_env": runtime.api_key_env,
        "config_path_env": runtime.config_path_env,
    }


def _export_manifest(
    manifest: PackManifest,
    skill: PackSkillConfig,
    host: str,
    runtime_manifest: dict[str, Any],
) -> dict[str, Any]:
    mode = str(runtime_manifest.get("mode") or "thin")
    return {
        "host": host,
        "slug": manifest.slug,
        "version": manifest.version,
        "skill_name": skill.name,
        "mode": mode,
        "filename": f"skill-{host}-{mode}.zip",
    }


def _write_host_artifacts(
    *,
    pack_dir: Path,
    manifest: PackManifest,
    skill: PackSkillConfig,
    runtime: PackSkillRuntimeConfig,
    host: str,
    output_root: Path,
    body: str,
) -> tuple[list[str], PackSkillExportInfo]:
    export_dir = _export_dir(output_root, host, skill.name)
    _reset_export_dir(export_dir)

    written: list[str] = []
    written.extend(_copy_optional_dirs(pack_dir, export_dir))
    written.extend(_copy_runtime_pack(pack_dir, export_dir))

    if skill.source == "provided":
        skill_md = body
    elif host == "claude":
        skill_md = _render_claude_skill(manifest, skill, body)
    elif host == "codex":
        skill_md = _render_codex_skill(manifest, skill, body)
    elif host == "openclaw":
        skill_md = _render_openclaw_skill(manifest, skill, body, runtime)
    else:
        skill_md = _render_portable_skill(manifest, skill, body, runtime)

    skill_md_path = export_dir / "SKILL.md"
    _write_text(skill_md_path, skill_md)
    written.append(skill_md_path.relative_to(output_root).as_posix())

    runtime_manifest = _runtime_manifest(manifest, skill, runtime)
    export_manifest = _export_manifest(manifest, skill, host, runtime_manifest)

    runtime_manifest_path = export_dir / "runtime" / "manifest.json"
    _write_text(
        runtime_manifest_path, json.dumps(runtime_manifest, indent=2, sort_keys=True)
    )
    written.append(runtime_manifest_path.relative_to(output_root).as_posix())

    export_manifest_path = export_dir / "runtime" / "export.json"
    _write_text(
        export_manifest_path, json.dumps(export_manifest, indent=2, sort_keys=True)
    )
    written.append(export_manifest_path.relative_to(output_root).as_posix())

    bootstrap_path = export_dir / "scripts" / "wf_bootstrap.py"
    _write_text(bootstrap_path, _render_bootstrap_script(runtime_manifest))
    written.append(bootstrap_path.relative_to(output_root).as_posix())

    run_path = export_dir / "scripts" / "wf_run.py"
    _write_text(run_path, _render_run_script())
    written.append(run_path.relative_to(output_root).as_posix())

    if host == "codex":
        policy_path = export_dir / "agents" / "openai.yaml"
        _write_text(policy_path, _render_codex_policy(skill))
        written.append(policy_path.relative_to(output_root).as_posix())

    info = PackSkillExportInfo(
        host=host,
        skill_name=skill.name,
        export_dir=export_dir,
        filename=export_manifest["filename"],
        mode=str(export_manifest["mode"]),
        runtime_manifest=runtime_manifest,
        export_manifest=export_manifest,
    )
    return written, info


def render_skill_exports(
    *,
    pack_dir: Path,
    output_root: Path | None = None,
    hosts: list[str] | tuple[str, ...] | None = None,
) -> PackSkillRenderReport:
    pack_dir = pack_dir.resolve()
    manifest_path = pack_dir / "wfpack.yaml"
    if not manifest_path.exists():
        raise PackSkillRenderError(f"Missing wfpack.yaml in {pack_dir}")

    try:
        manifest = PackManifest.load(manifest_path)
    except PackManifestError as exc:
        raise PackSkillRenderError(str(exc)) from exc

    if not manifest.skill or not manifest.skill.enabled:
        return PackSkillRenderReport(
            output_root=_build_root(pack_dir, output_root),
            skill_name=None,
            rendered_hosts=[],
            written_files=[],
            exports={},
        )

    selected_hosts = list(hosts) if hosts is not None else list(_HOSTS)
    invalid_hosts = [host for host in selected_hosts if host not in _HOSTS]
    if invalid_hosts:
        raise PackSkillRenderError(
            f"Unsupported render host(s): {', '.join(sorted(invalid_hosts))}"
        )

    output_root_resolved = _build_root(pack_dir, output_root)
    body = _source_markdown(pack_dir, manifest.skill)
    runtime = resolve_skill_runtime(manifest)
    if runtime.mode != "thin":
        raise PackSkillRenderError(
            "Only skill.runtime.mode=thin is supported for host skill exports"
        )

    rendered_hosts: list[str] = []
    written_files: list[str] = []
    exports: dict[str, PackSkillExportInfo] = {}
    for host in selected_hosts:
        rendered_hosts.append(host)
        host_written, export_info = _write_host_artifacts(
            pack_dir=pack_dir,
            manifest=manifest,
            skill=manifest.skill,
            runtime=runtime,
            host=host,
            output_root=output_root_resolved,
            body=body,
        )
        written_files.extend(host_written)
        exports[host] = export_info

    return PackSkillRenderReport(
        output_root=output_root_resolved,
        skill_name=manifest.skill.name,
        rendered_hosts=rendered_hosts,
        written_files=sorted(written_files),
        exports=exports,
    )
