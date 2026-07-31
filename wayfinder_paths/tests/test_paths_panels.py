"""Path panels: manifest parsing, doctor validation, and scaffolding."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from wayfinder_paths.paths.doctor import run_doctor
from wayfinder_paths.paths.manifest import PathManifest, PathManifestError
from wayfinder_paths.paths.preview import (
    PathPreviewError,
    _render_host_asset,
    inspect_panel_preview,
)
from wayfinder_paths.paths.scaffold import init_path

_BASE = """
schema_version: "0.1"
slug: funding-radar
name: Funding Radar
version: 0.1.0
capabilities: ["market.read", "wallet_read"]
panels:
  - id: radar
    name: Funding Radar
    build_dir: panels/radar/dist
    category: markets
    description: Live funding skew.
    size: { min_width: 320, min_height: 240 }
    capabilities: ["market.read"]
    permissions:
      external_origins: ["https://api.example.com", "wss://stream.example.com"]
"""


def _load(text: str) -> PathManifest:
    d = Path(tempfile.mkdtemp())
    (d / "wfpath.yaml").write_text(text)
    return PathManifest.load(d / "wfpath.yaml")


class TestManifestPanels:
    def test_valid_panel_parses(self) -> None:
        manifest = _load(_BASE)
        assert len(manifest.panels) == 1
        panel = manifest.panels[0]
        assert panel.panel_id == "radar"
        assert panel.entry == "index.html"  # default
        assert panel.size.min_width == 320
        assert panel.size.min_height == 240
        assert panel.capabilities == ("market.read",)
        assert panel.permissions.external_origins == (
            "https://api.example.com",
            "wss://stream.example.com",
        )

    def test_no_panels_key_is_empty(self) -> None:
        manifest = _load('schema_version: "0.1"\nslug: x\nname: X\nversion: 0.1.0\n')
        assert manifest.panels == ()

    def test_capability_must_be_subset(self) -> None:
        text = _BASE.replace('["market.read"]', '["positions.read"]')
        with pytest.raises(PathManifestError, match="not declared"):
            _load(text)

    def test_invalid_origin_rejected(self) -> None:
        text = _BASE.replace('"https://api.example.com"', '"ftp://nope.com"')
        with pytest.raises(PathManifestError, match="valid https/wss origin"):
            _load(text)

    def test_duplicate_ids_rejected(self) -> None:
        text = (
            _BASE
            + """  - id: radar
    name: Dup
    build_dir: panels/dup/dist
"""
        )
        with pytest.raises(PathManifestError, match="duplicate panel id"):
            _load(text)

    def test_too_many_panels_rejected(self) -> None:
        panels = "".join(
            f"  - id: p{i}\n    name: P{i}\n    build_dir: panels/p{i}/dist\n"
            for i in range(5)
        )
        text = (
            'schema_version: "0.1"\nslug: x\nname: X\nversion: 0.1.0\n'
            f"panels:\n{panels}"
        )
        with pytest.raises(PathManifestError, match="at most 4 panels"):
            _load(text)

    def test_traversal_build_dir_rejected(self) -> None:
        text = _BASE.replace("panels/radar/dist", "../escape/dist")
        with pytest.raises(PathManifestError, match="without '\\.\\.'"):
            _load(text)


class TestPanelScaffoldAndDoctor:
    def test_scaffold_creates_panel_files(self) -> None:
        d = Path(tempfile.mkdtemp()) / "demo"
        init_path(
            path_dir=d,
            slug="lp-manager",
            panels=["overview", "rewards"],
            with_skill=False,
            with_applet=False,
        )
        manifest = PathManifest.load(d / "wfpath.yaml")
        assert {p.panel_id for p in manifest.panels} == {"overview", "rewards"}
        assert manifest.panels[0].capabilities == ("market.read",)
        for pid in ("overview", "rewards"):
            for rel in (
                f"panels/{pid}/dist/index.html",
                f"panels/{pid}/dist/assets/panel.js",
                f"panels/{pid}/src/bridge.ts",
                f"panels/{pid}/README.md",
            ):
                assert (d / rel).exists(), rel

    def test_doctor_passes_on_scaffold(self) -> None:
        d = Path(tempfile.mkdtemp()) / "demo"
        init_path(
            path_dir=d,
            slug="lp-manager",
            panels=["overview"],
            with_skill=False,
            with_applet=False,
        )
        report = run_doctor(path_dir=d, host="opencode")
        assert report.errors == []

    def test_doctor_flags_missing_panel_build(self) -> None:
        d = Path(tempfile.mkdtemp()) / "demo"
        init_path(
            path_dir=d,
            slug="lp-manager",
            panels=["overview"],
            with_skill=False,
            with_applet=False,
        )
        # Remove the built entry to simulate a forgotten build step.
        (d / "panels/overview/dist/index.html").unlink()
        (d / "panels/overview/dist/assets/panel.js").unlink()
        report = run_doctor(path_dir=d, host="opencode")
        assert any("entry not found" in str(e).lower() for e in report.errors)


class TestPanelPreviewSandbox:
    def _scaffold(self) -> Path:
        d = Path(tempfile.mkdtemp()) / "demo"
        init_path(
            path_dir=d,
            slug="lp-manager",
            panels=["overview"],
            with_skill=False,
            with_applet=False,
        )
        return d

    def test_inspect_resolves_panel(self) -> None:
        d = self._scaffold()
        insp = inspect_panel_preview(path_dir=d, panel_id="overview", dev_server=None)
        assert insp.slug == "lp-manager"
        assert insp.panel.panel_id == "overview"
        assert insp.panel_root is not None and insp.panel_root.exists()

    def test_inspect_unknown_panel_errors(self) -> None:
        d = self._scaffold()
        with pytest.raises(PathPreviewError, match="not found"):
            inspect_panel_preview(path_dir=d, panel_id="ghost", dev_server=None)

    def test_dev_server_mode_skips_dist_check(self) -> None:
        d = self._scaffold()
        # Remove the built dist — dev-server mode must not require it.
        for f in (d / "panels/overview/dist").glob("**/*"):
            if f.is_file():
                f.unlink()
        insp = inspect_panel_preview(
            path_dir=d, panel_id="overview", dev_server="http://localhost:5173"
        )
        assert insp.panel_root is None

    def test_host_page_renders_static_mode(self) -> None:
        html = _render_host_asset(
            "index.html.tmpl",
            {
                "slug": "lp-manager",
                "panel_id": "overview",
                "panel_name": "Overview",
                "version": "0.1.0",
                "capabilities_json": json.dumps(["market.read"]),
                "dev_mode": "false",
                "dev_chip": "",
                "sandbox": "allow-scripts",
                "panel_src": "http://127.0.0.1:3334/index.html",
            },
        )
        assert "{{" not in html  # all placeholders rendered
        assert 'sandbox="allow-scripts"' in html
        assert "allow-same-origin" not in html  # production-parity isolation
        assert "lp-manager@0.1.0" in html
        assert '"market.read"' in html

    def test_host_page_dev_mode_relaxes_sandbox(self) -> None:
        html = _render_host_asset(
            "index.html.tmpl",
            {
                "slug": "x",
                "panel_id": "y",
                "panel_name": "Y",
                "version": "1",
                "capabilities_json": "[]",
                "dev_mode": "true",
                "dev_chip": '<span class="devchip">DEV MODE</span>',
                "sandbox": "allow-scripts allow-same-origin",
                "panel_src": "http://localhost:5173/",
            },
        )
        assert "allow-same-origin" in html
        assert "DEV MODE" in html
