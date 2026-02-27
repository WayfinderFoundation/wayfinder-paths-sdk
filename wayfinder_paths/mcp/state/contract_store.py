from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from wayfinder_paths.mcp.state.runs import now_iso, runs_root
from wayfinder_paths.mcp.utils import repo_root, sha256_json

logger = logging.getLogger(__name__)


class ContractArtifactStore:
    """Persist deployment artifacts under .wayfinder_runs/contracts/."""

    def __init__(self, root: Path | None = None):
        if root is None:
            root = runs_root() / "contracts"
        self.root = root

    @staticmethod
    def default() -> ContractArtifactStore:
        return ContractArtifactStore()

    def _index_path(self) -> Path:
        return self.root / "index.json"

    def _artifact_dir(self, chain_id: int, address: str) -> Path:
        return self.root / str(chain_id) / address.lower()

    def _load_index(self) -> list[dict[str, Any]]:
        path = self._index_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                return data
        except Exception as exc:
            logger.warning(f"Failed to load contract index: {exc}")
        return []

    def _save_index(self, entries: list[dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_path().write_text(json.dumps(entries, indent=2))

    def save(
        self,
        *,
        chain_id: int,
        contract_address: str,
        contract_name: str,
        deployer_address: str,
        wallet_label: str,
        tx_hash: str,
        source_code: str,
        abi: list[dict[str, Any]],
        bytecode: str,
        standard_json_input: dict[str, Any] | None = None,
        constructor_args: list[Any] | None = None,
        solc_version: str | None = None,
        source_path_original: str | None = None,
        verified: bool | None = None,
        explorer_url: str | None = None,
    ) -> str:
        """Persist all deployment artifacts. Returns the artifact directory path."""
        addr = contract_address.lower()
        artifact_dir = self._artifact_dir(chain_id, addr)
        artifact_dir.mkdir(parents=True, exist_ok=True)

        now = now_iso()
        bytecode_hex = bytecode[2:] if bytecode.startswith("0x") else bytecode

        (artifact_dir / "source.sol").write_text(source_code, encoding="utf-8")
        (artifact_dir / "abi.json").write_text(
            json.dumps(abi, indent=2), encoding="utf-8"
        )

        if standard_json_input is not None:
            (artifact_dir / "standard_input.json").write_text(
                json.dumps(standard_json_input, indent=2), encoding="utf-8"
            )

        metadata: dict[str, Any] = {
            "chain_id": chain_id,
            "contract_address": addr,
            "contract_name": contract_name,
            "deployer_address": deployer_address.lower(),
            "wallet_label": wallet_label,
            "tx_hash": tx_hash,
            "deployed_at": now,
            "abi_sha256": sha256_json(abi),
            "bytecode_size": len(bytecode_hex) // 2,
        }
        if constructor_args is not None:
            metadata["constructor_args"] = constructor_args
        if solc_version:
            metadata["solc_version"] = solc_version
        if source_path_original:
            metadata["source_path_original"] = source_path_original
        if verified is not None:
            metadata["verified"] = verified
        if explorer_url:
            metadata["explorer_url"] = explorer_url

        (artifact_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )

        index = self._load_index()
        index = [
            e
            for e in index
            if not (e.get("chain_id") == chain_id and e.get("contract_address") == addr)
        ]
        rel_dir = str(artifact_dir)
        try:
            rel_dir = str(artifact_dir.relative_to(repo_root()))
        except ValueError:
            pass

        index.insert(
            0,
            {
                "contract_name": contract_name,
                "contract_address": addr,
                "chain_id": chain_id,
                "deployer": deployer_address.lower(),
                "wallet_label": wallet_label,
                "tx_hash": tx_hash,
                "deployed_at": now,
                "verified": verified,
                "artifact_dir": rel_dir,
            },
        )
        self._save_index(index)

        return str(artifact_dir)

    def save_safe(self, **kwargs: Any) -> str | None:
        """Best-effort save — logs but never raises."""
        try:
            return self.save(**kwargs)
        except Exception as exc:
            logger.warning(f"Failed to persist contract artifacts: {exc}")
            return None

    def list_deployments(self) -> list[dict[str, Any]]:
        return self._load_index()

    def get_metadata(self, chain_id: int, address: str) -> dict[str, Any] | None:
        path = self._artifact_dir(chain_id, address) / "metadata.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except Exception as exc:
            logger.warning(f"Failed to load contract metadata: {exc}")
            return None

    def get_abi(self, chain_id: int, address: str) -> list[dict[str, Any]] | None:
        path = self._artifact_dir(chain_id, address) / "abi.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                return data
        except Exception as exc:
            logger.warning(f"Failed to load contract ABI: {exc}")
        return None

    def get_abi_path(self, chain_id: int, address: str) -> Path | None:
        """Return the path to abi.json if it exists, for use as abi_path."""
        path = self._artifact_dir(chain_id, address) / "abi.json"
        return path if path.exists() else None
