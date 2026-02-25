from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger
from web3 import AsyncWeb3

from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.core.utils.abi_caster import cast_args
from wayfinder_paths.core.utils.etherscan import (
    fetch_contract_abi,
    get_etherscan_transaction_link,
)
from wayfinder_paths.core.utils.proxy import resolve_proxy_implementation
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.wallets import make_sign_callback
from wayfinder_paths.mcp.state.contract_store import ContractArtifactStore
from wayfinder_paths.mcp.state.profile_store import WalletProfileStore
from wayfinder_paths.mcp.utils import (
    abi_function_signature,
    err,
    find_wallet_by_label,
    normalize_address,
    ok,
    resolve_path_inside_repo,
    resolve_wallet_address,
    sanitize_for_json,
    sha256_json,
    summarize_abi,
)


def _safe_checksum_address(addr: str) -> str:
    try:
        return AsyncWeb3.to_checksum_address(addr)
    except Exception:
        return str(addr).strip()


def _abi_error_code(message: str) -> str:
    return (
        "missing_api_key" if "api key" in str(message).lower() else "abi_fetch_failed"
    )


def _normalize_signature(sig: str) -> str:
    # Strip whitespace to tolerate "fn( uint256 , address )" input.
    return "".join(str(sig).split())


def _select_function_abi(
    abi: list[dict[str, Any]],
    *,
    function_name: str | None,
    function_signature: str | None,
) -> tuple[dict[str, Any], str] | dict[str, Any]:
    fns = [
        item
        for item in abi
        if isinstance(item, dict)
        and item.get("type") == "function"
        and item.get("name")
    ]

    if function_signature:
        want = _normalize_signature(function_signature)
        matches = [fn for fn in fns if abi_function_signature(fn) == want]
        if len(matches) != 1:
            candidates = sorted({abi_function_signature(fn) for fn in fns})
            return err(
                "not_found",
                f"Function signature '{want}' not found in ABI.",
                {"candidates": candidates},
            )
        return matches[0], want

    name = str(function_name or "").strip()
    if not name:
        return err(
            "invalid_request",
            "function_name or function_signature is required (use contract_get_abi to fetch ABI).",
        )

    matches = [fn for fn in fns if str(fn.get("name") or "").strip() == name]
    if not matches:
        available = sorted(
            {str(fn.get("name") or "").strip() for fn in fns if fn.get("name")}
        )
        return err(
            "not_found",
            f"Function '{name}' not found in ABI.",
            {"available_functions": available},
        )
    if len(matches) > 1:
        candidates = sorted({abi_function_signature(fn) for fn in matches})
        return err(
            "ambiguous_function",
            f"Function '{name}' is overloaded; provide function_signature.",
            {"candidates": candidates},
        )
    fn_abi = matches[0]
    return fn_abi, abi_function_signature(fn_abi)


def _parse_json_list(raw: str, *, field_name: str) -> list[Any] | dict[str, Any]:
    s = str(raw).strip()
    if not s:
        return []
    try:
        # LLMs sometimes emit single quotes; tolerate it for simple lists.
        s = s.replace("'", '"')
        val = json.loads(s)
    except json.JSONDecodeError as exc:
        return err("invalid_request", f"Invalid JSON in {field_name}: {exc}")
    if not isinstance(val, list):
        return err("invalid_request", f"{field_name} must be a JSON array")
    return val


def _parse_args(args: list[Any] | str | None) -> list[Any] | dict[str, Any]:
    if args is None:
        return []
    if isinstance(args, list):
        return args
    if isinstance(args, str):
        return _parse_json_list(args, field_name="args")
    return err("invalid_request", "args must be a list or JSON array string")


def _parse_value_wei(value_wei: Any) -> int | dict[str, Any]:
    try:
        value_i = int(value_wei or 0)
    except (TypeError, ValueError):
        return err("invalid_request", "value_wei must be an integer")
    if value_i < 0:
        return err("invalid_request", "value_wei must be >= 0")
    return value_i


def _load_json_inside_repo(path_raw: str) -> tuple[Path, str, Any] | dict[str, Any]:
    resolved_path = resolve_path_inside_repo(
        path_raw,
        field_name="abi_path",
        not_found_message="ABI file not found",
    )
    if isinstance(resolved_path, dict):
        return resolved_path
    resolved, display_path = resolved_path

    if resolved.suffix.lower() != ".json":
        return err(
            "invalid_request",
            "Only .json ABI files are supported",
            {"abi_path": str(resolved)},
        )

    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return err(
            "read_failed",
            f"Failed to read ABI file: {exc}",
            {"abi_path": str(resolved)},
        )

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        return err(
            "invalid_abi",
            f"ABI JSON parse failed: {exc}",
            {"abi_path": str(resolved)},
        )

    return resolved, display_path, obj


def _extract_abi(obj: Any) -> list[dict[str, Any]] | dict[str, Any]:
    # Accept either a bare ABI array or a dict containing {"abi": [...]}.
    if isinstance(obj, list):
        return [i for i in obj if isinstance(i, dict)]
    if isinstance(obj, dict) and isinstance(obj.get("abi"), list):
        return [i for i in obj["abi"] if isinstance(i, dict)]
    return err(
        "invalid_abi", "ABI must be a JSON array, or a JSON object with an 'abi' array"
    )


def _load_abi(
    *,
    abi: list[dict[str, Any]] | str | None,
    abi_path: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None] | dict[str, Any]:
    if abi is not None and abi_path:
        return err("invalid_request", "Provide only one of abi or abi_path")
    if abi is None and not abi_path:
        return err("invalid_request", "abi or abi_path is required")

    meta: dict[str, Any] = {}

    if abi_path:
        loaded = _load_json_inside_repo(abi_path)
        if isinstance(loaded, dict):
            return loaded
        _resolved, display_path, obj = loaded
        extracted = _extract_abi(obj)
        if isinstance(extracted, dict):
            return extracted
        meta["abi_path"] = display_path
        meta["abi_sha256"] = sha256_json(extracted)
        return extracted, meta

    if isinstance(abi, str):
        s = str(abi).strip()
        if not s:
            return err("invalid_abi", "abi must be a JSON array or object")
        try:
            # LLMs sometimes emit single quotes; tolerate it for simple ABIs.
            obj = json.loads(s.replace("'", '"'))
        except json.JSONDecodeError as exc:
            return err("invalid_abi", f"ABI JSON parse failed: {exc}")

        extracted = _extract_abi(obj)
        if isinstance(extracted, dict):
            return extracted
        meta["abi_sha256"] = sha256_json(extracted)
        return extracted, meta

    if isinstance(abi, list):
        extracted = _extract_abi(abi)
        if isinstance(extracted, dict):
            return extracted
        meta["abi_sha256"] = sha256_json(extracted)
        return extracted, meta

    return err("invalid_abi", "abi must be a list of ABI entries, or a JSON string")


async def _resolve_abi(
    *,
    chain_id: int,
    contract_address: str,
    abi: list[dict[str, Any]] | str | None,
    abi_path: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None] | dict[str, Any]:
    """Resolve ABI from inline input, repo file, local artifacts, or Etherscan V2 (fallback)."""
    if abi is None and not abi_path:
        # Check local artifact store first (deployed via deploy_contract)
        local_store = ContractArtifactStore.default()
        local_abi = local_store.get_abi(int(chain_id), contract_address)
        if local_abi:
            meta = {
                "abi_source": "local_artifacts",
                "abi_sha256": sha256_json(local_abi),
                "abi_address": _safe_checksum_address(contract_address),
            }
            return local_abi, meta

        try:
            fetched = await fetch_contract_abi(int(chain_id), contract_address)
        except ValueError as exc:
            msg = str(exc)
            code = _abi_error_code(msg)
            if code == "missing_api_key":
                return err(code, msg)

            upgraded = await _try_fetch_proxy_implementation_abi(
                chain_id=int(chain_id),
                contract_address=contract_address,
            )
            if isinstance(upgraded, tuple):
                return upgraded
            return err(code, msg)
        except Exception as exc:
            return err("abi_fetch_failed", str(exc))

        meta = {
            "abi_source": "etherscan_v2",
            "abi_sha256": sha256_json(fetched),
            "abi_address": _safe_checksum_address(contract_address),
        }
        return fetched, meta

    return _load_abi(abi=abi, abi_path=abi_path)


async def _try_fetch_proxy_implementation_abi(
    *, chain_id: int, contract_address: str
) -> tuple[list[dict[str, Any]], dict[str, Any]] | dict[str, Any] | None:
    impl, flavour = await resolve_proxy_implementation(int(chain_id), contract_address)
    if not impl:
        return None

    try:
        fetched = await fetch_contract_abi(int(chain_id), impl)
    except ValueError as exc:
        msg = str(exc)
        code = _abi_error_code(msg)
        return err(code, msg)
    except Exception as exc:
        return err("abi_fetch_failed", str(exc))

    meta = {
        "abi_source": "etherscan_v2_proxy",
        "proxy_address": _safe_checksum_address(contract_address),
        "implementation_address": impl,
        "proxy_flavour": flavour,
        "abi_sha256": sha256_json(fetched),
        "abi_address": impl,
    }
    return fetched, meta


async def _pick_function_abi(
    *,
    chain_id: int,
    contract_address: str,
    abi_list: list[dict[str, Any]],
    abi_meta: dict[str, Any] | None,
    function_name: str | None,
    function_signature: str | None,
) -> (
    tuple[dict[str, Any], str, list[dict[str, Any]], dict[str, Any] | None]
    | dict[str, Any]
):
    picked = _select_function_abi(
        abi_list,
        function_name=function_name,
        function_signature=function_signature,
    )
    if not isinstance(picked, dict):
        fn_abi, signature = picked
        return fn_abi, signature, abi_list, abi_meta

    if (
        abi_meta
        and abi_meta.get("abi_source") == "etherscan_v2"
        and picked.get("error", {}).get("code") == "not_found"
    ):
        upgraded = await _try_fetch_proxy_implementation_abi(
            chain_id=int(chain_id),
            contract_address=contract_address,
        )
        if isinstance(upgraded, dict):
            return upgraded
        if upgraded is not None:
            upgraded_abi, upgraded_meta = upgraded
            repicked = _select_function_abi(
                upgraded_abi,
                function_name=function_name,
                function_signature=function_signature,
            )
            if isinstance(repicked, dict):
                return repicked
            fn_abi, signature = repicked
            return fn_abi, signature, upgraded_abi, upgraded_meta

    return picked


def _annotate(
    *,
    address: str,
    label: str,
    status: str,
    chain_id: int,
    details: dict[str, Any],
    tool: str,
    action: str,
) -> None:
    store = WalletProfileStore.default()
    store.annotate_safe(
        address=address,
        label=label,
        protocol="contracts",
        action=action,
        tool=tool,
        status=status,
        chain_id=chain_id,
        details=details,
    )


async def contract_get_abi(
    *,
    chain_id: int,
    contract_address: str,
    resolve_proxy: bool = True,
) -> dict[str, Any]:
    """Fetch ABI for a deployed contract via Etherscan V2.

    If *resolve_proxy* is true, attempts to resolve common proxy patterns
    (EIP-1967 / ZeppelinOS / EIP-897) and fetch the implementation ABI.
    """
    addr = str(contract_address).strip()
    if not addr:
        return err("invalid_request", "contract_address is required")

    impl: str | None = None
    flavour: str | None = None
    impl_error: str | None = None

    if resolve_proxy:
        try:
            impl, flavour = await resolve_proxy_implementation(int(chain_id), addr)
        except Exception:
            impl, flavour = None, None

        if impl:
            try:
                impl_abi = await fetch_contract_abi(int(chain_id), impl)
            except ValueError as exc:
                msg = str(exc)
                code = _abi_error_code(msg)
                if code == "missing_api_key":
                    return err(code, msg)
                impl_error = msg
            except Exception as exc:
                impl_error = str(exc)
            else:
                meta: dict[str, Any] = {
                    "abi_source": "etherscan_v2_proxy",
                    "proxy_address": _safe_checksum_address(addr),
                    "implementation_address": impl,
                    "proxy_flavour": flavour,
                    "abi_sha256": sha256_json(impl_abi),
                    "abi_address": impl,
                }
                return ok(
                    {
                        "chain_id": int(chain_id),
                        "contract_address": _safe_checksum_address(addr),
                        "abi": impl_abi,
                        "abi_summary": summarize_abi(impl_abi),
                        **meta,
                    }
                )

    # Non-proxy / no proxy resolution
    try:
        abi_list = await fetch_contract_abi(int(chain_id), addr)
    except ValueError as exc:
        msg = str(exc)
        details = {"implementation_abi_error": impl_error} if impl_error else None
        return err(_abi_error_code(msg), msg, details)
    except Exception as exc:
        details = {"implementation_abi_error": impl_error} if impl_error else None
        return err("abi_fetch_failed", str(exc), details)

    meta: dict[str, Any] = {
        "abi_source": "etherscan_v2",
        "abi_sha256": sha256_json(abi_list),
        "abi_address": _safe_checksum_address(addr),
    }
    if impl:
        meta["proxy_address"] = _safe_checksum_address(addr)
        meta["implementation_address"] = impl
        meta["proxy_flavour"] = flavour
    if impl_error:
        meta["implementation_abi_error"] = impl_error
    return ok(
        {
            "chain_id": int(chain_id),
            "contract_address": _safe_checksum_address(addr),
            "abi": abi_list,
            "abi_summary": summarize_abi(abi_list),
            **meta,
        }
    )


async def contract_call(
    *,
    chain_id: int,
    contract_address: str,
    function_name: str | None = None,
    function_signature: str | None = None,
    args: list[Any] | str | None = None,
    value_wei: int = 0,
    from_address: str | None = None,
    wallet_label: str | None = None,
    abi: list[dict[str, Any]] | str | None = None,
    abi_path: str | None = None,
) -> dict[str, Any]:
    """Read from a deployed contract via eth_call.

    - Provide either `abi` (inline list or JSON string) or `abi_path` (JSON file inside this repo).
    - If neither is provided, this tool falls back to fetching the ABI from Etherscan V2
      (requires `system.etherscan_api_key` or `ETHERSCAN_API_KEY`, and the contract must be verified).
    - If the function is overloaded, pass `function_signature` like `deposit(uint256)`.
    """
    loaded_abi = await _resolve_abi(
        chain_id=int(chain_id),
        contract_address=contract_address,
        abi=abi,
        abi_path=abi_path,
    )
    if isinstance(loaded_abi, dict):
        return loaded_abi
    abi_list, abi_meta = loaded_abi

    parsed = _parse_args(args)
    if isinstance(parsed, dict):
        return parsed
    parsed_args = parsed

    value_parsed = _parse_value_wei(value_wei)
    if isinstance(value_parsed, dict):
        return value_parsed
    value_i = value_parsed

    from_addr = normalize_address(from_address)
    label = str(wallet_label or "").strip() or None
    caller, _label_used = resolve_wallet_address(
        wallet_label=label,
        wallet_address=from_addr,
    )
    if label and not from_addr and not caller:
        return err("not_found", f"Unknown wallet_label: {label}")

    picked = await _pick_function_abi(
        chain_id=int(chain_id),
        contract_address=contract_address,
        abi_list=abi_list,
        abi_meta=abi_meta,
        function_name=function_name,
        function_signature=function_signature,
    )
    if isinstance(picked, dict):
        return picked
    fn_abi, signature, abi_list, abi_meta = picked

    try:
        casted_args = cast_args(
            parsed_args,
            fn_abi.get("inputs", []) if isinstance(fn_abi.get("inputs"), list) else [],
        )
    except Exception as exc:
        return err("invalid_args", str(exc))

    try:
        async with web3_utils.web3_from_chain_id(int(chain_id)) as w3:
            contract = w3.eth.contract(
                address=AsyncWeb3.to_checksum_address(contract_address),
                abi=abi_list,
            )
            fn = contract.get_function_by_signature(signature)

            call_tx: dict[str, Any] = {}
            if caller:
                call_tx["from"] = AsyncWeb3.to_checksum_address(caller)
            if value_i:
                call_tx["value"] = value_i

            if call_tx:
                value = await fn(*casted_args).call(call_tx)
            else:
                value = await fn(*casted_args).call()
    except Exception as exc:
        logger.error(f"Contract call failed: {exc}")
        return err("call_failed", str(exc))

    result: dict[str, Any] = {
        "chain_id": int(chain_id),
        "contract_address": AsyncWeb3.to_checksum_address(contract_address),
        "function_signature": signature,
        "args": sanitize_for_json(casted_args),
        "value_wei": value_i,
        "value": sanitize_for_json(value),
    }
    if abi_meta:
        result.update(abi_meta)
    if wallet_label:
        result["wallet_label"] = wallet_label
    if caller:
        result["from_address"] = caller
    return ok(result)


async def contract_execute(
    *,
    wallet_label: str,
    chain_id: int,
    contract_address: str,
    function_name: str | None = None,
    function_signature: str | None = None,
    args: list[Any] | str | None = None,
    value_wei: int = 0,
    abi: list[dict[str, Any]] | str | None = None,
    abi_path: str | None = None,
    wait_for_receipt: bool = True,
) -> dict[str, Any]:
    """Execute a contract function by encoding calldata and broadcasting a tx.

    Use this for state-changing writes. For view/pure reads, use `contract_call`.
    """
    w = find_wallet_by_label(wallet_label)
    if not w:
        return err("not_found", f"Unknown wallet_label: {wallet_label}")

    sender = normalize_address(w.get("address"))
    pk = (
        (w.get("private_key") or w.get("private_key_hex"))
        if isinstance(w, dict)
        else None
    )
    if not sender or not pk:
        return err(
            "invalid_wallet",
            "Wallet must include address and private_key_hex in config.json",
        )

    loaded_abi = await _resolve_abi(
        chain_id=int(chain_id),
        contract_address=contract_address,
        abi=abi,
        abi_path=abi_path,
    )
    if isinstance(loaded_abi, dict):
        return loaded_abi
    abi_list, abi_meta = loaded_abi

    parsed = _parse_args(args)
    if isinstance(parsed, dict):
        return parsed
    parsed_args = parsed

    value_parsed = _parse_value_wei(value_wei)
    if isinstance(value_parsed, dict):
        return value_parsed
    value_i = value_parsed

    picked = await _pick_function_abi(
        chain_id=int(chain_id),
        contract_address=contract_address,
        abi_list=abi_list,
        abi_meta=abi_meta,
        function_name=function_name,
        function_signature=function_signature,
    )
    if isinstance(picked, dict):
        return picked
    fn_abi, signature, abi_list, abi_meta = picked

    mut = str(fn_abi.get("stateMutability") or "").strip().lower()
    if mut in {"view", "pure"} or bool(fn_abi.get("constant")):
        return err(
            "invalid_function",
            f"Function '{signature}' is view/pure; use contract_call instead of contract_execute.",
        )

    try:
        casted_args = cast_args(
            parsed_args,
            fn_abi.get("inputs", []) if isinstance(fn_abi.get("inputs"), list) else [],
        )
    except Exception as exc:
        return err("invalid_args", str(exc))

    sign_callback = make_sign_callback(pk)

    try:
        tx = await encode_call(
            target=contract_address,
            abi=abi_list,
            fn_name=signature,
            args=casted_args,
            from_address=sender,
            chain_id=int(chain_id),
            value=value_i,
        )
    except Exception as exc:
        logger.error(f"Failed to encode contract call: {exc}")
        return err("encode_failed", str(exc))

    try:
        txn_hash = await send_transaction(
            tx, sign_callback, wait_for_receipt=wait_for_receipt
        )
    except Exception as exc:
        logger.error(f"Contract execution failed: {exc}")
        _annotate(
            address=sender,
            label=wallet_label,
            status="failed",
            chain_id=int(chain_id),
            details={
                "contract_address": contract_address,
                "function_signature": signature,
                "args": sanitize_for_json(casted_args),
                "value_wei": value_i,
                "error": sanitize_for_json(str(exc)),
                **(abi_meta or {}),
            },
            tool="contract_execute",
            action="contract_execute",
        )
        return err("execution_failed", str(exc))

    explorer_link = get_etherscan_transaction_link(int(chain_id), txn_hash)
    result: dict[str, Any] = {
        "tx_hash": txn_hash,
        "chain_id": int(chain_id),
        "from_address": sender,
        "contract_address": AsyncWeb3.to_checksum_address(contract_address),
        "function_signature": signature,
        "args": sanitize_for_json(casted_args),
        "value_wei": value_i,
    }
    if abi_meta:
        result.update(abi_meta)
    if explorer_link:
        result["explorer_url"] = explorer_link

    _annotate(
        address=sender,
        label=wallet_label,
        status="confirmed" if wait_for_receipt else "broadcast",
        chain_id=int(chain_id),
        details=result,
        tool="contract_execute",
        action="contract_execute",
    )

    return ok(result)
