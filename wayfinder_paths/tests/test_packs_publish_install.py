from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zipfile import ZipFile

from click.testing import CliRunner

from wayfinder_paths.packs.builder import PackBuilder
from wayfinder_paths.packs.cli import pack_cli
from wayfinder_paths.packs.scaffold import init_pack


def test_pack_publish_uploads_rendered_skill_exports(tmp_path: Path, monkeypatch):
    pack_dir = tmp_path / "skill-demo"
    init_pack(
        pack_dir=pack_dir,
        slug="skill-demo",
        primary_kind="monitor",
        with_applet=False,
        with_skill=True,
    )

    class FakePublishClient:
        calls: list[dict[str, object]] = []

        def __init__(self, *, api_base_url=None):
            self.api_base_url = api_base_url

        def publish(self, **kwargs):
            self.__class__.calls.append(kwargs)
            return {
                "pack": {"slug": "skill-demo"},
                "version": {"version": "0.1.0"},
                "claimRequired": True,
                "riskTier": "interactive",
                "manageUrl": "https://app.example/packs/skill-demo/manage",
                "packId": "0xabc123",
            }

    monkeypatch.setattr("wayfinder_paths.packs.cli.PacksApiClient", FakePublishClient)

    result = CliRunner().invoke(
        pack_cli,
        [
            "publish",
            "--path",
            str(pack_dir),
            "--out",
            str(pack_dir / "dist" / "bundle.zip"),
            "--api-url",
            "https://packs.example",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(FakePublishClient.calls) == 1
    call = FakePublishClient.calls[0]

    # owner_wallet must NOT be passed in publish kwargs
    assert "owner_wallet" not in call

    exports_manifest = call["exports_manifest"]
    skill_exports = call["skill_exports"]

    assert exports_manifest is not None
    assert exports_manifest["targets"] == ["claude", "codex", "openclaw", "portable"]
    assert set(skill_exports) == {"claude", "codex", "openclaw", "portable"}

    with ZipFile(io.BytesIO(skill_exports["claude"]), "r") as zf:
        names = set(zf.namelist())
    assert "skill/SKILL.md" in names

    with ZipFile(io.BytesIO(skill_exports["codex"]), "r") as zf:
        names = set(zf.namelist())
    assert "skill/agents/openai.yaml" in names

    # Claim URL should be printed to stderr when claimRequired is true
    assert "Claim ownership and bond at:" in result.output
    assert "https://app.example/packs/skill-demo/manage" in result.output


def test_pack_install_requests_intent_and_submits_receipt(tmp_path: Path, monkeypatch):
    pack_dir = tmp_path / "install-demo"
    init_pack(
        pack_dir=pack_dir,
        slug="install-demo",
        primary_kind="monitor",
        with_applet=False,
        with_skill=True,
    )
    built = PackBuilder.build(
        pack_dir=pack_dir, out_path=pack_dir / "dist" / "bundle.zip"
    )

    class FakeInstallClient:
        install_intent_calls: list[dict[str, object]] = []
        receipt_calls: list[dict[str, object]] = []

        def __init__(self, *, api_base_url=None):
            self.api_base_url = api_base_url

        def get_pack(self, *, slug: str):
            return {
                "pack": {"slug": slug, "latest_version": "0.1.0"},
                "versions": [
                    {"version": "0.1.0", "bundle_sha256": built.bundle_sha256}
                ],
            }

        def get_pack_version(self, *, slug: str, version: str):
            return {
                "version": {"version": version, "bundle_sha256": built.bundle_sha256}
            }

        def create_install_intent(self, **kwargs):
            self.__class__.install_intent_calls.append(kwargs)
            expires_at = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
            return {
                "intent": {
                    "intent_id": "intent-123",
                    "pack_slug": kwargs["slug"],
                    "version": kwargs["version"],
                    "bundle_sha256": built.bundle_sha256,
                    "issued_at": datetime.now(UTC).isoformat(),
                    "expires_at": expires_at,
                    "runtime": kwargs["runtime"],
                },
                "signature": "signed-intent",
            }

        def download_bundle(self, *, slug: str, version: str, out_path: Path):
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(built.bundle_path.read_bytes())
            return out_path

        def submit_install_receipt(self, **kwargs):
            self.__class__.receipt_calls.append(kwargs)
            return {"status": "recorded"}

    monkeypatch.setattr("wayfinder_paths.packs.cli.PacksApiClient", FakeInstallClient)

    install_root = tmp_path / ".wayfinder" / "packs"
    result = CliRunner().invoke(
        pack_cli,
        [
            "install",
            "--slug",
            "install-demo",
            "--version",
            "0.1.0",
            "--dir",
            str(install_root),
            "--api-url",
            "https://packs.example",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(FakeInstallClient.install_intent_calls) == 1
    assert len(FakeInstallClient.receipt_calls) == 1

    output = json.loads(result.output)
    assert output["result"]["install_intent_id"] == "intent-123"
    assert output["result"]["verified_install"] is True
    assert output["result"]["warnings"] == []

    receipt = FakeInstallClient.receipt_calls[0]
    assert receipt["runtime"] == "sdk-cli"
    assert receipt["extracted_files"] > 0
    assert receipt["install_path"].endswith("install-demo/0.1.0")
