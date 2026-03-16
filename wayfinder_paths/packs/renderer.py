from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from wayfinder_paths.packs.manifest import (
    PackManifest,
    PackManifestError,
    PackSkillConfig,
)


class PackSkillRenderError(Exception):
    pass


_HOSTS = ("claude", "codex", "openclaw", "portable")
_CANONICAL_SKILL_SUBDIRS = ("scripts", "references", "assets")


@dataclass(frozen=True)
class PackSkillRenderReport:
    output_root: Path
    rendered_hosts: list[str]
    written_files: list[str]


def _component_path_from_manifest(manifest: PackManifest) -> str:
    components = manifest.raw.get("components")
    if isinstance(components, list) and components:
        first = components[0]
        if isinstance(first, dict):
            path_raw = str(first.get("path") or "").strip()
            if path_raw:
                return path_raw
    return "strategy.py" if manifest.primary_kind == "strategy" else "scripts/main.py"


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
    manifest: PackManifest, skill: PackSkillConfig, body: str
) -> str:
    metadata: dict[str, object] = {"tags": manifest.tags}
    if skill.openclaw:
        if skill.openclaw.user_invocable is not None:
            metadata["user-invocable"] = skill.openclaw.user_invocable
        if skill.openclaw.requires:
            metadata["requires"] = skill.openclaw.requires
        if skill.openclaw.install:
            metadata["install"] = skill.openclaw.install
    lines = [
        f"name: {skill.name}",
        f"description: {_quote_yaml(skill.description)}",
        f"metadata: {json.dumps(metadata, separators=(',', ':'), sort_keys=True)}",
    ]
    return _wrap_frontmatter(lines, body)


def _render_portable_skill(
    manifest: PackManifest, skill: PackSkillConfig, body: str
) -> str:
    metadata: dict[str, object] = {"tags": manifest.tags}
    if skill.portable:
        if skill.portable.python:
            metadata["python"] = skill.portable.python
        if skill.portable.package:
            metadata["package"] = skill.portable.package
    lines = [
        f"name: {skill.name}",
        f"description: {_quote_yaml(skill.description)}",
        f"metadata: {json.dumps(metadata, separators=(',', ':'), sort_keys=True)}",
    ]
    return _wrap_frontmatter(lines, body)


def _render_portable_launcher(manifest: PackManifest) -> str:
    component_path = _component_path_from_manifest(manifest)
    return "\n".join(
        [
            "#!/usr/bin/env python3",
            "from __future__ import annotations",
            "",
            "import argparse",
            "import subprocess",
            "import sys",
            "from pathlib import Path",
            "",
            "",
            f"PACK_SLUG = {component_path!r}",
            "",
            "",
            "def _pack_root() -> Path:",
            "    return Path(__file__).resolve().parents[5]",
            "",
            "",
            "def main() -> int:",
            "    parser = argparse.ArgumentParser(",
            "        description='Run the pack\\'s primary Python component from the portable skill export.'",
            "    )",
            "    parser.add_argument('args', nargs='*')",
            "    parsed = parser.parse_args()",
            "    target = _pack_root() / PACK_SLUG",
            "    if not target.exists():",
            "        raise SystemExit(f'Pack component not found: {target}')",
            "    cmd = [sys.executable, str(target), *parsed.args]",
            "    return subprocess.call(cmd)",
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


def _write_host_artifacts(
    *,
    pack_dir: Path,
    manifest: PackManifest,
    skill: PackSkillConfig,
    host: str,
    output_root: Path,
    body: str,
) -> list[str]:
    export_dir = _export_dir(output_root, host, skill.name)
    _reset_export_dir(export_dir)

    written: list[str] = []
    written.extend(_copy_optional_dirs(pack_dir, export_dir))

    if skill.source == "provided":
        skill_md = body
    elif host == "claude":
        skill_md = _render_claude_skill(manifest, skill, body)
    elif host == "codex":
        skill_md = _render_codex_skill(manifest, skill, body)
    elif host == "openclaw":
        skill_md = _render_openclaw_skill(manifest, skill, body)
    else:
        skill_md = _render_portable_skill(manifest, skill, body)

    skill_md_path = export_dir / "SKILL.md"
    _write_text(skill_md_path, skill_md)
    written.append(skill_md_path.relative_to(output_root).as_posix())

    if host == "codex":
        policy_path = export_dir / "agents" / "openai.yaml"
        _write_text(policy_path, _render_codex_policy(skill))
        written.append(policy_path.relative_to(output_root).as_posix())
    elif host == "portable":
        launcher_path = export_dir / "scripts" / "run_pack.py"
        _write_text(launcher_path, _render_portable_launcher(manifest))
        written.append(launcher_path.relative_to(output_root).as_posix())

    return written


def render_skill_exports(
    *,
    pack_dir: Path,
    output_root: Path | None = None,
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
            rendered_hosts=[],
            written_files=[],
        )

    output_root_resolved = _build_root(pack_dir, output_root)
    body = _source_markdown(pack_dir, manifest.skill)

    rendered_hosts: list[str] = []
    written_files: list[str] = []
    for host in _HOSTS:
        rendered_hosts.append(host)
        written_files.extend(
            _write_host_artifacts(
                pack_dir=pack_dir,
                manifest=manifest,
                skill=manifest.skill,
                host=host,
                output_root=output_root_resolved,
                body=body,
            )
        )

    return PackSkillRenderReport(
        output_root=output_root_resolved,
        rendered_hosts=rendered_hosts,
        written_files=sorted(written_files),
    )
