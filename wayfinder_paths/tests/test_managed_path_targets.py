import click
import pytest

from wayfinder_paths.paths.cli import _resolve_install_target_paths


@pytest.mark.parametrize(
    "destination",
    [
        "config.json",
        ".opencode/opencode.jsonc",
        ".opencode/package.json",
        ".opencode/node_modules",
        ".opencode/plugins/wayfinder-compaction.ts",
        ".opencode/agents/wayfinder.md",
        "../escape",
        "/etc/passwd",
    ],
)
def test_hosted_installer_rejects_platform_destinations(
    monkeypatch, tmp_path, destination
):
    monkeypatch.setenv("WAYFINDER_MANAGED_PATHS", "1")
    with pytest.raises(click.ClickException):
        _resolve_install_target_paths(
            source_dir=tmp_path / "source",
            destination_root=tmp_path / "sdk",
            target={"source": "file", "destination": destination},
        )


@pytest.mark.parametrize(
    "destination",
    [
        "opencode.json",
        "AGENTS.md",
        ".opencode/skills/demo",
        ".opencode/plugins/pipeline-state.ts",
        ".opencode/agents/demo-orchestrator.md",
    ],
)
def test_hosted_installer_allows_path_extensions(monkeypatch, tmp_path, destination):
    monkeypatch.setenv("WAYFINDER_MANAGED_PATHS", "1")
    _, target = _resolve_install_target_paths(
        source_dir=tmp_path / "source",
        destination_root=tmp_path / "sdk",
        target={"source": "file", "destination": destination},
    )
    assert target == tmp_path / "sdk" / destination


def test_hosted_installer_rejects_symlink_escape(monkeypatch, tmp_path):
    monkeypatch.setenv("WAYFINDER_MANAGED_PATHS", "1")
    root = tmp_path / "sdk"
    (root / ".opencode").mkdir(parents=True)
    (root / ".opencode" / "skills").symlink_to(tmp_path / "outside")
    with pytest.raises(click.ClickException):
        _resolve_install_target_paths(
            source_dir=tmp_path,
            destination_root=root,
            target={"source": "file", "destination": ".opencode/skills/escape"},
        )
