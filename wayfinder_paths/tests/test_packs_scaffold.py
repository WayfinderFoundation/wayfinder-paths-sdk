from __future__ import annotations

from pathlib import Path

import pytest

from wayfinder_paths.packs.doctor import run_doctor
from wayfinder_paths.packs.manifest import PackManifest
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
    assert (pack_dir / "skill" / "SKILL.md").exists()
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


def test_pack_doctor_fix_creates_missing_readme_and_skill(tmp_path: Path):
    pack_dir = tmp_path / "minimal"
    init_pack(
        pack_dir=pack_dir,
        slug="minimal",
        primary_kind="monitor",
        with_applet=False,
        overwrite=True,
    )

    (pack_dir / "README.md").unlink()
    (pack_dir / "skill" / "SKILL.md").unlink()

    report = run_doctor(pack_dir=pack_dir, fix=True)
    assert report.ok is True
    assert "README.md" in report.created_files
    assert "skill/SKILL.md" in report.created_files
