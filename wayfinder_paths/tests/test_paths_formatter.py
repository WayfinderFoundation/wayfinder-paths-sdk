from __future__ import annotations

from pathlib import Path

import yaml

from wayfinder_paths.paths.formatter import format_path
from wayfinder_paths.paths.manifest import PathManifest
from wayfinder_paths.paths.scaffold import init_path


def test_format_path_preserves_skill_dependencies(tmp_path: Path):
    path_dir = tmp_path / "publisher-qc"
    result = init_path(
        path_dir=path_dir,
        slug="publisher-qc",
        primary_kind="monitor",
        with_applet=False,
        with_skill=True,
    )
    raw = yaml.safe_load(result.manifest_path.read_text(encoding="utf-8"))
    raw["skill"]["dependencies"] = [
        "auditor-a",
        {"name": "auditor-b", "path_slug": "auditor-b-pack", "required": False},
        {"name": "auditor-c", "host_names": {"claude": "auditor-c-claude"}},
    ]
    result.manifest_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    before = PathManifest.load(result.manifest_path).skill
    assert before is not None

    format_path(path_dir=path_dir)

    after = PathManifest.load(result.manifest_path).skill
    assert after is not None
    assert [
        (d.name, d.path_slug, d.required, d.host_names) for d in after.dependencies
    ] == [
        ("auditor-a", "auditor-a", True, {}),
        ("auditor-b", "auditor-b-pack", False, {}),
        ("auditor-c", "auditor-c", True, {"claude": "auditor-c-claude"}),
    ]
    assert [
        (d.name, d.path_slug, d.required, d.host_names) for d in before.dependencies
    ] == [(d.name, d.path_slug, d.required, d.host_names) for d in after.dependencies]

    # Idempotent: a second pass rewrites nothing in the manifest.
    assert "wfpath.yaml" not in format_path(path_dir=path_dir).changed_files
