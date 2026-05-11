from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml

from wayfinder_paths.paths.builder import PathBuilder
from wayfinder_paths.paths.doctor import run_doctor
from wayfinder_paths.paths.evaluator import run_path_eval
from wayfinder_paths.paths.hooks import install_path_hooks
from wayfinder_paths.paths.manifest import PathManifest
from wayfinder_paths.paths.preview import inspect_preview_path
from wayfinder_paths.paths.renderer import render_skill_exports
from wayfinder_paths.paths.scaffold import init_path


@pytest.mark.smoke
def test_path_init_creates_expected_files(tmp_path: Path):
    path_dir = tmp_path / "basis-board"
    result = init_path(
        path_dir=path_dir,
        slug="basis-board",
        name="Basis Board",
        version="0.1.0",
        summary="Test path",
        primary_kind="monitor",
        with_applet=True,
    )

    assert result.manifest_path.exists()
    assert (path_dir / "README.md").exists()
    assert (path_dir / "skill" / "instructions.md").exists()
    assert (path_dir / "scripts" / "main.py").exists()
    assert (path_dir / "applet" / "applet.manifest.json").exists()
    assert (path_dir / "applet" / "dist" / "index.html").exists()
    assert (path_dir / "applet" / "dist" / "assets" / "app.js").exists()
    assert (path_dir / ".wayfinder" / "template.json").exists()
    applet_html = (path_dir / "applet" / "dist" / "index.html").read_text(
        encoding="utf-8"
    )
    applet_js = (path_dir / "applet" / "dist" / "assets" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "Applet: live as of" in applet_html
    assert "dataUpdatedAt" in applet_js
    assert 'source: "live_applet"' in applet_js

    manifest = PathManifest.load(result.manifest_path)
    assert manifest.slug == "basis-board"
    assert manifest.version == "0.1.0"
    assert manifest.primary_kind == "monitor"
    assert manifest.applet is not None
    assert manifest.skill is not None
    assert manifest.skill.enabled is True
    assert manifest.skill.source == "generated"
    assert manifest.skill.instructions_path == "skill/instructions.md"
    assert manifest.skill.runtime is not None
    assert manifest.skill.runtime.mode == "thin"
    assert manifest.skill.runtime.component == "main"


def test_path_init_no_skill_omits_skill_source(tmp_path: Path):
    path_dir = tmp_path / "basic-path"
    init_path(
        path_dir=path_dir,
        slug="basic-path",
        primary_kind="script",
        with_applet=False,
        with_skill=False,
    )

    manifest = PathManifest.load(path_dir / "wfpath.yaml")
    assert manifest.skill is None
    assert not (path_dir / "skill").exists()


def test_path_init_strategy_uses_path_helper_names(tmp_path: Path):
    path_dir = tmp_path / "strategy-path"
    init_path(
        path_dir=path_dir,
        slug="strategy-path",
        primary_kind="strategy",
        with_applet=False,
        with_skill=True,
    )

    strategy_source = (path_dir / "strategy.py").read_text(encoding="utf-8")
    assert "def wfpath_meta()" in strategy_source
    assert "def wfpath_state()" in strategy_source
    assert "def wfpath_decision()" in strategy_source


def test_path_init_pipeline_scaffolds_contract_files(tmp_path: Path):
    path_dir = tmp_path / "conditional-router"
    result = init_path(
        path_dir=path_dir,
        slug="conditional-router",
        template="pipeline",
        archetype="conditional-router",
        with_skill=True,
        with_applet=False,
    )

    manifest = PathManifest.load(result.manifest_path)
    assert manifest.pipeline is not None
    assert manifest.pipeline.archetype == "conditional-router"
    assert manifest.pipeline.graph_path == "pipeline/graph.yaml"
    assert manifest.inputs
    assert manifest.agents
    assert (path_dir / "policy" / "default.yaml").exists()
    assert (path_dir / "pipeline" / "graph.yaml").exists()
    assert (path_dir / "skill" / "agents" / "poly-scout.md").exists()
    assert (path_dir / "tests" / "evals" / "host_render.yaml").exists()
    assert (path_dir / ".wf-artifacts" / "README.md").exists()
    assert "gold reference" in (path_dir / "README.md").read_text(encoding="utf-8")
    assert "recession_prob" in (path_dir / "policy" / "default.yaml").read_text(
        encoding="utf-8"
    )
    assert "If US recession probability rises above 60%" in (
        path_dir / "inputs" / "thesis.md"
    ).read_text(encoding="utf-8")
    assert 'selected_playbook.id: "risk_off"' in (
        path_dir / "tests" / "evals" / "output_shape.yaml"
    ).read_text(encoding="utf-8")
    assert "entry_command" in (path_dir / "scripts" / "main.py").read_text(
        encoding="utf-8"
    )


def test_path_doctor_ok_on_scaffolded_path(tmp_path: Path):
    path_dir = tmp_path / "demo"
    init_path(
        path_dir=path_dir,
        slug="demo",
        primary_kind="monitor",
        with_applet=True,
    )

    report = run_doctor(path_dir=path_dir, fix=False)
    assert report.ok is True
    assert report.errors == []


def test_path_doctor_ok_on_pipeline_path(tmp_path: Path):
    path_dir = tmp_path / "pipeline-demo"
    init_path(
        path_dir=path_dir,
        slug="pipeline-demo",
        template="pipeline",
        archetype="conditional-router",
        with_applet=False,
    )

    report = run_doctor(path_dir=path_dir, fix=False)
    assert report.ok is True


def test_path_doctor_rejects_agent_output_outside_artifacts_dir(tmp_path: Path):
    path_dir = tmp_path / "bad-output"
    init_path(
        path_dir=path_dir,
        slug="bad-output",
        template="pipeline",
        archetype="conditional-router",
        with_applet=False,
    )

    manifest_path = path_dir / "wfpath.yaml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            ".wf-artifacts/$RUN_ID/normalize_thesis.json",
            "../escape.json",
        ),
        encoding="utf-8",
    )

    report = run_doctor(path_dir=path_dir, fix=False)
    assert report.ok is False
    assert any("artifacts_dir" in issue.message for issue in report.errors)


def test_path_doctor_fix_creates_missing_readme_and_generated_instructions(
    tmp_path: Path,
):
    path_dir = tmp_path / "minimal"
    init_path(
        path_dir=path_dir,
        slug="minimal",
        primary_kind="monitor",
        with_applet=False,
        overwrite=True,
    )

    (path_dir / "README.md").unlink()
    (path_dir / "skill" / "instructions.md").unlink()

    report = run_doctor(path_dir=path_dir, fix=True)
    assert report.ok is True
    assert "README.md" in report.created_files
    assert "skill/instructions.md" in report.created_files


def test_path_doctor_provided_skill_requires_skill_md(tmp_path: Path):
    path_dir = tmp_path / "provided-skill"
    init_path(
        path_dir=path_dir,
        slug="provided-skill",
        primary_kind="monitor",
        with_applet=False,
    )

    manifest_path = path_dir / "wfpath.yaml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "  source: generated\n"
            '  name: "provided-skill"\n'
            '  description: "Use the provided-skill path through Wayfinder."\n'
            '  instructions: "skill/instructions.md"\n',
            "  source: provided\n"
            '  name: "provided-skill"\n'
            '  description: "Use the provided-skill path through Wayfinder."\n',
        ),
        encoding="utf-8",
    )
    (path_dir / "skill" / "instructions.md").unlink()

    report = run_doctor(path_dir=path_dir, fix=False)
    assert report.ok is False
    assert any("skill/SKILL.md" in (issue.path or "") for issue in report.errors)


def test_path_doctor_warns_on_legacy_skill_portable(tmp_path: Path):
    path_dir = tmp_path / "portable-legacy"
    init_path(
        path_dir=path_dir,
        slug="portable-legacy",
        primary_kind="monitor",
        with_applet=False,
    )

    manifest_path = path_dir / "wfpath.yaml"
    manifest_data = yaml_safe_load(manifest_path)
    skill = manifest_data.get("skill")
    assert isinstance(skill, dict)
    runtime = skill.pop("runtime", None)
    assert isinstance(runtime, dict)
    skill["portable"] = {
        "python": runtime["python"],
        "package": runtime["package"],
    }
    manifest_path.write_text(
        yaml.safe_dump(manifest_data, sort_keys=False), encoding="utf-8"
    )

    report = run_doctor(path_dir=path_dir, fix=False)
    assert report.ok is True
    assert any(
        "skill.portable is deprecated" in issue.message for issue in report.warnings
    )


def test_path_doctor_rejects_embedded_skill_runtime_mode(tmp_path: Path):
    path_dir = tmp_path / "embedded-mode"
    init_path(
        path_dir=path_dir,
        slug="embedded-mode",
        primary_kind="monitor",
        with_applet=False,
    )

    manifest_path = path_dir / "wfpath.yaml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "    mode: thin\n",
            "    mode: embedded\n",
        ),
        encoding="utf-8",
    )

    report = run_doctor(path_dir=path_dir, fix=False)
    assert report.ok is False
    assert any(
        "Only skill.runtime.mode=thin is supported" in issue.message
        for issue in report.errors
    )


def test_render_skill_exports_writes_all_hosts(tmp_path: Path):
    path_dir = tmp_path / "render-demo"
    init_path(
        path_dir=path_dir,
        slug="render-demo",
        primary_kind="monitor",
        with_applet=False,
    )

    report = render_skill_exports(path_dir=path_dir)

    assert report.rendered_hosts == [
        "claude",
        "opencode",
        "codex",
        "openclaw",
        "portable",
    ]
    assert (report.output_root / "claude" / "render-demo" / "SKILL.md").exists()
    assert (report.output_root / "opencode" / "render-demo" / "SKILL.md").exists()
    assert (report.output_root / "codex" / "render-demo" / "SKILL.md").exists()
    assert (
        report.output_root / "codex" / "render-demo" / "agents" / "openai.yaml"
    ).exists()
    assert (report.output_root / "openclaw" / "render-demo" / "SKILL.md").exists()
    assert (report.output_root / "portable" / "render-demo" / "SKILL.md").exists()
    assert (
        report.output_root / "portable" / "render-demo" / "scripts" / "wf_bootstrap.py"
    ).exists()
    assert (
        report.output_root / "portable" / "render-demo" / "scripts" / "wf_run.py"
    ).exists()
    assert (
        report.output_root / "portable" / "render-demo" / "runtime" / "manifest.json"
    ).exists()
    assert (
        report.output_root / "portable" / "render-demo" / "runtime" / "export.json"
    ).exists()
    assert (
        report.output_root / "portable" / "render-demo" / "path" / "wfpath.yaml"
    ).exists()
    assert (
        report.output_root / "opencode" / "render-demo" / "install" / "opencode.json"
    ).exists()
    runtime_manifest = json.loads(
        (
            report.output_root
            / "portable"
            / "render-demo"
            / "runtime"
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert runtime_manifest["version"]
    assert runtime_manifest["path_version"] == "0.1.0"


def test_path_eval_runs_fixture_and_host_render_checks(tmp_path: Path):
    path_dir = tmp_path / "eval-demo"
    init_path(
        path_dir=path_dir,
        slug="eval-demo",
        template="pipeline",
        archetype="conditional-router",
        with_applet=False,
    )

    report = run_path_eval(path_dir=path_dir)
    assert report.ok is True
    assert any(issue.name == "doctor" for issue in report.issues)


def test_rendered_portable_export_runs_without_original_path_tree(tmp_path: Path):
    path_dir = tmp_path / "portable-demo"
    init_path(
        path_dir=path_dir,
        slug="portable-demo",
        primary_kind="monitor",
        with_applet=False,
    )

    report = render_skill_exports(path_dir=path_dir)
    export_dir = report.exports["portable"].export_dir
    copied_export = tmp_path / "standalone-skill"
    import shutil

    shutil.copytree(export_dir, copied_export)
    shutil.rmtree(path_dir)

    result = subprocess.run(
        [sys.executable, str(copied_export / "scripts" / "wf_bootstrap.py")],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "TODO: implement path script logic" in result.stdout


def test_build_ignores_dot_build_artifacts(tmp_path: Path):
    path_dir = tmp_path / "bundle-demo"
    init_path(
        path_dir=path_dir,
        slug="bundle-demo",
        primary_kind="monitor",
        with_applet=False,
    )
    render_skill_exports(path_dir=path_dir)

    built = PathBuilder.build(
        path_dir=path_dir, out_path=path_dir / "dist" / "bundle.zip"
    )

    with ZipFile(built.bundle_path, "r") as zf:
        names = zf.namelist()
    assert not any(name.startswith(".build/") for name in names)


def test_install_path_hooks_is_idempotent(tmp_path: Path):
    first = install_path_hooks(path_dir=tmp_path)
    second = install_path_hooks(path_dir=tmp_path)

    config = yaml_safe_load(first.config_path)
    local_repo = next(repo for repo in config["repos"] if repo["repo"] == "local")
    hook_ids = [hook["id"] for hook in local_repo["hooks"]]

    assert first.changed is True
    assert second.changed is False
    assert hook_ids == [
        "wayfinder-path-fmt",
        "wayfinder-path-doctor",
        "wayfinder-path-preview",
    ]


def test_preview_check_uses_applet_manifest_entry(tmp_path: Path):
    path_dir = tmp_path / "preview-demo"
    init_path(
        path_dir=path_dir,
        slug="preview-demo",
        primary_kind="monitor",
        with_applet=True,
    )

    applet_manifest_path = path_dir / "applet" / "applet.manifest.json"
    applet_manifest = json.loads(applet_manifest_path.read_text(encoding="utf-8"))
    applet_manifest["entry"] = "dashboard.html"
    applet_manifest_path.write_text(
        json.dumps(applet_manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    (path_dir / "applet" / "dist" / "dashboard.html").write_text(
        "<!doctype html><html><body>Preview</body></html>\n",
        encoding="utf-8",
    )

    inspection = inspect_preview_path(path_dir=path_dir)
    assert inspection.entry == "dashboard.html"
    assert inspection.entry_path.name == "dashboard.html"


def yaml_safe_load(path: Path) -> dict[str, object]:
    import yaml

    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed
