from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

from wayfinder_paths.packs.builder import PackBuilder
from wayfinder_paths.packs.doctor import run_doctor
from wayfinder_paths.packs.hooks import install_pack_hooks
from wayfinder_paths.packs.manifest import PackManifest
from wayfinder_paths.packs.preview import inspect_preview_pack
from wayfinder_paths.packs.renderer import render_skill_exports
from wayfinder_paths.packs.scaffold import init_pack


@pytest.mark.smoke
def test_pack_init_creates_expected_files(tmp_path: Path):
    pack_dir = tmp_path / "basis-board"
    result = init_pack(
        pack_dir=pack_dir,
        slug="basis-board",
        name="Basis Board",
        version="0.1.0",
        summary="Test pack",
        primary_kind="monitor",
        with_applet=True,
    )

    assert result.manifest_path.exists()
    assert (pack_dir / "README.md").exists()
    assert (pack_dir / "skill" / "instructions.md").exists()
    assert (pack_dir / "scripts" / "main.py").exists()
    assert (pack_dir / "applet" / "applet.manifest.json").exists()
    assert (pack_dir / "applet" / "dist" / "index.html").exists()
    assert (pack_dir / "applet" / "dist" / "assets" / "app.js").exists()
    assert (pack_dir / ".wayfinder" / "template.json").exists()

    manifest = PackManifest.load(result.manifest_path)
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


def test_pack_init_no_skill_omits_skill_source(tmp_path: Path):
    pack_dir = tmp_path / "basic-pack"
    init_pack(
        pack_dir=pack_dir,
        slug="basic-pack",
        primary_kind="script",
        with_applet=False,
        with_skill=False,
    )

    manifest = PackManifest.load(pack_dir / "wfpack.yaml")
    assert manifest.skill is None
    assert not (pack_dir / "skill").exists()


def test_pack_doctor_ok_on_scaffolded_pack(tmp_path: Path):
    pack_dir = tmp_path / "demo"
    init_pack(
        pack_dir=pack_dir,
        slug="demo",
        primary_kind="monitor",
        with_applet=True,
    )

    report = run_doctor(pack_dir=pack_dir, fix=False)
    assert report.ok is True
    assert report.errors == []


def test_pack_doctor_fix_creates_missing_readme_and_generated_instructions(
    tmp_path: Path,
):
    pack_dir = tmp_path / "minimal"
    init_pack(
        pack_dir=pack_dir,
        slug="minimal",
        primary_kind="monitor",
        with_applet=False,
        overwrite=True,
    )

    (pack_dir / "README.md").unlink()
    (pack_dir / "skill" / "instructions.md").unlink()

    report = run_doctor(pack_dir=pack_dir, fix=True)
    assert report.ok is True
    assert "README.md" in report.created_files
    assert "skill/instructions.md" in report.created_files


def test_pack_doctor_provided_skill_requires_skill_md(tmp_path: Path):
    pack_dir = tmp_path / "provided-skill"
    init_pack(
        pack_dir=pack_dir,
        slug="provided-skill",
        primary_kind="monitor",
        with_applet=False,
    )

    manifest_path = pack_dir / "wfpack.yaml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "  source: generated\n"
            '  name: "provided-skill"\n'
            '  description: "Use the provided-skill pack through Wayfinder."\n'
            '  instructions: "skill/instructions.md"\n',
            "  source: provided\n"
            '  name: "provided-skill"\n'
            '  description: "Use the provided-skill pack through Wayfinder."\n',
        ),
        encoding="utf-8",
    )
    (pack_dir / "skill" / "instructions.md").unlink()

    report = run_doctor(pack_dir=pack_dir, fix=False)
    assert report.ok is False
    assert any("skill/SKILL.md" in (issue.path or "") for issue in report.errors)


def test_pack_doctor_warns_on_legacy_skill_portable(tmp_path: Path):
    pack_dir = tmp_path / "portable-legacy"
    init_pack(
        pack_dir=pack_dir,
        slug="portable-legacy",
        primary_kind="monitor",
        with_applet=False,
    )

    manifest_path = pack_dir / "wfpack.yaml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            '  runtime:\n'
            '    mode: thin\n'
            '    package: "wayfinder-paths"\n'
            '    version: "0.8.0"\n'
            '    python: ">=3.12,<3.13"\n'
            '    component: "main"\n'
            '    bootstrap: uv\n'
            '    fallback_bootstrap: pipx\n'
            '    prefer_existing_runtime: true\n'
            '    require_api_key: false\n'
            '    api_key_env: "WAYFINDER_API_KEY"\n'
            '    config_path_env: "WAYFINDER_CONFIG_PATH"\n',
            '  portable:\n'
            '    python: ">=3.12,<3.13"\n'
            '    package: "wayfinder-paths"\n',
        ),
        encoding="utf-8",
    )

    report = run_doctor(pack_dir=pack_dir, fix=False)
    assert report.ok is True
    assert any("skill.portable is deprecated" in issue.message for issue in report.warnings)


def test_pack_doctor_rejects_embedded_skill_runtime_mode(tmp_path: Path):
    pack_dir = tmp_path / "embedded-mode"
    init_pack(
        pack_dir=pack_dir,
        slug="embedded-mode",
        primary_kind="monitor",
        with_applet=False,
    )

    manifest_path = pack_dir / "wfpack.yaml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "    mode: thin\n",
            "    mode: embedded\n",
        ),
        encoding="utf-8",
    )

    report = run_doctor(pack_dir=pack_dir, fix=False)
    assert report.ok is False
    assert any(
        "Only skill.runtime.mode=thin is supported" in issue.message
        for issue in report.errors
    )


def test_render_skill_exports_writes_all_hosts(tmp_path: Path):
    pack_dir = tmp_path / "render-demo"
    init_pack(
        pack_dir=pack_dir,
        slug="render-demo",
        primary_kind="monitor",
        with_applet=False,
    )

    report = render_skill_exports(pack_dir=pack_dir)

    assert report.rendered_hosts == ["claude", "codex", "openclaw", "portable"]
    assert (report.output_root / "claude" / "render-demo" / "SKILL.md").exists()
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
        report.output_root / "portable" / "render-demo" / "pack" / "wfpack.yaml"
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
    assert runtime_manifest["pack_version"] == "0.1.0"


def test_rendered_portable_export_runs_without_original_pack_tree(tmp_path: Path):
    pack_dir = tmp_path / "portable-demo"
    init_pack(
        pack_dir=pack_dir,
        slug="portable-demo",
        primary_kind="monitor",
        with_applet=False,
    )

    report = render_skill_exports(pack_dir=pack_dir)
    export_dir = report.exports["portable"].export_dir
    copied_export = tmp_path / "standalone-skill"
    import shutil

    shutil.copytree(export_dir, copied_export)
    shutil.rmtree(pack_dir)

    result = subprocess.run(
        [sys.executable, str(copied_export / "scripts" / "wf_bootstrap.py")],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "TODO: implement pack script logic" in result.stdout


def test_build_ignores_dot_build_artifacts(tmp_path: Path):
    pack_dir = tmp_path / "bundle-demo"
    init_pack(
        pack_dir=pack_dir,
        slug="bundle-demo",
        primary_kind="monitor",
        with_applet=False,
    )
    render_skill_exports(pack_dir=pack_dir)

    built = PackBuilder.build(
        pack_dir=pack_dir, out_path=pack_dir / "dist" / "bundle.zip"
    )

    with ZipFile(built.bundle_path, "r") as zf:
        names = zf.namelist()
    assert not any(name.startswith(".build/") for name in names)


def test_install_pack_hooks_is_idempotent(tmp_path: Path):
    first = install_pack_hooks(pack_dir=tmp_path)
    second = install_pack_hooks(pack_dir=tmp_path)

    config = yaml_safe_load(first.config_path)
    local_repo = next(repo for repo in config["repos"] if repo["repo"] == "local")
    hook_ids = [hook["id"] for hook in local_repo["hooks"]]

    assert first.changed is True
    assert second.changed is False
    assert hook_ids == [
        "wayfinder-pack-fmt",
        "wayfinder-pack-doctor",
        "wayfinder-pack-preview",
    ]


def test_preview_check_uses_applet_manifest_entry(tmp_path: Path):
    pack_dir = tmp_path / "preview-demo"
    init_pack(
        pack_dir=pack_dir,
        slug="preview-demo",
        primary_kind="monitor",
        with_applet=True,
    )

    applet_manifest_path = pack_dir / "applet" / "applet.manifest.json"
    applet_manifest = json.loads(applet_manifest_path.read_text(encoding="utf-8"))
    applet_manifest["entry"] = "dashboard.html"
    applet_manifest_path.write_text(
        json.dumps(applet_manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    (pack_dir / "applet" / "dist" / "dashboard.html").write_text(
        "<!doctype html><html><body>Preview</body></html>\n",
        encoding="utf-8",
    )

    inspection = inspect_preview_pack(pack_dir=pack_dir)
    assert inspection.entry == "dashboard.html"
    assert inspection.entry_path.name == "dashboard.html"


def yaml_safe_load(path: Path) -> dict[str, object]:
    import yaml

    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed
