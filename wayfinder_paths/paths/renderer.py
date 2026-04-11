from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wayfinder_paths.paths.manifest import (
    PathAgentConfig,
    PathManifest,
    PathManifestError,
    PathSkillConfig,
    PathSkillRuntimeConfig,
    resolve_skill_runtime,
)
from wayfinder_paths.paths.pipeline import get_pipeline_archetype


class PathSkillRenderError(Exception):
    pass


_HOSTS = ("claude", "opencode", "codex", "openclaw", "portable")
_CANONICAL_SKILL_SUBDIRS = ("scripts", "references", "assets")
_EXCLUDED_PATH_DIRS = {
    ".build",
    ".git",
    ".runtime",
    ".venv",
    ".wf-artifacts",
    ".wf-state",
    ".wayfinder",
    "__pycache__",
    "applet",
    "dist",
    "node_modules",
    "skill",
    "tests",
}
_EXCLUDED_PATH_FILES = {"bundle.zip", "source.zip"}


@dataclass(frozen=True)
class PathSkillExportInfo:
    host: str
    skill_name: str
    export_dir: Path
    filename: str
    mode: str
    runtime_manifest: dict[str, Any]
    export_manifest: dict[str, Any]


@dataclass(frozen=True)
class PathSkillRenderReport:
    output_root: Path
    skill_name: str | None
    rendered_hosts: list[str]
    written_files: list[str]
    exports: dict[str, PathSkillExportInfo]


def _component_path_from_manifest(
    manifest: PathManifest,
    component_id: str | None = None,
) -> str:
    component = manifest.resolve_component(component_id)
    return str(component.get("path") or "").strip()


def _build_root(path_dir: Path, output_root: Path | None = None) -> Path:
    return (output_root or path_dir / ".build" / "skills").resolve()


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _write_text(path: Path, content: str) -> None:
    _ensure_parent(path)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def _copy_optional_dirs(path_dir: Path, export_dir: Path) -> list[str]:
    written: list[str] = []
    for name in _CANONICAL_SKILL_SUBDIRS:
        src = path_dir / "skill" / name
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


def _copy_runtime_path(path_dir: Path, export_dir: Path) -> list[str]:
    path_export_dir = export_dir / "path"
    written: list[str] = []
    path_export_dir.mkdir(parents=True, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(path_dir):
        rel_dir = Path(dirpath).relative_to(path_dir)
        dirnames[:] = sorted(
            [
                name
                for name in dirnames
                if name not in _EXCLUDED_PATH_DIRS
                and not (rel_dir == Path(".") and name == "dist")
            ]
        )
        for filename in sorted(filenames):
            if filename in _EXCLUDED_PATH_FILES:
                continue
            src = Path(dirpath) / filename
            rel_path = src.relative_to(path_dir)
            if any(part in _EXCLUDED_PATH_DIRS for part in rel_path.parts):
                continue
            dest = path_export_dir / rel_path
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
    manifest: PathManifest, skill: PathSkillConfig, body: str
) -> str:
    lines = [
        f"name: {skill.name}",
        f"description: {_quote_yaml(skill.description)}",
    ]
    if skill.claude and skill.claude.disable_model_invocation is not None:
        lines.append(
            f"disable-model-invocation: {str(skill.claude.disable_model_invocation).lower()}"
        )
    allowed = list(skill.claude.allowed_tools) if skill.claude else []
    if manifest.pipeline and manifest.agents:
        for agent in manifest.agents:
            allowed.append(f"Agent({_claude_agent_name(skill.name, agent.agent_id)})")
        for tool in ("Read", "Glob", "Grep", "Bash"):
            if tool not in allowed:
                allowed.append(tool)
    if allowed:
        lines.append(_yaml_list("allowed-tools", allowed))
    if manifest.tags:
        lines.append("metadata:")
        lines.append(_yaml_list("tags", manifest.tags, indent=2))
    return _wrap_frontmatter(lines, body)


def _render_opencode_skill(
    manifest: PathManifest,
    skill: PathSkillConfig,
    body: str,
) -> str:
    metadata: dict[str, object] = {"category": "wayfinder", "kind": "path-skill"}
    if manifest.pipeline and manifest.pipeline.archetype:
        metadata["kind"] = "strategy-pipeline"
        metadata["archetype"] = manifest.pipeline.archetype
    lines = [
        f"name: {skill.name}",
        f"description: {_quote_yaml(skill.description)}",
        "compatibility: opencode",
        f"metadata: {json.dumps(metadata, separators=(',', ':'), sort_keys=True)}",
    ]
    return _wrap_frontmatter(lines, body)


def _render_codex_skill(
    manifest: PathManifest, skill: PathSkillConfig, body: str
) -> str:
    lines = [
        f"name: {skill.name}",
        f"description: {_quote_yaml(skill.description)}",
    ]
    if manifest.tags:
        lines.append("metadata:")
        lines.append(_yaml_list("tags", manifest.tags, indent=2))
    return _wrap_frontmatter(lines, body)


def _claude_agent_name(skill_name: str, agent_id: str) -> str:
    return f"{skill_name}-{agent_id}"


def _opencode_agent_name(skill_name: str, agent_id: str) -> str:
    return f"{skill_name}-{agent_id}"


def _canonical_agent_body(path_dir: Path, agent: PathAgentConfig) -> str:
    agent_path = path_dir / "skill" / "agents" / f"{agent.agent_id}.md"
    if not agent_path.exists():
        raise PathSkillRenderError(f"Agent source not found: {agent_path}")
    return agent_path.read_text(encoding="utf-8").strip() + "\n"


def _claude_agent_tools(agent: PathAgentConfig) -> str:
    mapping = {
        "read": "Read",
        "glob": "Glob",
        "grep": "Grep",
        "bash": "Bash",
        "webfetch": "WebFetch",
        "websearch": "WebSearch",
        "edit": "Edit",
        "write": "Write",
    }
    return ", ".join(mapping.get(tool, tool) for tool in agent.tools) or "Read"


def _render_claude_agent(
    *,
    path_dir: Path,
    skill: PathSkillConfig,
    agent: PathAgentConfig,
) -> str:
    body = _canonical_agent_body(path_dir, agent)
    lines = [
        f"name: {_claude_agent_name(skill.name, agent.agent_id)}",
        f"description: {_quote_yaml(agent.description)}",
        f"tools: {_claude_agent_tools(agent)}",
        "model: sonnet",
    ]
    return _wrap_frontmatter(lines, body)


def _render_claude_rules(manifest: PathManifest) -> str:
    pipeline = manifest.pipeline
    archetype = pipeline.archetype if pipeline and pipeline.archetype else "pipeline"
    lines = [
        f"# {manifest.name} Claude Rules",
        "",
        f"This generated section orchestrates the `{archetype}` workflow.",
        "",
        "Rules:",
        "- The main-thread skill owns orchestration.",
        "- Worker agents are leaf-only and write one artifact each.",
        f"- Runtime artifacts live under `{pipeline.artifacts_dir if pipeline else '.wf-artifacts'}`.",
        "- Null-state evaluation is mandatory before any job is armed.",
        "- If risk checks fail, downgrade to draft or null-state.",
    ]
    return "\n".join(lines) + "\n"


def _render_claude_settings(manifest: PathManifest, skill: PathSkillConfig) -> str:
    matchers = "|".join(
        _claude_agent_name(skill.name, agent.agent_id) for agent in manifest.agents
    )
    inject_cmd = f'python "$CLAUDE_PROJECT_DIR/.claude/skills/{skill.name}/scripts/inject_run_context.py"'
    validate_cmd = f'python "$CLAUDE_PROJECT_DIR/.claude/skills/{skill.name}/scripts/validate_hook.py"'
    hooks = {
        "hooks": {
            "SubagentStart": [
                {
                    "matcher": matchers or skill.name,
                    "hooks": [
                        {
                            "type": "command",
                            "command": inject_cmd if manifest.pipeline else "true",
                        }
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "Write|Edit",
                    "hooks": [
                        {
                            "type": "command",
                            "command": validate_cmd,
                            "async": True,
                            "timeout": 120,
                        }
                    ],
                }
            ],
        }
    }
    return json.dumps(hooks, indent=2) + "\n"


def _render_opencode_agents_md(manifest: PathManifest, skill: PathSkillConfig) -> str:
    command_name = (
        manifest.pipeline.entry_command
        if manifest.pipeline and manifest.pipeline.entry_command
        else skill.name
    )
    lines = [
        f"# {manifest.name} OpenCode Rules",
        "",
        "## Strategy pipeline rules",
        "- The primary orchestrator owns the workflow.",
        "- Hidden subagents are leaf workers.",
        "- Every worker writes exactly one artifact under `.wf-artifacts/$RUN_ID/`.",
        "- Always evaluate a null state.",
        "- Never place live trades before the risk verifier passes.",
        f"- The primary command entrypoint is `{command_name}`.",
    ]
    return "\n".join(lines) + "\n"


def _opencode_permission_block(agent: PathAgentConfig) -> list[str]:
    lines = ["permission:"]
    tool_values = set(agent.tools)
    for name in ("read", "glob", "grep", "webfetch", "websearch"):
        if name in tool_values:
            lines.append(f"  {name}: allow")
    if "edit" in tool_values or "write" in tool_values:
        lines.append("  edit: allow")
    else:
        lines.append("  edit: deny")
    if "bash" in tool_values:
        lines.append("  bash:")
        lines.append('    "*": ask')
        lines.append('    "python *": allow')
    return lines


def _render_opencode_orchestrator(
    manifest: PathManifest, skill: PathSkillConfig
) -> str:
    task_ids = [
        _opencode_agent_name(skill.name, agent.agent_id) for agent in manifest.agents
    ]
    lines = [
        "---",
        f"description: Orchestrates the {skill.name} pipeline using hidden worker subagents.",
        "mode: primary",
        "model: anthropic/claude-sonnet-4-20250514",
        "temperature: 0.1",
        "steps: 20",
        "permission:",
        "  skill:",
        f'    "{skill.name}": allow',
        "  task:",
        '    "*": deny',
    ]
    for task_id in task_ids:
        lines.append(f'    "{task_id}": allow')
    lines.extend(
        [
            "  bash:",
            '    "*": ask',
            '    "python *": allow',
            '    "wayfinder *": allow',
            "  webfetch: allow",
            "  websearch: allow",
            "---",
            "",
            "You are the sole orchestrator.",
            f"Load the `{skill.name}` skill at task start.",
            "Use hidden subagents for analysis fan-out.",
            "Do not perform worker analysis yourself.",
            "Require a null-state comparison before compile_job.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_opencode_worker(
    *,
    path_dir: Path,
    skill: PathSkillConfig,
    agent: PathAgentConfig,
) -> str:
    body = _canonical_agent_body(path_dir, agent)
    lines = [
        "---",
        f"description: {agent.description}",
        "mode: subagent",
        "hidden: true",
        "model: anthropic/claude-sonnet-4-20250514",
        "temperature: 0.1",
        *_opencode_permission_block(agent),
        "---",
        "",
        body.strip(),
        "",
    ]
    return "\n".join(lines)


def _render_opencode_command(manifest: PathManifest, skill: PathSkillConfig) -> str:
    agent_name = f"{skill.name}-orchestrator"
    lines = [
        "---",
        f"description: Run the {skill.name} workflow",
        f"agent: {agent_name}",
        "model: anthropic/claude-sonnet-4-20250514",
        "---",
        "",
        "Run the pipeline for this request:",
        "",
        "$ARGUMENTS",
        "",
    ]
    for slot in manifest.inputs:
        lines.append(f"@{slot.path}")
    lines.append("")
    return "\n".join(lines)


def _render_opencode_plugin_state() -> str:
    return "\n".join(
        [
            'import type { Plugin } from "@opencode-ai/plugin"',
            "",
            "export const PipelineState: Plugin = async () => ({",
            '  "experimental.session.compacting": async (_input, output) => {',
            '    output.context.push("## Pipeline state\\n- Generated by Wayfinder path export")',
            "  },",
            "})",
            "",
        ]
    )


def _render_opencode_plugin_guard() -> str:
    return "\n".join(
        [
            'import type { Plugin } from "@opencode-ai/plugin"',
            "",
            "export const TradeGuard: Plugin = async () => ({",
            '  "tool.execute.before": async (_input, _output) => {',
            "    return",
            "  },",
            "})",
            "",
        ]
    )


def _render_opencode_tool(name: str, description: str) -> str:
    return "\n".join(
        [
            'import { tool } from "@opencode-ai/plugin"',
            "",
            "export default tool({",
            f"  description: {json.dumps(description)},",
            "  args: {},",
            "  async execute() {",
            f"    return {{ ok: true, tool: {json.dumps(name)} }}",
            "  },",
            "})",
            "",
        ]
    )


def _render_opencode_config() -> str:
    payload = {
        "$schema": "https://opencode.ai/config.json",
        "instructions": ["AGENTS.md"],
    }
    return json.dumps(payload, indent=2) + "\n"


def _render_codex_policy(skill: PathSkillConfig) -> str:
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
    manifest: PathManifest,
    skill: PathSkillConfig,
    body: str,
    runtime: PathSkillRuntimeConfig,
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
    manifest: PathManifest,
    skill: PathSkillConfig,
    body: str,
    runtime: PathSkillRuntimeConfig,
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
            "            [binary, 'path', 'version'],",
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
            "    path_dir = SKILL_ROOT / 'path'",
            "    component = str(manifest.get('component') or 'main')",
            "    return [",
            "        'path',",
            "        'exec',",
            "        '--path-dir',",
            "        str(path_dir),",
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


def _provided_skill_path(path_dir: Path) -> Path:
    return path_dir / "skill" / "SKILL.md"


def _generated_skill_path(path_dir: Path, skill: PathSkillConfig) -> Path:
    if not skill.instructions_path:
        raise PathSkillRenderError("Generated skill is missing instructions_path")
    return path_dir / skill.instructions_path


def _source_markdown(path_dir: Path, skill: PathSkillConfig) -> str:
    source_path = (
        _generated_skill_path(path_dir, skill)
        if skill.source == "generated"
        else _provided_skill_path(path_dir)
    )
    if not source_path.exists():
        raise PathSkillRenderError(f"Skill source not found: {source_path}")
    return source_path.read_text(encoding="utf-8").strip() + "\n"


def _export_dir(output_root: Path, host: str, skill_name: str) -> Path:
    return output_root / host / skill_name


def _reset_export_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _runtime_manifest(
    manifest: PathManifest,
    skill: PathSkillConfig,
    runtime: PathSkillRuntimeConfig,
) -> dict[str, Any]:
    return {
        "slug": manifest.slug,
        "path_version": manifest.version,
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
    manifest: PathManifest,
    skill: PathSkillConfig,
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


def _copy_payload_to_install_tree(
    *,
    export_dir: Path,
    install_root: Path,
    output_root: Path,
) -> list[str]:
    written: list[str] = []
    install_root.mkdir(parents=True, exist_ok=True)
    for name in (
        "SKILL.md",
        "runtime",
        "scripts",
        "references",
        "assets",
        "path",
        "agents",
    ):
        src = export_dir / name
        if not src.exists():
            continue
        dest = install_root / name
        if src.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
            for path in sorted(dest.rglob("*")):
                if path.is_file():
                    written.append(path.relative_to(output_root).as_posix())
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            written.append(dest.relative_to(output_root).as_posix())
    return written


def _write_install_json(
    export_dir: Path,
    relative_path: str,
    payload: dict[str, Any],
    output_root: Path,
) -> str:
    path = export_dir / "install" / relative_path
    _write_text(path, json.dumps(payload, indent=2, sort_keys=True))
    return path.relative_to(output_root).as_posix()


def _write_install_text(
    export_dir: Path,
    relative_path: str,
    content: str,
    output_root: Path,
) -> str:
    path = export_dir / "install" / relative_path
    _write_text(path, content)
    return path.relative_to(output_root).as_posix()


def _write_host_install_assets(
    *,
    path_dir: Path,
    export_dir: Path,
    output_root: Path,
    manifest: PathManifest,
    skill: PathSkillConfig,
    host: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    written: list[str] = []
    install_targets: list[dict[str, Any]] = []

    if host == "claude":
        skill_rel = f".claude/skills/{skill.name}"
        written.extend(
            _copy_payload_to_install_tree(
                export_dir=export_dir,
                install_root=export_dir / "install" / skill_rel,
                output_root=output_root,
            )
        )
        install_targets.append(
            {
                "op": "copy_tree",
                "source": f"install/{skill_rel}",
                "destination": skill_rel,
            }
        )
        for agent in manifest.agents:
            rel_path = (
                f".claude/agents/{_claude_agent_name(skill.name, agent.agent_id)}.md"
            )
            written.append(
                _write_install_text(
                    export_dir,
                    rel_path,
                    _render_claude_agent(path_dir=path_dir, skill=skill, agent=agent),
                    output_root,
                )
            )
            install_targets.append(
                {
                    "op": "copy_file",
                    "source": f"install/{rel_path}",
                    "destination": rel_path,
                }
            )
        rules_rel = ".claude/CLAUDE.md"
        written.append(
            _write_install_text(
                export_dir,
                rules_rel,
                _render_claude_rules(manifest),
                output_root,
            )
        )
        install_targets.append(
            {
                "op": "merge_markdown",
                "source": f"install/{rules_rel}",
                "destination": rules_rel,
                "section_id": f"wayfinder-path:{manifest.slug}:claude-rules",
            }
        )
        settings_rel = ".claude/settings.json"
        written.append(
            _write_install_json(
                export_dir,
                settings_rel,
                json.loads(_render_claude_settings(manifest, skill)),
                output_root,
            )
        )
        install_targets.append(
            {
                "op": "merge_json",
                "source": f"install/{settings_rel}",
                "destination": settings_rel,
            }
        )
        return written, install_targets

    if host == "opencode":
        skill_rel = f".opencode/skills/{skill.name}"
        written.extend(
            _copy_payload_to_install_tree(
                export_dir=export_dir,
                install_root=export_dir / "install" / skill_rel,
                output_root=output_root,
            )
        )
        install_targets.append(
            {
                "op": "copy_tree",
                "source": f"install/{skill_rel}",
                "destination": skill_rel,
            }
        )
        orchestrator_rel = f".opencode/agents/{skill.name}-orchestrator.md"
        written.append(
            _write_install_text(
                export_dir,
                orchestrator_rel,
                _render_opencode_orchestrator(manifest, skill),
                output_root,
            )
        )
        install_targets.append(
            {
                "op": "copy_file",
                "source": f"install/{orchestrator_rel}",
                "destination": orchestrator_rel,
            }
        )
        for agent in manifest.agents:
            rel_path = f".opencode/agents/{_opencode_agent_name(skill.name, agent.agent_id)}.md"
            written.append(
                _write_install_text(
                    export_dir,
                    rel_path,
                    _render_opencode_worker(
                        path_dir=path_dir, skill=skill, agent=agent
                    ),
                    output_root,
                )
            )
            install_targets.append(
                {
                    "op": "copy_file",
                    "source": f"install/{rel_path}",
                    "destination": rel_path,
                }
            )
        command_name = (
            manifest.pipeline.entry_command
            if manifest.pipeline and manifest.pipeline.entry_command
            else skill.name
        )
        command_rel = f".opencode/commands/{command_name}.md"
        written.append(
            _write_install_text(
                export_dir,
                command_rel,
                _render_opencode_command(manifest, skill),
                output_root,
            )
        )
        install_targets.append(
            {
                "op": "copy_file",
                "source": f"install/{command_rel}",
                "destination": command_rel,
            }
        )
        for rel_path, content in (
            (".opencode/plugins/pipeline-state.ts", _render_opencode_plugin_state()),
            (".opencode/plugins/trade-guard.ts", _render_opencode_plugin_guard()),
            (
                ".opencode/tools/compile_job.ts",
                _render_opencode_tool("compile_job", "Compile a validated runner job."),
            ),
            (
                ".opencode/tools/validate_order.ts",
                _render_opencode_tool(
                    "validate_order", "Validate a candidate order payload."
                ),
            ),
        ):
            written.append(
                _write_install_text(export_dir, rel_path, content, output_root)
            )
            install_targets.append(
                {
                    "op": "copy_file",
                    "source": f"install/{rel_path}",
                    "destination": rel_path,
                }
            )
        written.append(
            _write_install_text(
                export_dir,
                "AGENTS.md",
                _render_opencode_agents_md(manifest, skill),
                output_root,
            )
        )
        install_targets.append(
            {
                "op": "merge_markdown",
                "source": "install/AGENTS.md",
                "destination": "AGENTS.md",
                "section_id": f"wayfinder-path:{manifest.slug}:opencode-rules",
            }
        )
        written.append(
            _write_install_json(
                export_dir,
                "opencode.json",
                json.loads(_render_opencode_config()),
                output_root,
            )
        )
        install_targets.append(
            {
                "op": "merge_json",
                "source": "install/opencode.json",
                "destination": "opencode.json",
            }
        )
        return written, install_targets

    return written, install_targets


def _inject_required_skills(manifest: PathManifest, body: str) -> str:
    """Prepend data-source prerequisite directives from the archetype."""
    if not manifest.pipeline or not manifest.pipeline.archetype:
        return body
    try:
        archetype = get_pipeline_archetype(manifest.pipeline.archetype)
    except Exception:
        return body
    if not archetype.required_skills:
        return body
    skill_list = "\n".join(f"- `/{s}`" for s in archetype.required_skills)
    block = (
        "\n## Prerequisites\n\n"
        "Before writing any scripts or invoking workers, load these data-source skills.\n"
        "They document method signatures, return shapes, and field names for the "
        "clients and adapters this pipeline depends on.\n\n"
        f"{skill_list}\n"
    )
    return body.rstrip("\n") + "\n" + block + "\n"


def _write_host_artifacts(
    *,
    path_dir: Path,
    manifest: PathManifest,
    skill: PathSkillConfig,
    runtime: PathSkillRuntimeConfig,
    host: str,
    output_root: Path,
    body: str,
) -> tuple[list[str], PathSkillExportInfo]:
    export_dir = _export_dir(output_root, host, skill.name)
    _reset_export_dir(export_dir)

    written: list[str] = []
    written.extend(_copy_optional_dirs(path_dir, export_dir))
    written.extend(_copy_runtime_path(path_dir, export_dir))

    body = _inject_required_skills(manifest, body)

    if skill.source == "provided":
        skill_md = body
    elif host == "claude":
        skill_md = _render_claude_skill(manifest, skill, body)
    elif host == "opencode":
        skill_md = _render_opencode_skill(manifest, skill, body)
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

    install_written, install_targets = _write_host_install_assets(
        path_dir=path_dir,
        export_dir=export_dir,
        output_root=output_root,
        manifest=manifest,
        skill=skill,
        host=host,
    )
    written.extend(install_written)
    if install_targets:
        export_manifest["install_targets"] = install_targets
        _write_text(
            export_manifest_path, json.dumps(export_manifest, indent=2, sort_keys=True)
        )

    info = PathSkillExportInfo(
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
    path_dir: Path,
    output_root: Path | None = None,
    hosts: list[str] | tuple[str, ...] | None = None,
) -> PathSkillRenderReport:
    path_dir = path_dir.resolve()
    manifest_path = path_dir / "wfpath.yaml"
    if not manifest_path.exists():
        raise PathSkillRenderError(f"Missing wfpath.yaml in {path_dir}")

    try:
        manifest = PathManifest.load(manifest_path)
    except PathManifestError as exc:
        raise PathSkillRenderError(str(exc)) from exc

    if not manifest.skill or not manifest.skill.enabled:
        return PathSkillRenderReport(
            output_root=_build_root(path_dir, output_root),
            skill_name=None,
            rendered_hosts=[],
            written_files=[],
            exports={},
        )

    selected_hosts = list(hosts) if hosts is not None else list(_HOSTS)
    invalid_hosts = [host for host in selected_hosts if host not in _HOSTS]
    if invalid_hosts:
        raise PathSkillRenderError(
            f"Unsupported render host(s): {', '.join(sorted(invalid_hosts))}"
        )

    output_root_resolved = _build_root(path_dir, output_root)
    body = _source_markdown(path_dir, manifest.skill)
    runtime = resolve_skill_runtime(manifest)
    if runtime.mode != "thin":
        raise PathSkillRenderError(
            "Only skill.runtime.mode=thin is supported for host skill exports"
        )

    rendered_hosts: list[str] = []
    written_files: list[str] = []
    exports: dict[str, PathSkillExportInfo] = {}
    for host in selected_hosts:
        rendered_hosts.append(host)
        host_written, export_info = _write_host_artifacts(
            path_dir=path_dir,
            manifest=manifest,
            skill=manifest.skill,
            runtime=runtime,
            host=host,
            output_root=output_root_resolved,
            body=body,
        )
        written_files.extend(host_written)
        exports[host] = export_info

    return PathSkillRenderReport(
        output_root=output_root_resolved,
        skill_name=manifest.skill.name,
        rendered_hosts=rendered_hosts,
        written_files=sorted(written_files),
        exports=exports,
    )
