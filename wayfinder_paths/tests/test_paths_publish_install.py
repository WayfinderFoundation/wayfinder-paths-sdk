from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zipfile import ZipFile

import httpx
from click.testing import CliRunner

from wayfinder_paths.paths.builder import PathBuilder
from wayfinder_paths.paths.cli import path_cli
from wayfinder_paths.paths.client import PathsApiClient
from wayfinder_paths.paths.doctor import DoctorIssue, PathDoctorReport
from wayfinder_paths.paths.scaffold import init_path


def test_path_publish_uploads_rendered_skill_exports_and_bond_metadata(
    tmp_path: Path, monkeypatch
):
    path_dir = tmp_path / "skill-demo"
    init_path(
        path_dir=path_dir,
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
                "path": {"slug": "skill-demo"},
                "version": {"version": "0.1.0"},
                "ownerLinkRequired": True,
                "effectiveRiskTier": "interactive",
                "requiredInitialBond": "1000",
                "requiredUpgradePendingBond": "1000",
                "manageUrl": "https://app.example/paths/skill-demo/manage?version=0.1.0",
                "reservationExpiresAt": "2026-04-15T00:00:00+00:00",
                "slugPermanent": False,
            }

    monkeypatch.setattr("wayfinder_paths.paths.cli.PathsApiClient", FakePublishClient)

    result = CliRunner().invoke(
        path_cli,
        [
            "publish",
            "--path",
            str(path_dir),
            "--out",
            str(path_dir / "dist" / "bundle.zip"),
            "--api-url",
            "https://paths.example",
            "--bonded",
            "--owner-wallet",
            "0x1234567890AbcdEF1234567890aBcdef12345678",
            "--risk-tier",
            "interactive",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(FakePublishClient.calls) == 1
    call = FakePublishClient.calls[0]

    assert call["owner_wallet"] == "0x1234567890AbcdEF1234567890aBcdef12345678"
    assert call["bonded"] is True
    assert call["risk_tier"] == "interactive"
    assert call["source_path"] is not None
    assert Path(call["source_path"]).name == "source.zip"

    exports_manifest = call["exports_manifest"]
    skill_exports = call["skill_exports"]

    assert exports_manifest is not None
    assert exports_manifest["targets"] == [
        "claude",
        "opencode",
        "codex",
        "openclaw",
        "portable",
    ]
    assert set(skill_exports) == {"claude", "opencode", "codex", "openclaw", "portable"}
    assert (
        exports_manifest["exports"]["portable"]["filename"] == "skill-portable-thin.zip"
    )
    assert exports_manifest["exports"]["portable"]["mode"] == "thin"
    assert exports_manifest["exports"]["portable"]["runtime"]["component"] == "main"

    with ZipFile(io.BytesIO(skill_exports["claude"]), "r") as zf:
        names = set(zf.namelist())
    assert "skill/SKILL.md" in names
    assert "skill/runtime/manifest.json" in names
    assert "skill/runtime/export.json" in names
    assert "skill/scripts/wf_bootstrap.py" in names
    assert "skill/scripts/wf_run.py" in names
    assert "skill/path/wfpath.yaml" in names
    assert "skill/install/.claude/skills/skill-demo/SKILL.md" in names
    assert not any(name.startswith("skill/applet/") for name in names)

    with ZipFile(io.BytesIO(skill_exports["opencode"]), "r") as zf:
        names = set(zf.namelist())
    assert "skill/install/.opencode/skills/skill-demo/SKILL.md" in names
    assert "skill/install/opencode.json" in names

    with ZipFile(io.BytesIO(skill_exports["codex"]), "r") as zf:
        names = set(zf.namelist())
    assert "skill/agents/openai.yaml" in names

    assert "Link owner wallet and bond at:" in result.output
    assert "https://app.example/paths/skill-demo/manage?version=0.1.0" in result.output
    assert "Effective risk tier: interactive" in result.output
    assert "Required initial bond: 1000" in result.output
    assert "Required upgrade pending bond: 1000" in result.output
    assert "Temporary slug reservation expires at:" in result.output
    assert "Slug reservation is temporary until approval/publication." in result.output


def test_paths_api_client_publish_uses_direct_upload_flow(tmp_path: Path):
    bundle_path = tmp_path / "bundle.zip"
    source_path = tmp_path / "source.zip"
    bundle_bytes = b"bundle-bytes"
    source_bytes = b"source-bytes"
    bundle_path.write_bytes(bundle_bytes)
    source_path.write_bytes(source_bytes)
    export_bytes = b"export-bytes"
    requests: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        requests.append((request.method, str(request.url), dict(request.headers), body))
        if (
            request.method == "POST"
            and request.url.path == "/api/v1/paths/publish/init/"
        ):
            payload = json.loads(body.decode("utf-8"))
            assert payload["manifest"]["slug"] == "skill-demo"
            assert payload["bundle_sha256"] == hashlib.sha256(bundle_bytes).hexdigest()
            assert payload["source_sha256"] == hashlib.sha256(source_bytes).hexdigest()
            assert (
                payload["skill_exports"]["claude"]["sha256"]
                == hashlib.sha256(export_bytes).hexdigest()
            )
            return httpx.Response(
                201,
                json={
                    "uploadId": "upload-1",
                    "finalizeToken": "token-1",
                    "artifacts": {
                        "bundle": {
                            "uploadUrl": "https://uploads.example/bundle",
                            "headers": {"Content-Type": "application/zip"},
                        },
                        "source": {
                            "uploadUrl": "https://uploads.example/source",
                            "headers": {"Content-Type": "application/zip"},
                        },
                        "skillExports": {
                            "claude": {
                                "uploadUrl": "https://uploads.example/claude",
                                "headers": {"Content-Type": "application/zip"},
                            }
                        },
                    },
                },
            )
        if (
            request.method == "PUT"
            and str(request.url) == "https://uploads.example/bundle"
        ):
            assert body == bundle_bytes
            return httpx.Response(200)
        if (
            request.method == "PUT"
            and str(request.url) == "https://uploads.example/source"
        ):
            assert body == source_bytes
            return httpx.Response(200)
        if (
            request.method == "PUT"
            and str(request.url) == "https://uploads.example/claude"
        ):
            assert body == export_bytes
            return httpx.Response(200)
        if (
            request.method == "POST"
            and request.url.path == "/api/v1/paths/publish/finalize/"
        ):
            payload = json.loads(body.decode("utf-8"))
            assert payload == {"upload_id": "upload-1", "finalize_token": "token-1"}
            return httpx.Response(
                201,
                json={"path": {"slug": "skill-demo"}, "version": {"version": "0.1.0"}},
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = PathsApiClient(
        api_base_url="https://paths.example",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    resp = client.publish(
        bundle_path=bundle_path,
        source_path=source_path,
        exports_manifest={
            "doctor": {"status": "ok", "warnings": []},
            "exports": {
                "claude": {
                    "filename": "skill-claude-thin.zip",
                    "mode": "thin",
                    "runtime": {"component": "main"},
                    "export": {"host": "claude"},
                }
            },
        },
        skill_exports={"claude": export_bytes},
        manifest={
            "schema_version": "0.1",
            "slug": "skill-demo",
            "name": "Skill Demo",
            "version": "0.1.0",
            "summary": "Demo",
            "primary_kind": "monitor",
        },
        applet_meta={},
        has_skill=True,
    )

    assert resp["path"]["slug"] == "skill-demo"
    assert any(
        method == "POST" and url.endswith("/publish/init/")
        for method, url, _headers, _body in requests
    )
    assert any(
        method == "PUT" and url == "https://uploads.example/bundle"
        for method, url, _headers, _body in requests
    )
    assert any(
        method == "POST" and url.endswith("/publish/finalize/")
        for method, url, _headers, _body in requests
    )


def test_path_build_is_deterministic(tmp_path: Path):
    path_dir = tmp_path / "deterministic-path"
    init_path(
        path_dir=path_dir,
        slug="deterministic-path",
        primary_kind="monitor",
        with_applet=False,
        with_skill=True,
    )

    first = PathBuilder.build(
        path_dir=path_dir,
        out_path=path_dir / "dist" / "bundle-a.zip",
    )
    second = PathBuilder.build(
        path_dir=path_dir,
        out_path=path_dir / "dist" / "bundle-b.zip",
    )
    source_archive = PathBuilder.build_source_archive(
        path_dir=path_dir,
        out_path=path_dir / "dist" / "source.zip",
    )

    assert first.bundle_sha256 == second.bundle_sha256
    assert first.bundle_path.read_bytes() == second.bundle_path.read_bytes()
    assert source_archive.exists()


def test_path_publish_requires_owner_wallet_for_bonded(tmp_path: Path):
    path_dir = tmp_path / "skill-demo"
    init_path(
        path_dir=path_dir,
        slug="skill-demo",
        primary_kind="monitor",
        with_applet=False,
        with_skill=True,
    )

    result = CliRunner().invoke(
        path_cli,
        [
            "publish",
            "--path",
            str(path_dir),
            "--bonded",
        ],
    )

    assert result.exit_code != 0
    assert "--owner-wallet is required with --bonded" in result.output


def test_path_activate_copies_rendered_export_to_host_scope(tmp_path: Path):
    path_dir = tmp_path / "activate-demo"
    init_path(
        path_dir=path_dir,
        slug="activate-demo",
        primary_kind="monitor",
        with_applet=False,
        with_skill=True,
    )

    runner = CliRunner()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    with runner.isolated_filesystem(temp_dir=str(workspace)):
        result = runner.invoke(
            path_cli,
            [
                "activate",
                "--host",
                "claude",
                "--scope",
                "project",
                "--path",
                str(path_dir),
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        dest = Path(payload["result"]["dest"])
        applied = payload["result"]["applied"]
        assert dest.name != "activate-demo"
        assert any(path.endswith(".claude/skills/activate-demo") for path in applied)
        assert (dest / ".claude" / "skills" / "activate-demo" / "SKILL.md").exists()
        assert (
            dest / ".claude" / "skills" / "activate-demo" / "runtime" / "manifest.json"
        ).exists()
        assert (dest / ".claude" / "CLAUDE.md").exists()
        assert (dest / ".claude" / "settings.json").exists()


def test_paths_api_client_list_paths_defaults_to_bonded_only():
    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "paths": [
                    {
                        "slug": "bonded-path",
                        "trust": {"tier": "bonded", "canonical_state": "active_stable"},
                        "trust_state": "active",
                        "active_bonded_version": "1.0.0",
                    },
                    {
                        "slug": "unbonded-path",
                        "trust": {"tier": "unbonded", "canonical_state": "idle"},
                        "trust_state": "unbonded",
                        "active_bonded_version": None,
                    },
                ]
            }

    class FakeHttpClient:
        def get(self, url, params=None, headers=None):
            return FakeResponse()

    client = PathsApiClient(
        api_base_url="https://paths.example",
        client=FakeHttpClient(),
    )

    bonded = client.list_paths()
    assert [path["slug"] for path in bonded] == ["bonded-path"]

    all_paths = client.list_paths(bonded_only=False)
    assert [path["slug"] for path in all_paths] == ["bonded-path", "unbonded-path"]


def test_path_install_requests_intent_and_submits_receipt(tmp_path: Path, monkeypatch):
    path_dir = tmp_path / "install-demo"
    init_path(
        path_dir=path_dir,
        slug="install-demo",
        primary_kind="monitor",
        with_applet=False,
        with_skill=True,
    )
    built = PathBuilder.build(
        path_dir=path_dir, out_path=path_dir / "dist" / "bundle.zip"
    )

    class FakeInstallClient:
        install_intent_calls: list[dict[str, object]] = []
        receipt_calls: list[dict[str, object]] = []

        def __init__(self, *, api_base_url=None):
            self.api_base_url = api_base_url

        def get_path(self, *, slug: str):
            return {
                "path": {"slug": slug, "latest_version": "0.1.0"},
                "versions": [
                    {"version": "0.1.0", "bundle_sha256": built.bundle_sha256}
                ],
            }

        def get_path_version(self, *, slug: str, version: str):
            return {
                "version": {"version": version, "bundle_sha256": built.bundle_sha256}
            }

        def create_install_intent(self, **kwargs):
            self.__class__.install_intent_calls.append(kwargs)
            expires_at = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
            return {
                "intent": {
                    "intent_id": "intent-123",
                    "path_slug": kwargs["slug"],
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
            return {
                "status": "recorded",
                "installation_id": "install-123",
                "heartbeat_token": "heartbeat-secret",
            }

        def submit_install_heartbeat(self, **kwargs):
            return {"status": "recorded", "installation_id": kwargs["installation_id"]}

    monkeypatch.setattr("wayfinder_paths.paths.cli.PathsApiClient", FakeInstallClient)

    install_root = tmp_path / ".wayfinder" / "paths"
    result = CliRunner().invoke(
        path_cli,
        [
            "install",
            "--slug",
            "install-demo",
            "--version",
            "0.1.0",
            "--dir",
            str(install_root),
            "--api-url",
            "https://paths.example",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(FakeInstallClient.install_intent_calls) == 1
    assert len(FakeInstallClient.receipt_calls) == 1
    assert FakeInstallClient.install_intent_calls[0]["venue"] == "sdk-cli"

    output = json.loads(result.output)
    assert output["result"]["install_intent_id"] == "intent-123"
    assert output["result"]["installation_id"] == "install-123"
    assert output["result"]["heartbeat_enabled"] is True
    assert output["result"]["verified_install"] is True
    assert output["result"]["warnings"] == []

    receipt = FakeInstallClient.receipt_calls[0]
    assert receipt["runtime"] == "sdk-cli"
    assert receipt["venue"] == "sdk-cli"
    assert receipt["extracted_files"] > 0
    assert receipt["install_path"].endswith("install-demo/0.1.0")

    lock = json.loads((tmp_path / ".wayfinder" / "paths.lock.json").read_text())
    assert lock["paths"]["install-demo"]["installation_id"] == "install-123"
    assert lock["paths"]["install-demo"]["heartbeat_token"] == "heartbeat-secret"
    assert lock["paths"]["install-demo"]["venue"] == "sdk-cli"


def test_path_install_migrates_legacy_lockfile_and_directory(
    tmp_path: Path, monkeypatch
):
    path_dir = tmp_path / "install-demo"
    init_path(
        path_dir=path_dir,
        slug="install-demo",
        primary_kind="monitor",
        with_applet=False,
        with_skill=True,
    )
    built = PathBuilder.build(
        path_dir=path_dir, out_path=path_dir / "dist" / "bundle.zip"
    )

    legacy_lock_dir = tmp_path / ".wayfinder"
    legacy_lock_dir.mkdir(parents=True, exist_ok=True)
    (legacy_lock_dir / "packs.lock.json").write_text(
        json.dumps(
            {
                "schemaVersion": "0.1",
                "packs": {
                    "legacy-path": {
                        "version": "0.0.1",
                        "bundle_sha256": "legacy-sha",
                    }
                },
            }
        )
        + "\n"
    )

    class FakeInstallClient:
        def __init__(self, *, api_base_url=None):
            self.api_base_url = api_base_url

        def get_path(self, *, slug: str):
            return {
                "path": {"slug": slug, "latest_version": "0.1.0"},
                "versions": [
                    {"version": "0.1.0", "bundle_sha256": built.bundle_sha256}
                ],
            }

        def get_path_version(self, *, slug: str, version: str):
            return {
                "version": {"version": version, "bundle_sha256": built.bundle_sha256}
            }

        def create_install_intent(self, **kwargs):
            return {
                "intent": {
                    "intent_id": "intent-456",
                    "path_slug": kwargs["slug"],
                    "version": kwargs["version"],
                },
                "signature": "signed-intent",
            }

        def download_bundle(self, *, slug: str, version: str, out_path: Path):
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(built.bundle_path.read_bytes())
            return out_path

        def submit_install_receipt(self, **kwargs):
            return {
                "status": "recorded",
                "installation_id": "install-456",
                "heartbeat_token": "heartbeat-456",
            }

    monkeypatch.setattr("wayfinder_paths.paths.cli.PathsApiClient", FakeInstallClient)

    result = CliRunner().invoke(
        path_cli,
        [
            "install",
            "--slug",
            "install-demo",
            "--version",
            "0.1.0",
            "--dir",
            str(tmp_path / ".wayfinder" / "packs"),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["result"]["dest"].endswith(".wayfinder/paths/install-demo/0.1.0")
    assert payload["result"]["lockfile"].endswith(".wayfinder/paths.lock.json")

    lock = json.loads((tmp_path / ".wayfinder" / "paths.lock.json").read_text())
    assert lock["paths"]["legacy-path"]["bundle_sha256"] == "legacy-sha"
    assert lock["paths"]["install-demo"]["installation_id"] == "install-456"
    assert not (tmp_path / ".wayfinder" / "packs" / "install-demo").exists()


def test_path_heartbeat_install_uses_lockfile_credentials(tmp_path: Path, monkeypatch):
    lock_dir = tmp_path / ".wayfinder"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "paths.lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "schemaVersion": "0.1",
                "paths": {
                    "install-demo": {
                        "version": "0.1.0",
                        "installation_id": "install-123",
                        "heartbeat_token": "heartbeat-secret",
                    }
                },
            }
        )
    )

    class FakeHeartbeatClient:
        heartbeat_calls: list[dict[str, object]] = []

        def __init__(self, *, api_base_url=None):
            self.api_base_url = api_base_url

        def submit_install_heartbeat(self, **kwargs):
            self.__class__.heartbeat_calls.append(kwargs)
            return {"status": "recorded", "installation_id": kwargs["installation_id"]}

    monkeypatch.setattr("wayfinder_paths.paths.cli.PathsApiClient", FakeHeartbeatClient)

    result = CliRunner().invoke(
        path_cli,
        [
            "heartbeat-install",
            "--slug",
            "install-demo",
            "--dir",
            str(tmp_path / ".wayfinder" / "paths"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert FakeHeartbeatClient.heartbeat_calls == [
        {
            "installation_id": "install-123",
            "heartbeat_token": "heartbeat-secret",
            "status": "active",
        }
    ]


def test_path_heartbeat_install_reads_legacy_lockfile(tmp_path: Path, monkeypatch):
    lock_dir = tmp_path / ".wayfinder"
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "packs.lock.json").write_text(
        json.dumps(
            {
                "schemaVersion": "0.1",
                "packs": {
                    "install-demo": {
                        "version": "0.1.0",
                        "installation_id": "install-123",
                        "heartbeat_token": "heartbeat-secret",
                    }
                },
            }
        )
    )

    class FakeHeartbeatClient:
        heartbeat_calls: list[dict[str, object]] = []

        def __init__(self, *, api_base_url=None):
            self.api_base_url = api_base_url

        def submit_install_heartbeat(self, **kwargs):
            self.__class__.heartbeat_calls.append(kwargs)
            return {"status": "recorded", "installation_id": kwargs["installation_id"]}

    monkeypatch.setattr("wayfinder_paths.paths.cli.PathsApiClient", FakeHeartbeatClient)

    result = CliRunner().invoke(
        path_cli,
        [
            "heartbeat-install",
            "--slug",
            "install-demo",
            "--dir",
            str(tmp_path / ".wayfinder" / "packs"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert FakeHeartbeatClient.heartbeat_calls == [
        {
            "installation_id": "install-123",
            "heartbeat_token": "heartbeat-secret",
            "status": "active",
        }
    ]


def _build_path_bundle(tmp_path: Path, *, slug: str, version: str) -> PathBuilder:
    path_dir = tmp_path / f"{slug}-{version}"
    init_path(
        path_dir=path_dir,
        slug=slug,
        version=version,
        primary_kind="monitor",
        with_applet=False,
        with_skill=True,
    )
    return PathBuilder.build(path_dir=path_dir, out_path=path_dir / "dist" / "bundle.zip")


def _write_paths_lockfile(tmp_path: Path, paths: dict[str, object]) -> Path:
    lock_dir = tmp_path / ".wayfinder"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "paths.lock.json"
    lock_path.write_text(
        json.dumps({"schemaVersion": "0.1", "paths": paths}, indent=2) + "\n"
    )
    return lock_path


def test_path_activate_records_activation_metadata_for_installed_path(tmp_path: Path, monkeypatch):
    installed_path = tmp_path / ".wayfinder" / "paths" / "activate-demo" / "0.1.0"
    init_path(
        path_dir=installed_path,
        slug="activate-demo",
        primary_kind="monitor",
        with_applet=False,
        with_skill=True,
    )
    _write_paths_lockfile(
        tmp_path,
        {
            "activate-demo": {
                "version": "0.1.0",
                "bundle_sha256": "abc123",
                "path": str(installed_path),
            }
        },
    )

    def fake_activate_export(*, host, scope, path_dir=None, export_path=None):
        assert export_path is None
        assert path_dir == installed_path.resolve()
        return {
            "host": host,
            "scope": scope,
            "source": str(path_dir),
            "dest": str(Path.cwd()),
            "mode": "install",
            "applied": [str(Path.cwd() / ".claude" / "skills" / "activate-demo")],
        }

    monkeypatch.setattr("wayfinder_paths.paths.cli._activate_export", fake_activate_export)

    runner = CliRunner()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    with runner.isolated_filesystem(temp_dir=str(workspace)):
        result = runner.invoke(
            path_cli,
            [
                "activate",
                "--host",
                "claude",
                "--scope",
                "project",
                "--path",
                str(installed_path),
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["result"]["activation_recorded"] is True

    lock = json.loads((tmp_path / ".wayfinder" / "paths.lock.json").read_text())
    activation = lock["paths"]["activate-demo"]["activation"]
    assert activation["host"] == "claude"
    assert activation["scope"] == "project"
    assert activation["mode"] == "install"
    assert activation["applied"][0].endswith(".claude/skills/activate-demo")


def test_path_activate_skips_doctor_for_installed_paths(tmp_path: Path, monkeypatch) -> None:
    installed_path = tmp_path / ".wayfinder" / "paths" / "activate-demo" / "0.1.0"
    init_path(
        path_dir=installed_path,
        slug="activate-demo",
        primary_kind="monitor",
        with_applet=False,
        with_skill=True,
    )
    _write_paths_lockfile(
        tmp_path,
        {
            "activate-demo": {
                "version": "0.1.0",
                "bundle_sha256": "abc123",
                "path": str(installed_path),
            }
        },
    )

    def fake_run_doctor(*, path_dir: Path, fix: bool, overwrite: bool) -> PathDoctorReport:
        return PathDoctorReport(
            ok=False,
            slug="activate-demo",
            version="0.1.0",
            primary_kind="monitor",
            errors=[DoctorIssue(level="error", message="doctor should be skipped")],
            warnings=[],
            created_files=[],
        )

    monkeypatch.setattr("wayfinder_paths.paths.cli.run_doctor", fake_run_doctor)

    runner = CliRunner()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    with runner.isolated_filesystem(temp_dir=str(workspace)):
        result = runner.invoke(
            path_cli,
            [
                "activate",
                "--host",
                "claude",
                "--scope",
                "project",
                "--path",
                str(installed_path),
            ],
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["result"]["activation_recorded"] is True
    assert Path(payload["result"]["dest"]).joinpath(
        ".claude", "skills", "activate-demo", "SKILL.md"
    ).exists()


def test_path_update_requires_existing_lock_entry(tmp_path: Path):
    _write_paths_lockfile(tmp_path, {})

    result = CliRunner().invoke(
        path_cli,
        [
            "update",
            "missing-demo",
            "--dir",
            str(tmp_path / ".wayfinder" / "paths"),
        ],
    )

    assert result.exit_code != 0
    assert "Path not found in lockfile: missing-demo" in result.output


def test_path_update_ignores_newer_latest_when_active_bonded_matches_install(
    tmp_path: Path, monkeypatch
):
    installed_path = tmp_path / ".wayfinder" / "paths" / "bonded-demo" / "1.0.0"
    installed_path.mkdir(parents=True, exist_ok=True)
    _write_paths_lockfile(
        tmp_path,
        {
            "bonded-demo": {
                "version": "1.0.0",
                "path": str(installed_path),
                "activation": {"host": "claude", "scope": "project"},
            }
        },
    )

    activation_calls: list[dict[str, object]] = []

    def fake_activate_export(*, host, scope, path_dir=None, export_path=None):
        activation_calls.append(
            {
                "host": host,
                "scope": scope,
                "path_dir": str(path_dir),
                "export_path": export_path,
            }
        )
        return {
            "host": host,
            "scope": scope,
            "source": str(path_dir),
            "dest": str(Path.cwd()),
            "mode": "install",
            "applied": [str(Path.cwd() / ".claude" / "skills" / "bonded-demo")],
        }

    class FakeUpdateClient:
        get_path_calls: list[str] = []

        def __init__(self, *, api_base_url=None):
            self.api_base_url = api_base_url

        def get_path(self, *, slug: str):
            self.__class__.get_path_calls.append(slug)
            return {
                "path": {
                    "slug": slug,
                    "latest_version": "1.1.0",
                    "active_bonded_version": "1.0.0",
                },
                "versions": [
                    {"version": "1.1.0", "bundle_sha256": "bb" * 32},
                    {"version": "1.0.0", "bundle_sha256": "aa" * 32},
                ],
            }

    monkeypatch.setattr("wayfinder_paths.paths.cli.PathsApiClient", FakeUpdateClient)
    monkeypatch.setattr("wayfinder_paths.paths.cli._activate_export", fake_activate_export)

    runner = CliRunner()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    with runner.isolated_filesystem(temp_dir=str(workspace)):
        result = runner.invoke(
            path_cli,
            [
                "update",
                "bonded-demo",
                "--dir",
                str(tmp_path / ".wayfinder" / "paths"),
                "--api-url",
                "https://paths.example",
            ],
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["result"]["current_version"] == "1.0.0"
    assert payload["result"]["target_version"] == "1.0.0"
    assert payload["result"]["updated"] is False
    assert payload["result"]["activated"] is True
    assert payload["result"]["activation_source"] == "lockfile"
    assert activation_calls == [
        {
            "host": "claude",
            "scope": "project",
            "path_dir": str(installed_path),
            "export_path": None,
        }
    ]


def test_path_update_reuses_lockfile_activation_for_live_bonded_upgrade(
    tmp_path: Path, monkeypatch
):
    built = _build_path_bundle(tmp_path, slug="update-demo", version="1.1.0")
    _write_paths_lockfile(
        tmp_path,
        {
            "update-demo": {
                "version": "1.0.0",
                "bundle_sha256": "11" * 32,
                "path": str(tmp_path / ".wayfinder" / "paths" / "update-demo" / "1.0.0"),
                "activation": {"host": "claude", "scope": "project"},
            }
        },
    )

    activation_calls: list[dict[str, object]] = []

    def fake_activate_export(*, host, scope, path_dir=None, export_path=None):
        activation_calls.append(
            {
                "host": host,
                "scope": scope,
                "path_dir": str(path_dir),
                "export_path": export_path,
            }
        )
        return {
            "host": host,
            "scope": scope,
            "source": str(path_dir),
            "dest": str(Path.cwd()),
            "mode": "install",
            "applied": [str(Path.cwd() / ".claude" / "skills" / "update-demo")],
        }

    class FakeUpdateClient:
        def __init__(self, *, api_base_url=None):
            self.api_base_url = api_base_url

        def get_path(self, *, slug: str):
            return {
                "path": {
                    "slug": slug,
                    "latest_version": "1.2.0",
                    "active_bonded_version": "1.1.0",
                },
                "versions": [
                    {"version": "1.1.0", "bundle_sha256": built.bundle_sha256},
                    {"version": "1.0.0", "bundle_sha256": "11" * 32},
                ],
            }

        def get_path_version(self, *, slug: str, version: str):
            return {
                "version": {"version": version, "bundle_sha256": built.bundle_sha256}
            }

        def create_install_intent(self, **kwargs):
            expires_at = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
            return {
                "intent": {
                    "intent_id": "intent-update",
                    "path_slug": kwargs["slug"],
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
            return {
                "status": "recorded",
                "installation_id": "install-update",
                "heartbeat_token": "heartbeat-update",
            }

    monkeypatch.setattr("wayfinder_paths.paths.cli.PathsApiClient", FakeUpdateClient)
    monkeypatch.setattr("wayfinder_paths.paths.cli._activate_export", fake_activate_export)

    runner = CliRunner()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    with runner.isolated_filesystem(temp_dir=str(workspace)):
        result = runner.invoke(
            path_cli,
            [
                "update",
                "update-demo",
                "--dir",
                str(tmp_path / ".wayfinder" / "paths"),
                "--api-url",
                "https://paths.example",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["result"]["updated"] is True
        assert payload["result"]["activated"] is True
        assert payload["result"]["activation_source"] == "lockfile"
        assert payload["result"]["target_version"] == "1.1.0"
        assert payload["result"]["install"]["installation_id"] == "install-update"

    assert activation_calls == [
        {
            "host": "claude",
            "scope": "project",
            "path_dir": str(tmp_path / ".wayfinder" / "paths" / "update-demo" / "1.1.0"),
            "export_path": None,
        }
    ]
    lock = json.loads((tmp_path / ".wayfinder" / "paths.lock.json").read_text())
    assert lock["paths"]["update-demo"]["version"] == "1.1.0"
    assert lock["paths"]["update-demo"]["installation_id"] == "install-update"
    assert lock["paths"]["update-demo"]["activation"]["host"] == "claude"


def test_path_update_uses_default_activation_when_workspace_has_single_marker(
    tmp_path: Path, monkeypatch
):
    built = _build_path_bundle(tmp_path, slug="default-demo", version="1.1.0")
    _write_paths_lockfile(
        tmp_path,
        {
            "default-demo": {
                "version": "1.0.0",
                "bundle_sha256": "11" * 32,
                "path": str(tmp_path / ".wayfinder" / "paths" / "default-demo" / "1.0.0"),
            }
        },
    )

    activation_calls: list[dict[str, object]] = []

    def fake_activate_export(*, host, scope, path_dir=None, export_path=None):
        activation_calls.append({"host": host, "scope": scope, "path_dir": str(path_dir)})
        return {
            "host": host,
            "scope": scope,
            "source": str(path_dir),
            "dest": str(Path.cwd()),
            "mode": "install",
            "applied": [str(Path.cwd() / ".claude" / "skills" / "default-demo")],
        }

    class FakeUpdateClient:
        def __init__(self, *, api_base_url=None):
            self.api_base_url = api_base_url

        def get_path(self, *, slug: str):
            return {
                "path": {
                    "slug": slug,
                    "latest_version": "1.2.0",
                    "active_bonded_version": "1.1.0",
                },
                "versions": [{"version": "1.1.0", "bundle_sha256": built.bundle_sha256}],
            }

        def get_path_version(self, *, slug: str, version: str):
            return {
                "version": {"version": version, "bundle_sha256": built.bundle_sha256}
            }

        def create_install_intent(self, **kwargs):
            return {
                "intent": {"intent_id": "intent-default", "version": kwargs["version"]},
                "signature": "signed-intent",
            }

        def download_bundle(self, *, slug: str, version: str, out_path: Path):
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(built.bundle_path.read_bytes())
            return out_path

        def submit_install_receipt(self, **kwargs):
            return {"status": "recorded", "installation_id": "install-default", "heartbeat_token": "heartbeat-default"}

    monkeypatch.setattr("wayfinder_paths.paths.cli.PathsApiClient", FakeUpdateClient)
    monkeypatch.setattr("wayfinder_paths.paths.cli._activate_export", fake_activate_export)

    runner = CliRunner()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    with runner.isolated_filesystem(temp_dir=str(workspace)):
        Path(".claude").mkdir()
        result = runner.invoke(
            path_cli,
            [
                "update",
                "default-demo",
                "--dir",
                str(tmp_path / ".wayfinder" / "paths"),
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["result"]["activated"] is True
        assert payload["result"]["activation_source"] == "default"

    assert activation_calls == [
        {
            "host": "claude",
            "scope": "project",
            "path_dir": str(tmp_path / ".wayfinder" / "paths" / "default-demo" / "1.1.0"),
        }
    ]


def test_path_update_falls_back_to_pull_without_activation_target(
    tmp_path: Path, monkeypatch
):
    built = _build_path_bundle(tmp_path, slug="manual-demo", version="1.1.0")
    _write_paths_lockfile(
        tmp_path,
        {
            "manual-demo": {
                "version": "1.0.0",
                "bundle_sha256": "11" * 32,
                "path": str(tmp_path / ".wayfinder" / "paths" / "manual-demo" / "1.0.0"),
            }
        },
    )

    class FakeUpdateClient:
        def __init__(self, *, api_base_url=None):
            self.api_base_url = api_base_url

        def get_path(self, *, slug: str):
            return {
                "path": {
                    "slug": slug,
                    "latest_version": "1.2.0",
                    "active_bonded_version": "1.1.0",
                },
                "versions": [{"version": "1.1.0", "bundle_sha256": built.bundle_sha256}],
            }

        def get_path_version(self, *, slug: str, version: str):
            return {
                "version": {"version": version, "bundle_sha256": built.bundle_sha256}
            }

        def create_install_intent(self, **kwargs):
            return {
                "intent": {"intent_id": "intent-manual", "version": kwargs["version"]},
                "signature": "signed-intent",
            }

        def download_bundle(self, *, slug: str, version: str, out_path: Path):
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(built.bundle_path.read_bytes())
            return out_path

        def submit_install_receipt(self, **kwargs):
            return {"status": "recorded", "installation_id": "install-manual", "heartbeat_token": "heartbeat-manual"}

    monkeypatch.setattr("wayfinder_paths.paths.cli.PathsApiClient", FakeUpdateClient)

    runner = CliRunner()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    with runner.isolated_filesystem(temp_dir=str(workspace)):
        result = runner.invoke(
            path_cli,
            [
                "update",
                "manual-demo",
                "--dir",
                str(tmp_path / ".wayfinder" / "paths"),
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["result"]["updated"] is True
        assert payload["result"]["activated"] is False
        assert payload["result"]["activation_source"] == "none"
        assert (
            "wayfinder path activate --host <host> --scope <scope>"
            in payload["result"]["manual_activate_command"]
        )


def test_path_update_allows_explicit_version_override(tmp_path: Path, monkeypatch):
    built = _build_path_bundle(tmp_path, slug="override-demo", version="1.0.1")
    _write_paths_lockfile(
        tmp_path,
        {
            "override-demo": {
                "version": "1.0.0",
                "bundle_sha256": "11" * 32,
                "path": str(tmp_path / ".wayfinder" / "paths" / "override-demo" / "1.0.0"),
            }
        },
    )

    class FakeUpdateClient:
        def __init__(self, *, api_base_url=None):
            self.api_base_url = api_base_url

        def get_path(self, *, slug: str):
            return {
                "path": {
                    "slug": slug,
                    "latest_version": "1.2.0",
                    "active_bonded_version": "1.1.0",
                },
                "versions": [{"version": "1.0.1", "bundle_sha256": built.bundle_sha256}],
            }

        def get_path_version(self, *, slug: str, version: str):
            return {
                "version": {"version": version, "bundle_sha256": built.bundle_sha256}
            }

        def create_install_intent(self, **kwargs):
            return {
                "intent": {"intent_id": "intent-override", "version": kwargs["version"]},
                "signature": "signed-intent",
            }

        def download_bundle(self, *, slug: str, version: str, out_path: Path):
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(built.bundle_path.read_bytes())
            return out_path

        def submit_install_receipt(self, **kwargs):
            return {"status": "recorded", "installation_id": "install-override", "heartbeat_token": "heartbeat-override"}

    monkeypatch.setattr("wayfinder_paths.paths.cli.PathsApiClient", FakeUpdateClient)

    result = CliRunner().invoke(
        path_cli,
        [
            "update",
            "override-demo",
            "--version",
            "1.0.1",
            "--dir",
            str(tmp_path / ".wayfinder" / "paths"),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["result"]["target_version"] == "1.0.1"
    assert payload["result"]["install"]["version"] == "1.0.1"
