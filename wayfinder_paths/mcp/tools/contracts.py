"""MCP tools for Solidity contract compilation and deployment."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from wayfinder_paths.core.utils.contracts import (
    deploy_contract as _deploy_contract,
)
from wayfinder_paths.core.utils.solidity import compile_solidity
from wayfinder_paths.mcp.scripting import _make_sign_callback
from wayfinder_paths.mcp.state.profile_store import WalletProfileStore
from wayfinder_paths.mcp.utils import (
    err,
    find_wallet_by_label,
    normalize_address,
    ok,
    repo_root,
)


def _load_solidity_source(source_path: str) -> tuple[Path, str, str] | dict[str, Any]:
    """Resolve and read a Solidity file inside the repo.

    Returns ``(resolved_path, display_path, source_code)`` or an MCP-style
    ``err(...)`` response dict.
    """
    raw = str(source_path).strip()
    if not raw:
        return err("invalid_request", "source_path is required")

    root = repo_root()
    root_resolved = root.resolve(strict=False)

    p = Path(raw)
    if not p.is_absolute():
        p = root / p
    resolved = p.resolve(strict=False)

    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return err(
            "invalid_request",
            "source_path must be inside the repository",
            {"repo_root": str(root_resolved), "source_path": str(resolved)},
        )

    if not resolved.exists():
        return err(
            "not_found",
            "Solidity source file not found",
            {"source_path": str(resolved)},
        )

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

    display_path = str(resolved)
    try:
        display_path = str(resolved.relative_to(root_resolved))
    except ValueError:
        pass

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
            "abi_summary": _abi_summary(artifact["abi"]),
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
    escape_hatch: bool = True,
) -> dict[str, Any]:
    """Compile, deploy, and optionally verify a Solidity contract.

    ``constructor_args`` is a JSON-encoded list (e.g. ``'["0xabc...", 1000]'``).
    """
    w = find_wallet_by_label(wallet_label)
    if not w:
        return err("not_found", f"Unknown wallet_label: {wallet_label}")

    sender = normalize_address(w.get("address"))
    pk = (w.get("private_key") or w.get("private_key_hex")) if isinstance(w, dict) else None
    if not sender or not pk:
        return err(
            "invalid_wallet",
            "Wallet must include address and private_key_hex in config.json",
        )

    sign_callback = _make_sign_callback(pk)

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
                        return err("invalid_args", "constructor_args must be a JSON array")
                except json.JSONDecodeError as exc:
                    return err("invalid_args", f"Invalid JSON in constructor_args: {exc}")

    try:
        result = await _deploy_contract(
            source_code=source_code,
            contract_name=contract_name,
            constructor_args=parsed_args,
            from_address=sender,
            chain_id=chain_id,
            sign_callback=sign_callback,
            verify=verify,
            escape_hatch=escape_hatch,
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
            "escape_hatch": bool(escape_hatch),
        },
    )
    return ok(result)


def _abi_summary(abi: list[dict[str, Any]]) -> list[str]:
    """Produce a concise summary of ABI entries for display."""
    entries: list[str] = []
    for item in abi:
        kind = item.get("type", "")
        name = item.get("name", "")
        if kind == "function":
            inputs = ", ".join(i.get("type", "?") for i in item.get("inputs", []))
            outputs = ", ".join(o.get("type", "?") for o in item.get("outputs", []))
            entries.append(f"{name}({inputs}) -> ({outputs})")
        elif kind == "event":
            inputs = ", ".join(i.get("type", "?") for i in item.get("inputs", []))
            entries.append(f"event {name}({inputs})")
        elif kind == "constructor":
            inputs = ", ".join(i.get("type", "?") for i in item.get("inputs", []))
            entries.append(f"constructor({inputs})")
    return entries
