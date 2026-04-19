from __future__ import annotations

import json
from pathlib import Path

import yaml

from wayfinder_paths.paths.doctor import run_doctor
from wayfinder_paths.paths.renderer import render_skill_exports
from wayfinder_paths.paths.scaffold import init_path


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_frontmatter(path: Path) -> dict:
    text = _read_text(path)
    start = text.find("---\n")
    end = text.find("\n---\n", start + 4)
    assert start == 0 and end > 0
    return yaml.safe_load(text[4:end]) or {}


def _make_pipeline_path(tmp_path: Path) -> Path:
    path_dir = tmp_path / "multi-asset-hedge-finder"
    init_path(
        path_dir=path_dir,
        slug="multi-asset-hedge-finder",
        template="pipeline",
        archetype="hedge-finder",
        with_skill=True,
        with_applet=False,
    )
    return path_dir


def test_opencode_export_is_model_neutral_and_callable_by_default(tmp_path: Path):
    path_dir = _make_pipeline_path(tmp_path)

    report = render_skill_exports(path_dir=path_dir, hosts=["opencode"])
    export_dir = report.exports["opencode"].export_dir

    orchestrator = (
        export_dir
        / "install"
        / ".opencode"
        / "agents"
        / "multi-asset-hedge-finder-orchestrator.md"
    )
    command = export_dir / "install" / ".opencode" / "commands" / "hedge-finder.md"
    worker = (
        export_dir
        / "install"
        / ".opencode"
        / "agents"
        / "multi-asset-hedge-finder-exposure-reader.md"
    )
    artifact_gate = (
        export_dir
        / "install"
        / ".opencode"
        / "tools"
        / "wayfinder_artifact_gate.ts"
    )
    opencode_config = export_dir / "install" / "opencode.json"
    export_manifest = json.loads(_read_text(export_dir / "runtime" / "export.json"))

    orchestrator_frontmatter = _load_frontmatter(orchestrator)
    command_frontmatter = _load_frontmatter(command)
    worker_frontmatter = _load_frontmatter(worker)
    opencode_config_payload = json.loads(_read_text(opencode_config))

    assert "model" not in orchestrator_frontmatter
    assert "model" not in command_frontmatter
    assert "model" not in worker_frontmatter
    assert orchestrator_frontmatter["mode"] == "all"
    assert command_frontmatter["subtask"] is True
    assert command_frontmatter["agent"] == "multi-asset-hedge-finder-orchestrator"
    assert worker_frontmatter["mode"] == "subagent"
    assert worker_frontmatter["hidden"] is True
    assert orchestrator_frontmatter["permission"]["task"]["*"] == "deny"
    assert "general" not in orchestrator_frontmatter["permission"]["task"]
    assert "explore" not in orchestrator_frontmatter["permission"]["task"]
    assert (
        orchestrator_frontmatter["permission"]["task"][
            "multi-asset-hedge-finder-exposure-reader"
        ]
        == "allow"
    )
    assert "@inputs/" not in _read_text(command)
    assert artifact_gate.exists()
    artifact_gate_text = _read_text(artifact_gate)
    assert "required_files: tool.schema.array" not in artifact_gate_text
    assert 'const REQUIRED_FILES = ["exposure_reader.json"' in artifact_gate_text
    assert "context?.worktree ?? context?.directory ?? process.cwd()" in artifact_gate_text
    assert "AGENTS.md" in opencode_config_payload["instructions"]
    assert (
        opencode_config_payload["agent"]["multi-asset-hedge-finder-orchestrator"][
            "permission"
        ]["skill"]["using-delta-lab"]
        == "allow"
    )
    assert export_manifest["install"]["preferred_sdk_command"].startswith(
        "wayfinder path install --slug multi-asset-hedge-finder --version 0.1.0 --host opencode --scope project"
    )
    assert export_manifest["requires"]["skills"][0]["path_slug"] == "using-delta-lab"
    assert export_manifest["requires"]["skills"][0]["skill_name"] == "using-delta-lab"


def test_opencode_export_renders_model_only_when_configured(tmp_path: Path):
    path_dir = _make_pipeline_path(tmp_path)
    manifest_path = path_dir / "wfpath.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest.setdefault("host", {})
    manifest["host"]["opencode"] = {
        **(manifest["host"].get("opencode") or {}),
        "model": "moonshot/kimi-k2-5",
    }
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    report = render_skill_exports(path_dir=path_dir, hosts=["opencode"])
    export_dir = report.exports["opencode"].export_dir
    orchestrator = (
        export_dir
        / "install"
        / ".opencode"
        / "agents"
        / "multi-asset-hedge-finder-orchestrator.md"
    )
    command = export_dir / "install" / ".opencode" / "commands" / "hedge-finder.md"
    export_manifest = json.loads(_read_text(export_dir / "runtime" / "export.json"))

    assert _load_frontmatter(orchestrator)["model"] == "moonshot/kimi-k2-5"
    assert _load_frontmatter(command)["model"] == "moonshot/kimi-k2-5"
    assert export_manifest["install"]["model"] == "moonshot/kimi-k2-5"


def test_opencode_doctor_validates_rendered_export_contract(tmp_path: Path):
    path_dir = _make_pipeline_path(tmp_path)

    report = run_doctor(path_dir=path_dir, host="opencode")

    assert not any("OpenCode" in issue.message for issue in report.errors)
    assert not any("orchestrator" in issue.message.lower() for issue in report.errors)


def test_dependency_resolution_unions_explicit_and_archetype_defaults(tmp_path: Path):
    path_dir = _make_pipeline_path(tmp_path)
    manifest_path = path_dir / "wfpath.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest.setdefault("skill", {})
    manifest["skill"]["dependencies"] = [
        {
            "name": "using-delta-lab",
            "path_slug": "delta-lab-pack",
            "host_names": {"opencode": "using-delta-lab-v2"},
        },
        {
            "name": "custom-market-data",
            "path_slug": "custom-market-data-pack",
            "required": True,
        },
    ]
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    report = render_skill_exports(path_dir=path_dir, hosts=["opencode"])
    export_manifest = json.loads(
        _read_text(report.exports["opencode"].export_dir / "runtime" / "export.json")
    )
    dependencies = {
        item["name"]: item for item in export_manifest["requires"]["skills"]
    }

    assert set(dependencies) == {
        "using-delta-lab",
        "using-hyperliquid-adapter",
        "using-pool-token-balance-data",
        "custom-market-data",
    }
    assert dependencies["using-delta-lab"]["path_slug"] == "delta-lab-pack"
    assert dependencies["using-delta-lab"]["skill_name"] == "using-delta-lab-v2"
    assert dependencies["using-hyperliquid-adapter"]["path_slug"] == "using-hyperliquid-adapter"


def test_claude_export_keeps_claude_dependency_language(tmp_path: Path):
    path_dir = _make_pipeline_path(tmp_path)
    manifest_path = path_dir / "wfpath.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest.setdefault("host", {})
    manifest["host"]["opencode"] = {"model": "moonshot/kimi-k2-5"}
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    report = render_skill_exports(path_dir=path_dir, hosts=["claude", "opencode"])
    claude_skill = (
        report.exports["claude"].export_dir
        / "install"
        / ".claude"
        / "skills"
        / "multi-asset-hedge-finder"
        / "SKILL.md"
    )
    claude_text = _read_text(claude_skill)

    assert "/using-delta-lab" in claude_text
    assert "moonshot/kimi-k2-5" not in claude_text
