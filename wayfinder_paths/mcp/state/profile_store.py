from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from wayfinder_paths.mcp.state.runs import now_iso, runs_root

logger = logging.getLogger(__name__)


class WalletProfileStore:
    SCHEMA_VERSION = "1.0"
    MAX_TRANSACTIONS = 100  # Bound history size per wallet

    def __init__(self, path: Path | None = None):
        if path is None:
            path = runs_root() / "wallet_profiles.json"
        self.path = path

    @staticmethod
    def default() -> WalletProfileStore:
        return WalletProfileStore()

    def _ensure_dir(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": self.SCHEMA_VERSION, "profiles": {}}
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict):
                return {"schema_version": self.SCHEMA_VERSION, "profiles": {}}
            if not isinstance(data.get("profiles"), dict):
                data["profiles"] = {}
            return data
        except Exception as exc:
            logger.warning(f"Failed to load wallet profiles: {exc}")
            return {"schema_version": self.SCHEMA_VERSION, "profiles": {}}

    def _save(self, data: dict[str, Any]) -> None:
        self._ensure_dir()
        data["schema_version"] = self.SCHEMA_VERSION
        self.path.write_text(json.dumps(data, indent=2, sort_keys=False))

    def _normalize_address(self, address: str) -> str:
        return str(address).strip().lower()

    def get_profile(self, address: str) -> dict[str, Any] | None:
        data = self._load()
        norm = self._normalize_address(address)
        profile = data["profiles"].get(norm)
        if profile:
            return {"address": norm, **profile}
        return None

    def list_profiles(self) -> list[dict[str, Any]]:
        data = self._load()
        results: list[dict[str, Any]] = []
        for addr, profile in data.get("profiles", {}).items():
            protocols = list((profile.get("protocols") or {}).keys())
            tx_count = len(profile.get("transactions") or [])
            results.append(
                {
                    "address": addr,
                    "label": profile.get("label"),
                    "protocols": protocols,
                    "protocol_count": len(protocols),
                    "transaction_count": tx_count,
                    "last_activity": profile.get("last_activity"),
                }
            )
        return results

    def get_protocols_for_wallet(self, address: str) -> list[str]:
        profile = self.get_profile(address)
        if not profile:
            return []
        return list((profile.get("protocols") or {}).keys())

    def annotate(
        self,
        *,
        address: str,
        label: str | None = None,
        protocol: str,
        action: str,
        tool: str,
        status: str,
        chain_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        data = self._load()
        norm = self._normalize_address(address)
        now = now_iso()

        if norm not in data["profiles"]:
            data["profiles"][norm] = {
                "label": label,
                "protocols": {},
                "transactions": [],
            }

        profile = data["profiles"][norm]

        if label:
            profile["label"] = label

        if protocol not in profile["protocols"]:
            profile["protocols"][protocol] = {
                "first_seen": now,
                "last_seen": now,
                "interaction_count": 0,
                "chains": [],
            }

        proto_info = profile["protocols"][protocol]
        proto_info["last_seen"] = now
        proto_info["interaction_count"] = proto_info.get("interaction_count", 0) + 1
        if chain_id is not None:
            chains = proto_info.get("chains") or []
            if chain_id not in chains:
                chains.append(chain_id)
                proto_info["chains"] = chains

        tx = {
            "timestamp": now,
            "protocol": protocol,
            "action": action,
            "tool": tool,
            "status": status,
        }
        if chain_id is not None:
            tx["chain_id"] = chain_id
        if details:
            tx["details"] = details

        transactions = profile.get("transactions") or []
        transactions.insert(0, tx)
        if len(transactions) > self.MAX_TRANSACTIONS:
            transactions = transactions[: self.MAX_TRANSACTIONS]
        profile["transactions"] = transactions
        profile["last_activity"] = now
        self._save(data)

    def annotate_safe(
        self,
        *,
        address: str,
        label: str | None = None,
        protocol: str,
        action: str,
        tool: str,
        status: str,
        chain_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        # Best-effort: logs but doesn't raise on failure so annotation doesn't block main operation
        try:
            self.annotate(
                address=address,
                label=label,
                protocol=protocol,
                action=action,
                tool=tool,
                status=status,
                chain_id=chain_id,
                details=details,
            )
        except Exception as exc:
            logger.warning(f"Failed to annotate wallet profile: {exc}")
