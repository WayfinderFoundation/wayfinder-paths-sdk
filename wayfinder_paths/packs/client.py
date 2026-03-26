from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from wayfinder_paths.core.config import get_api_key, get_packs_api_base_url
from wayfinder_paths.packs.builder import _sha256_file


class PacksApiError(Exception):
    pass


class PacksApiClient:
    def __init__(
        self,
        *,
        api_base_url: str | None = None,
        client: httpx.Client | None = None,
    ):
        base = (api_base_url or get_packs_api_base_url()).rstrip("/")
        self.base_url = base
        self._client = client or httpx.Client(timeout=httpx.Timeout(60))

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        api_key = get_api_key()
        if api_key:
            headers["X-API-Key"] = api_key
        return headers

    def publish(
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
        url = f"{self.base_url}/api/v1/packs/publish/"
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
            raise PacksApiError(f"Publish failed ({resp.status_code}): {resp.text}")
        return resp.json()

    def create_install_intent(
        self,
        *,
        slug: str,
        version: str,
        runtime: str = "sdk-cli",
        wallet_address: str | None = None,
        install_target: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/packs/{slug}/install-intent/"
        body: dict[str, Any] = {"version": version, "runtime": runtime}
        if wallet_address:
            body["wallet_address"] = wallet_address
        if install_target:
            body["install_target"] = install_target

        resp = self._client.post(url, json=body, headers=self._headers())
        if resp.status_code >= 400:
            raise PacksApiError(
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
        install_path: str | None = None,
        extracted_files: int | None = None,
        workspace_hash: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/packs/{slug}/install-receipt/"
        body: dict[str, Any] = {
            "intent": intent,
            "signature": signature,
            "runtime": runtime,
        }
        if install_path:
            body["install_path"] = install_path
        if extracted_files is not None:
            body["extracted_files"] = extracted_files
        if workspace_hash:
            body["workspace_hash"] = workspace_hash

        resp = self._client.post(url, json=body, headers=self._headers())
        if resp.status_code >= 400:
            raise PacksApiError(
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
        url = f"{self.base_url}/api/v1/packs/installations/{installation_id}/heartbeat/"
        body: dict[str, Any] = {}
        if status:
            body["status"] = status
        if metrics:
            body["metrics"] = metrics

        headers = self._headers()
        headers["X-Heartbeat-Token"] = heartbeat_token

        resp = self._client.post(url, json=body, headers=headers)
        if resp.status_code >= 400:
            raise PacksApiError(
                f"Install heartbeat failed ({resp.status_code}): {resp.text}"
            )
        return resp.json()

    def emit_signal(
        self,
        *,
        slug: str,
        pack_version: str | None,
        title: str,
        message: str | None = None,
        level: str = "info",
        metrics: dict[str, float] | None = None,
        visibility: str = "public",
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/packs/{slug}/events/"
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
        if pack_version:
            payload["pack_version"] = pack_version

        resp = self._client.post(url, json=payload, headers=self._headers())
        if resp.status_code >= 400:
            raise PacksApiError(f"Signal emit failed ({resp.status_code}): {resp.text}")
        return resp.json()

    def emit_event(
        self,
        *,
        slug: str,
        event_type: str,
        pack_version: str | None = None,
        payload: dict[str, Any] | None = None,
        visibility: str = "public",
        stream_key: str = "public",
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/packs/{slug}/events/"
        body: dict[str, Any] = {
            "type": event_type,
            "visibility": visibility,
            "stream_key": stream_key,
            "payload": payload or {},
        }
        if pack_version:
            body["pack_version"] = pack_version

        resp = self._client.post(url, json=body, headers=self._headers())
        if resp.status_code >= 400:
            raise PacksApiError(f"Event emit failed ({resp.status_code}): {resp.text}")
        return resp.json()

    def list_packs(
        self,
        *,
        owner_wallet: str | None = None,
        tag: str | None = None,
        bonded_only: bool = True,
    ) -> list[dict[str, Any]]:
        url = f"{self.base_url}/api/v1/packs/"
        params: dict[str, str] = {}
        if owner_wallet:
            params["owner_wallet"] = owner_wallet
        if tag:
            params["tag"] = tag

        resp = self._client.get(url, params=params, headers=self._headers())
        if resp.status_code >= 400:
            raise PacksApiError(f"List packs failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        packs = data.get("packs", [])
        if not isinstance(packs, list):
            return []
        if bonded_only:
            packs = [pack for pack in packs if self._is_bonded_pack(pack)]
        return packs

    @staticmethod
    def _is_bonded_pack(pack: dict[str, Any]) -> bool:
        trust = pack.get("trust")
        if isinstance(trust, dict):
            tier = str(trust.get("tier") or "").strip().lower()
            if tier:
                return tier == "bonded"

        trust_state = str(pack.get("trust_state") or "").strip().lower()
        if trust_state:
            return trust_state != "unbonded"

        return bool(str(pack.get("active_bonded_version") or "").strip())

    def get_pack(self, *, slug: str) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/packs/{slug}/"
        resp = self._client.get(url, headers=self._headers())
        if resp.status_code >= 400:
            raise PacksApiError(f"Get pack failed ({resp.status_code}): {resp.text}")
        return resp.json()

    def get_pack_version(self, *, slug: str, version: str) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/packs/{slug}/versions/{version}"
        resp = self._client.get(url, headers=self._headers())
        if resp.status_code >= 400:
            raise PacksApiError(
                f"Get pack version failed ({resp.status_code}): {resp.text}"
            )
        return resp.json()

    def download_bundle(
        self,
        *,
        slug: str,
        version: str,
        out_path: Path,
    ) -> Path:
        url = f"{self.base_url}/api/v1/packs/{slug}/versions/{version}/bundle.zip"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with self._client.stream("GET", url, headers=self._headers()) as resp:
            if resp.status_code >= 400:
                raise PacksApiError(
                    f"Download bundle failed ({resp.status_code}): {resp.text}"
                )
            with out_path.open("wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
        return out_path

    @staticmethod
    def sha256_file(path: Path) -> str:
        return _sha256_file(path)

    def fork_pack(
        self,
        *,
        slug: str,
        version: str | None = None,
        new_slug: str | None = None,
        name: str | None = None,
        summary: str | None = None,
        owner_wallet: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/api/v1/packs/{slug}/fork/"
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
            raise PacksApiError(f"Fork failed ({resp.status_code}): {resp.text}")
        return resp.json()
