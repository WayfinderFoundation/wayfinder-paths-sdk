from __future__ import annotations

from typing import Any

from wayfinder_paths.paths.manifest import PathManifest


def _skill_name(manifest: PathManifest) -> str:
    if manifest.skill and manifest.skill.name:
        return manifest.skill.name
    return manifest.slug


def _command_name(manifest: PathManifest, *, skill_name: str) -> str:
    if manifest.pipeline and manifest.pipeline.entry_command:
        return manifest.pipeline.entry_command
    return skill_name


def build_path_invocation_guidance(
    manifest: PathManifest,
    *,
    host: str | None = None,
) -> dict[str, Any]:
    """Return deterministic user-facing guidance for running an installed Path."""

    normalized_host = str(host or "").strip().lower() or None
    skill_name = _skill_name(manifest)
    command_name = _command_name(manifest, skill_name=skill_name)
    display_name = manifest.name.strip() or manifest.slug
    example_prompt = f"Run the {display_name} Path."
    example_prompts = [example_prompt]
    if manifest.summary.strip():
        example_prompts.append(f"Use {skill_name} for: {manifest.summary.strip()}")
    else:
        example_prompts.append(f"Use {skill_name} to complete this workflow.")

    slash_command = f"/{command_name}" if normalized_host == "opencode" else None
    instructions = (
        [
            f"In OpenCode, type `{slash_command}` followed by your task or context.",
            f'You can also ask your agent: "{example_prompt}"',
        ]
        if slash_command
        else [f'Ask your agent: "{example_prompt}"']
    )

    return {
        "title": "How to invoke this Path",
        "host": normalized_host,
        "skill_name": skill_name,
        "command_name": command_name,
        "slash_command": slash_command,
        "example_prompt": example_prompt,
        "example_prompts": example_prompts,
        "instructions": instructions,
    }
