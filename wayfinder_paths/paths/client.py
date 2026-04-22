from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx

from wayfinder_paths.core.config import get_api_key, get_paths_api_base_url
from wayfinder_paths.paths.builder import _sha256_file


class PathsApiError(Exception):
    pass


class PathsApiClient:
    def __init__(
        self,
        *,
        api_base_url: str | None = None,
        client: httpx.Client | None = None,
    ):
        base = (api_base_url or get_paths_api_base_url()).rstrip("/")
        self.base_url = base
        self._client = client or httpx.Client(timeout=httpx.Timeout(60))

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        api_key = get_api_key()
        if api_key:
            headers["X-API-Key"] = api_key
        return headers

    @staticmethod
    def _sha256_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _publish_legacy(
        self,
        *,
        bundle_path: Path,
        source_path: Path | None = None,
        exports_manifest: dict[str, Any] | None = None,
        skill_exports: dict[str, bytes] | None = None,
        owner_wallet: str | None = None,
        bonded: bool = False,
        risk_tier: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/paths/publish/"
        data: dict[str, str] = {}
        if owner_wallet:
            data["owner_wallet"] = owner_wallet
        if bonded:
            data["bonded"] = "true"
        if risk_tier:
            data["risk_tier"] = risk_tier

        files: dict[str, tuple[str, bytes, str]] = {
            "bundle": ("bundle.zip", bundle_path.read_bytes(), "application/zip")
        }
        if source_path:
            files["source"] = (
                "source.zip",
                source_path.read_bytes(),
                "application/zip",
            )
        if exports_manifest:
            files["exports_manifest"] = (
                "exports_manifest.json",
                json.dumps(exports_manifest).encode("utf-8"),
                "application/json",
            )
        if skill_exports:
            for target, export_bytes in skill_exports.items():
                files[f"skill-{target}"] = (
                    f"skill-{target}.zip",
                    export_bytes,
                    "application/zip",
                )

        resp = self._client.post(url, data=data, files=files, headers=self._headers())
        if resp.status_code >= 400:
            raise PathsApiError(f"Publish failed ({resp.status_code}): {resp.text}")
        return resp.json()

    def publish_init(
        self,
        *,
        bundle_path: Path,
        source_path: Path,
        manifest: dict[str, Any],
        applet_meta: dict[str, Any] | None = None,
        has_skill: bool = False,
        exports_manifest: dict[str, Any] | None = None,
        skill_exports: dict[str, bytes] | None = None,
        owner_wallet: str | None = None,
        bonded: bool = False,
        risk_tier: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/paths/publish/init/"
        bundle_size = bundle_path.stat().st_size
        source_size = source_path.stat().st_size
        payload: dict[str, Any] = {
            "manifest": manifest,
            "applet_meta": applet_meta or {},
            "has_skill": bool(has_skill),
            "bundle_sha256": _sha256_file(bundle_path),
            "bundle_size": bundle_size,
            "source_sha256": _sha256_file(source_path),
            "source_size": source_size,
        }
        if owner_wallet:
            payload["owner_wallet"] = owner_wallet
        if bonded:
            payload["bonded"] = True
        if risk_tier:
            payload["risk_tier"] = risk_tier

        if exports_manifest and skill_exports:
            payload["doctor"] = exports_manifest.get("doctor") or {
                "status": "ok",
                "warnings": [],
            }
            exports_payload: dict[str, Any] = {}
            for target, export_bytes in skill_exports.items():
                info = (
                    exports_manifest.get("exports", {}).get(target, {})
                    if isinstance(exports_manifest.get("exports"), dict)
                    else {}
                )
                exports_payload[target] = {
                    "filename": info.get("filename") or f"skill-{target}-thin.zip",
                    "mode": info.get("mode") or "thin",
                    "runtime": info.get("runtime") or {},
                    "export": info.get("export") or {},
                    "warnings": list(info.get("warnings") or []),
                    "size": len(export_bytes),
                    "sha256": self._sha256_bytes(export_bytes),
                }
            payload["skill_exports"] = exports_payload

        resp = self._client.post(url, json=payload, headers=self._headers())
        if resp.status_code in {404, 405}:
            raise PathsApiError(f"Publish init unavailable ({resp.status_code})")
        if resp.status_code >= 400:
            raise PathsApiError(
                f"Publish init failed ({resp.status_code}): {resp.text}"
            )
        return resp.json()

    def publish_finalize(
        self, *, upload_id: str, finalize_token: str
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/paths/publish/finalize/"
        resp = self._client.post(
            url,
            json={"upload_id": upload_id, "finalize_token": finalize_token},
            headers=self._headers(),
        )
        if resp.status_code >= 400:
            raise PathsApiError(
                f"Publish finalize failed ({resp.status_code}): {resp.text}"
            )
        return resp.json()

    def _upload_signed_artifact(
        self,
        *,
        upload_url: str,
        headers: dict[str, str] | None,
        data: bytes,
    ) -> None:
        resp = self._client.put(
            upload_url,
            content=data,
            headers=headers or {},
        )
        if resp.status_code >= 400:
            raise PathsApiError(
                f"Artifact upload failed ({resp.status_code}): {resp.text}"
            )

    def publish(
        self,
        *,
        bundle_path: Path,
        source_path: Path | None = None,
        exports_manifest: dict[str, Any] | None = None,
        skill_exports: dict[str, bytes] | None = None,
        manifest: dict[str, Any] | None = None,
        applet_meta: dict[str, Any] | None = None,
        has_skill: bool = False,
        owner_wallet: str | None = None,
        bonded: bool = False,
        risk_tier: str | None = None,
    ) -> dict[str, Any]:
        if source_path is None or manifest is None:
            return self._publish_legacy(
                bundle_path=bundle_path,
                source_path=source_path,
                exports_manifest=exports_manifest,
                skill_exports=skill_exports,
                owner_wallet=owner_wallet,
                bonded=bonded,
                risk_tier=risk_tier,
            )

        try:
            init = self.publish_init(
                bundle_path=bundle_path,
                source_path=source_path,
                manifest=manifest,
                applet_meta=applet_meta or {},
                has_skill=has_skill,
                exports_manifest=exports_manifest,
                skill_exports=skill_exports,
                owner_wallet=owner_wallet,
                bonded=bonded,
                risk_tier=risk_tier,
            )
        except PathsApiError as exc:
            if "unavailable (404)" in str(exc) or "unavailable (405)" in str(exc):
                return self._publish_legacy(
                    bundle_path=bundle_path,
                    source_path=source_path,
                    exports_manifest=exports_manifest,
                    skill_exports=skill_exports,
                    owner_wallet=owner_wallet,
                    bonded=bonded,
                    risk_tier=risk_tier,
                )
            raise

        artifacts = (
            init.get("artifacts") if isinstance(init.get("artifacts"), dict) else {}
        )
        bundle_artifact = (
            artifacts.get("bundle") if isinstance(artifacts.get("bundle"), dict) else {}
        )
        source_artifact = (
            artifacts.get("source") if isinstance(artifacts.get("source"), dict) else {}
        )
        bundle_upload_url = str(bundle_artifact.get("uploadUrl") or "").strip()
        source_upload_url = str(source_artifact.get("uploadUrl") or "").strip()
        if not bundle_upload_url or not source_upload_url:
            raise PathsApiError(
                "Publish init response is missing bundle/source upload URLs"
            )

        self._upload_signed_artifact(
            upload_url=bundle_upload_url,
            headers=bundle_artifact.get("headers")
            if isinstance(bundle_artifact.get("headers"), dict)
            else {},
            data=bundle_path.read_bytes(),
        )
        self._upload_signed_artifact(
            upload_url=source_upload_url,
            headers=source_artifact.get("headers")
            if isinstance(source_artifact.get("headers"), dict)
            else {},
            data=source_path.read_bytes(),
        )
        for target, export_bytes in (skill_exports or {}).items():
            export_artifact = (
                artifacts.get("skillExports", {}).get(target, {})
                if isinstance(artifacts.get("skillExports"), dict)
                else {}
            )
            upload_url = str(export_artifact.get("uploadUrl") or "").strip()
            if not upload_url:
                raise PathsApiError(
                    f"Publish init response is missing upload URL for skill export '{target}'"
                )
            self._upload_signed_artifact(
                upload_url=upload_url,
                headers=export_artifact.get("headers")
                if isinstance(export_artifact.get("headers"), dict)
                else {},
                data=export_bytes,
            )

        upload_id = str(init.get("uploadId") or "").strip()
        finalize_token = str(init.get("finalizeToken") or "").strip()
        if not upload_id or not finalize_token:
            raise PathsApiError(
                "Publish init response is missing upload session details"
            )
        return self.publish_finalize(upload_id=upload_id, finalize_token=finalize_token)

    def create_install_intent(
        self,
        *,
        slug: str,
        version: str,
        runtime: str = "sdk-cli",
        venue: str | None = None,
        wallet_address: str | None = None,
        install_target: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/paths/{slug}/install-intent/"
        body: dict[str, Any] = {"version": version, "runtime": runtime}
        if venue:
            body["venue"] = venue
        if wallet_address:
            body["wallet_address"] = wallet_address
        if install_target:
            body["install_target"] = install_target

        resp = self._client.post(url, json=body, headers=self._headers())
        if resp.status_code >= 400:
            raise PathsApiError(
                f"Create install intent failed ({resp.status_code}): {resp.text}"
            )
        return resp.json()

    def submit_install_receipt(
        self,
        *,
        slug: str,
        intent: dict[str, Any],
        signature: str,
        runtime: str,
        venue: str | None = None,
        install_path: str | None = None,
        extracted_files: int | None = None,
        workspace_hash: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/paths/{slug}/install-receipt/"
        body: dict[str, Any] = {
            "intent": intent,
            "signature": signature,
            "runtime": runtime,
        }
        if venue:
            body["venue"] = venue
        if install_path:
            body["install_path"] = install_path
        if extracted_files is not None:
            body["extracted_files"] = extracted_files
        if workspace_hash:
            body["workspace_hash"] = workspace_hash

        resp = self._client.post(url, json=body, headers=self._headers())
        if resp.status_code >= 400:
            raise PathsApiError(
                f"Install receipt failed ({resp.status_code}): {resp.text}"
            )
        return resp.json()

    def submit_install_heartbeat(
        self,
        *,
        installation_id: str,
        heartbeat_token: str,
        status: str | None = None,
        metrics: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/paths/installations/{installation_id}/heartbeat/"
        body: dict[str, Any] = {}
        if status:
            body["status"] = status
        if metrics:
            body["metrics"] = metrics

        headers = self._headers()
        headers["X-Heartbeat-Token"] = heartbeat_token

        resp = self._client.post(url, json=body, headers=headers)
        if resp.status_code >= 400:
            raise PathsApiError(
                f"Install heartbeat failed ({resp.status_code}): {resp.text}"
            )
        return resp.json()

    def submit_batch_install_heartbeats(
        self,
        *,
        heartbeats: list[dict[str, Any]],
        source: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/paths/installations/heartbeat-batch/"
        body: dict[str, Any] = {"heartbeats": heartbeats}
        if source:
            body["source"] = source

        resp = self._client.post(url, json=body, headers=self._headers())
        if resp.status_code >= 400:
            raise PathsApiError(
                f"Batch install heartbeat failed ({resp.status_code}): {resp.text}"
            )
        return resp.json()

    def emit_signal(
        self,
        *,
        slug: str,
        path_version: str | None,
        title: str,
        message: str | None = None,
        level: str = "info",
        metrics: dict[str, float] | None = None,
        visibility: str = "public",
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/paths/{slug}/events/"
        payload: dict[str, Any] = {
            "type": "signal",
            "visibility": visibility,
            "payload": {
                "title": title,
                "message": message or "",
                "level": level,
                "metrics": metrics or {},
            },
        }
        if path_version:
            payload["path_version"] = path_version

        resp = self._client.post(url, json=payload, headers=self._headers())
        if resp.status_code >= 400:
            raise PathsApiError(f"Signal emit failed ({resp.status_code}): {resp.text}")
        return resp.json()

    def emit_event(
        self,
        *,
        slug: str,
        event_type: str,
        path_version: str | None = None,
        payload: dict[str, Any] | None = None,
        visibility: str = "public",
        stream_key: str = "public",
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/paths/{slug}/events/"
        body: dict[str, Any] = {
            "type": event_type,
            "visibility": visibility,
            "stream_key": stream_key,
            "payload": payload or {},
        }
        if path_version:
            body["path_version"] = path_version

        resp = self._client.post(url, json=body, headers=self._headers())
        if resp.status_code >= 400:
            raise PathsApiError(f"Event emit failed ({resp.status_code}): {resp.text}")
        return resp.json()

    def list_paths(
        self,
        *,
        owner_wallet: str | None = None,
        tag: str | None = None,
        bonded_only: bool = True,
    ) -> list[dict[str, Any]]:
        url = f"{self.base_url}/api/v1/paths/"
        params: dict[str, str] = {}
        if owner_wallet:
            params["owner_wallet"] = owner_wallet
        if tag:
            params["tag"] = tag

        resp = self._client.get(url, params=params, headers=self._headers())
        if resp.status_code >= 400:
            raise PathsApiError(f"List paths failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        paths = data.get("paths", [])
        if not isinstance(paths, list):
            return []
        if bonded_only:
            paths = [path for path in paths if self._is_bonded_path(path)]
        return paths

    @staticmethod
    def _is_bonded_path(path: dict[str, Any]) -> bool:
        trust = path.get("trust")
        if isinstance(trust, dict):
            tier = str(trust.get("tier") or "").strip().lower()
            if tier:
                return tier == "bonded"

        trust_state = str(path.get("trust_state") or "").strip().lower()
        if trust_state:
            return trust_state != "unbonded"

        return bool(str(path.get("active_bonded_version") or "").strip())

    def get_path(self, *, slug: str) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/paths/{slug}/"
        resp = self._client.get(url, headers=self._headers())
        if resp.status_code >= 400:
            raise PathsApiError(f"Get path failed ({resp.status_code}): {resp.text}")
        return resp.json()

    def get_path_version(self, *, slug: str, version: str) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/paths/{slug}/versions/{version}"
        resp = self._client.get(url, headers=self._headers())
        if resp.status_code >= 400:
            raise PathsApiError(
                f"Get path version failed ({resp.status_code}): {resp.text}"
            )
        return resp.json()

    def download_bundle(
        self,
        *,
        slug: str,
        version: str,
        out_path: Path,
    ) -> Path:
        url = f"{self.base_url}/api/v1/paths/{slug}/versions/{version}/bundle.zip"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with self._client.stream("GET", url, headers=self._headers()) as resp:
            if resp.status_code >= 400:
                raise PathsApiError(
                    f"Download bundle failed ({resp.status_code}): {resp.text}"
                )
            with out_path.open("wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
        return out_path

    @staticmethod
    def sha256_file(path: Path) -> str:
        return _sha256_file(path)

    def fork_path(
        self,
        *,
        slug: str,
        version: str | None = None,
        new_slug: str | None = None,
        name: str | None = None,
        summary: str | None = None,
        owner_wallet: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/paths/{slug}/fork/"
        body: dict[str, Any] = {}
        if version:
            body["version"] = version
        if new_slug:
            body["slug"] = new_slug
        if name:
            body["name"] = name
        if summary:
            body["summary"] = summary
        if owner_wallet:
            body["owner_wallet"] = owner_wallet

        resp = self._client.post(url, json=body, headers=self._headers())
        if resp.status_code >= 400:
            raise PathsApiError(f"Fork failed ({resp.status_code}): {resp.text}")
        return resp.json()
