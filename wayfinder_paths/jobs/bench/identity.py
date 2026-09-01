"""Runtime identity pins and preflight checks for comparable A/B arms."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from wayfinder_paths.jobs.bench.env import git_sha, sha256_file, sha256_json

IDENTITY_SCHEMA_VERSION = "1.0"


def ensure_model_declared(config_path: Path, model: str) -> bool:
    """Declare an API model in an isolated config; return whether it was added.

    OpenCode requires models to be present in its provider catalog even when
    the compatible upstream endpoint already serves them. The benchmark never
    mutates the source checkout's config.
    """
    provider_id, separator, model_id = model.partition("/")
    if not separator or not provider_id or not model_id:
        raise ValueError(f"model must be provider/model, got {model!r}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    provider = (config.get("provider") or {}).get(provider_id)
    if not isinstance(provider, dict):
        raise ValueError(f"provider {provider_id!r} is not configured")
    models = provider.setdefault("models", {})
    if model_id in models:
        return False
    template: dict[str, Any] = next(iter(models.values()), {})
    models[model_id] = {
        "name": model_id.replace("-", " ").title(),
        **({"limit": template["limit"]} if template.get("limit") else {}),
        **(
            {"interleaved": template["interleaved"]}
            if template.get("interleaved")
            else {}
        ),
    }
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return True


def runtime_identity(
    *,
    sdk_root: Path,
    sandbox: Path,
    model: str,
    variant: str | None,
    repeat_seed: int,
    world_manifest: dict[str, Any],
    prompt_hashes: list[str],
    declared_differences: list[str],
    arm_parameters: dict[str, Any],
    opencode: Path,
) -> dict[str, Any]:
    agents = sandbox / ".opencode" / "agents"
    config = sandbox / ".opencode" / "opencode.json"
    version = subprocess.run(
        [str(opencode), "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "sdk_ref": git_sha(sdk_root),
        "world_id": world_manifest["world_id"],
        "data": dict(world_manifest["dataset"]),
        "source_revision": world_manifest["source_revision"],
        "model": model,
        "variant": variant,
        "repeat_seed": repeat_seed,
        "agent_temperatures": {
            path.name: _agent_temperature(path) for path in sorted(agents.glob("*.md"))
        },
        "opencode_version": version.stdout.strip() or version.stderr.strip(),
        "opencode_config_sha256": sha256_file(config),
        "agent_hashes": {
            path.name: sha256_file(path) for path in sorted(agents.glob("*.md"))
        },
        "prompt_hashes": list(prompt_hashes),
        "initial_prompt_sha256": prompt_hashes[0] if prompt_hashes else None,
        "arm_parameters": dict(arm_parameters),
        "declared_differences": sorted(set(declared_differences)),
    }


def _agent_temperature(path: Path) -> float | None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return None
    _, separator, remainder = text.partition("---\n")
    frontmatter, closing, _ = remainder.partition("\n---\n")
    if not separator or not closing:
        return None
    parsed = yaml.safe_load(frontmatter) or {}
    temperature = parsed.get("temperature")
    return float(temperature) if temperature is not None else None


def compare_identities(
    left: dict[str, Any], right: dict[str, Any], *, allowed: set[str]
) -> dict[str, Any]:
    """Fail closed when arms differ outside their pre-registered fields."""
    ignored = {"declared_differences", *allowed}
    keys = set(left) | set(right)
    differences = {
        key: {"left": left.get(key), "right": right.get(key)}
        for key in sorted(keys - ignored)
        if sha256_json(left.get(key)) != sha256_json(right.get(key))
    }
    return {"comparable": not differences, "differences": differences}


def assert_isolation(*, sandbox: Path, sealed_dir: Path) -> None:
    sandbox = sandbox.resolve()
    sealed_dir = sealed_dir.resolve()
    if sealed_dir == sandbox or sealed_dir.is_relative_to(sandbox):
        raise ValueError("sealed holdout must be outside the arm sandbox")
    if sandbox.is_relative_to(sealed_dir):
        raise ValueError("arm sandbox must not be nested under the sealed holdout")
