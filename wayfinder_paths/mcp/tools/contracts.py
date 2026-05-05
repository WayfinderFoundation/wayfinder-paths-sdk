"""MCP tools for Solidity contract compilation, deployment, and artifact lookup."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from wayfinder_paths.core.utils.contracts import (
    deploy_contract as _deploy_contract,
)
from wayfinder_paths.core.utils.etherscan import fetch_contract_abi
from wayfinder_paths.core.utils.proxy import resolve_proxy_implementation
from wayfinder_paths.core.utils.solidity import SOLC_VERSION, compile_solidity
from wayfinder_paths.core.utils.wallets import get_wallet_signing_callback
from wayfinder_paths.mcp.state.contract_store import ContractArtifactStore
from wayfinder_paths.mcp.state.profile_store import WalletProfileStore
from wayfinder_paths.mcp.utils import (
    err,
    ok,
    resolve_path_inside_repo,
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


async def contracts_compile(
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


async def contracts_deploy(
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
    try:
        sign_callback, sender = await get_wallet_signing_callback(wallet_label)
    except ValueError as e:
        return err("invalid_wallet", str(e))

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


async def contracts_list() -> str:
    """List all locally-deployed contracts from the artifact store."""
    store = ContractArtifactStore.default()
    entries = store.list_deployments()
    return json.dumps({"contracts": entries, "count": len(entries)}, indent=2)


async def contracts_get(
    chain_id: str | int,
    address: str,
    *,
    resolve_proxy: bool = True,
) -> str:
    """Get ABI + metadata for a deployed contract.

    Resolution order:
      1. Local artifact store (contracts deployed via `contracts_deploy`) — returns full
         deployment metadata + ABI.
      2. Etherscan V2 fetch — returns ABI only. If `resolve_proxy` is true and the address
         is a proxy (EIP-1967 / ZeppelinOS / EIP-897), fetches the implementation's ABI.
    """
    store = ContractArtifactStore.default()
    cid = int(chain_id)
    addr = str(address).strip()
    addr_lc = addr.lower()
    if not addr:
        return json.dumps({"error": "address is required"})

    metadata = store.get_metadata(cid, addr_lc)
    if metadata:
        result: dict[str, Any] = {"source": "local_artifacts", "metadata": metadata}
        local_abi = store.get_abi(cid, addr_lc)
        if local_abi is not None:
            result["abi"] = local_abi
        abi_path = store.get_abi_path(cid, addr_lc)
        if abi_path is not None:
            result["abi_path"] = str(abi_path)
        return json.dumps(result, indent=2)

    impl: str | None = None
    flavour: str | None = None
    if resolve_proxy:
        try:
            impl, flavour = await resolve_proxy_implementation(cid, addr)
        except Exception:
            impl, flavour = None, None

        if impl:
            try:
                impl_abi = await fetch_contract_abi(cid, impl)
                return json.dumps(
                    {
                        "source": "etherscan_v2_proxy",
                        "chain_id": cid,
                        "contract_address": addr,
                        "proxy_address": addr,
                        "implementation_address": impl,
                        "proxy_flavour": flavour,
                        "abi": impl_abi,
                        "abi_summary": summarize_abi(impl_abi),
                    },
                    indent=2,
                )
            except Exception:
                pass

    try:
        abi_list = await fetch_contract_abi(cid, addr)
    except Exception as exc:
        return json.dumps({"error": f"ABI not found locally or on Etherscan: {exc}"})

    return json.dumps(
        {
            "source": "etherscan_v2",
            "chain_id": cid,
            "contract_address": addr,
            "abi": abi_list,
            "abi_summary": summarize_abi(abi_list),
        },
        indent=2,
    )
