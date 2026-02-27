"""MCP tools for Solidity contract compilation and deployment."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from wayfinder_paths.core.utils.contracts import (
    deploy_contract as _deploy_contract,
)
from wayfinder_paths.core.utils.solidity import SOLC_VERSION, compile_solidity
from wayfinder_paths.mcp.state.contract_store import ContractArtifactStore
from wayfinder_paths.mcp.state.profile_store import WalletProfileStore
from wayfinder_paths.mcp.utils import (
    err,
    ok,
    resolve_path_inside_repo,
    resolve_wallet_and_signer,
    summarize_abi,
)


def _load_solidity_source(source_path: str) -> tuple[Path, str, str] | dict[str, Any]:
    """Resolve and read a Solidity file inside the repo.

    Returns ``(resolved_path, display_path, source_code)`` or an MCP-style
    ``err(...)`` response dict.
    """
    resolved_path = resolve_path_inside_repo(
        source_path,
        field_name="source_path",
        not_found_message="Solidity source file not found",
    )
    if isinstance(resolved_path, dict):
        return resolved_path
    resolved, display_path = resolved_path

    if resolved.suffix.lower() != ".sol":
        return err(
            "invalid_request",
            "Only .sol files are supported",
            {"source_path": str(resolved)},
        )

    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return err(
            "read_failed",
            f"Failed to read source file: {exc}",
            {"source_path": str(resolved)},
        )

    if not text.strip():
        return err(
            "invalid_source",
            "Source file is empty",
            {"source_path": str(resolved)},
        )

    return resolved, display_path, text


def _annotate_deploy(
    *,
    address: str,
    label: str,
    status: str,
    chain_id: int,
    details: dict[str, Any],
) -> None:
    store = WalletProfileStore.default()
    store.annotate_safe(
        address=address,
        label=label,
        protocol="contracts",
        action="deploy_contract",
        tool="deploy_contract",
        status=status,
        chain_id=chain_id,
        details=details,
    )


async def compile_contract(
    *,
    source_path: str,
    contract_name: str | None = None,
) -> dict[str, Any]:
    """Compile Solidity source code with OpenZeppelin import support.

    Returns ABI and bytecode for each contract found. Installs solc
    and npm dependencies automatically if needed.
    """
    loaded = _load_solidity_source(source_path)
    if isinstance(loaded, dict):
        return loaded
    _resolved, display_path, source_code = loaded

    try:
        artifacts = compile_solidity(
            source_code,
            contract_name=contract_name,
        )
    except Exception as exc:
        return err("compilation_error", str(exc))

    result: dict[str, Any] = {"contracts": {}}
    if display_path:
        result["source_path"] = display_path
    for name, artifact in artifacts.items():
        result["contracts"][name] = {
            "abi": artifact["abi"],
            "bytecode": artifact["bytecode"],
            "abi_summary": summarize_abi(artifact["abi"]),
        }

    if contract_name and contract_name in result["contracts"]:
        result["primary"] = contract_name

    return ok(result)


async def deploy_contract(
    *,
    wallet_label: str,
    source_path: str,
    contract_name: str,
    chain_id: int,
    constructor_args: list[Any] | str | None = None,
    verify: bool = True,
) -> dict[str, Any]:
    """Compile, deploy, and optionally verify a Solidity contract.

    ``constructor_args`` is a JSON-encoded list (e.g. ``'["0xabc...", 1000]'``).
    """
    resolved = resolve_wallet_and_signer(wallet_label)
    if isinstance(resolved, dict):
        return resolved
    sender, _, sign_callback = resolved

    loaded = _load_solidity_source(source_path)
    if isinstance(loaded, dict):
        return loaded
    _resolved, display_path, source_code = loaded

    parsed_args: list[Any] | None = None
    if constructor_args is not None:
        if isinstance(constructor_args, list):
            parsed_args = constructor_args
        else:
            raw = str(constructor_args).strip()
            if not raw:
                parsed_args = None
            else:
                try:
                    parsed_args = json.loads(raw)
                    if not isinstance(parsed_args, list):
                        return err(
                            "invalid_args", "constructor_args must be a JSON array"
                        )
                except json.JSONDecodeError as exc:
                    return err(
                        "invalid_args", f"Invalid JSON in constructor_args: {exc}"
                    )

    try:
        result = await _deploy_contract(
            source_code=source_code,
            contract_name=contract_name,
            constructor_args=parsed_args,
            from_address=sender,
            chain_id=chain_id,
            sign_callback=sign_callback,
            verify=verify,
        )
    except Exception as exc:
        logger.error(f"Contract deployment failed: {exc}")
        return err("deploy_error", str(exc))

    if display_path:
        result["source_path"] = display_path

    _annotate_deploy(
        address=sender,
        label=wallet_label,
        status="confirmed",
        chain_id=chain_id,
        details={
            "source_path": display_path,
            "contract_name": contract_name,
            "contract_address": result.get("contract_address"),
            "tx_hash": result.get("tx_hash"),
            "explorer_url": result.get("explorer_url"),
            "verified": result.get("verified"),
            "verification_error": result.get("verification_error"),
        },
    )

    # Persist deployment artifacts (best-effort)
    store = ContractArtifactStore.default()
    artifact_dir = store.save_safe(
        chain_id=chain_id,
        contract_address=result["contract_address"],
        contract_name=contract_name,
        deployer_address=sender,
        wallet_label=wallet_label,
        tx_hash=result["tx_hash"],
        source_code=source_code,
        abi=result["abi"],
        bytecode=result["bytecode"],
        standard_json_input=result.get("standard_json_input"),
        constructor_args=parsed_args,
        solc_version=SOLC_VERSION,
        source_path_original=display_path,
        verified=result.get("verified"),
        explorer_url=result.get("explorer_url"),
    )
    if artifact_dir:
        result["artifact_dir"] = artifact_dir

    return ok(result)
