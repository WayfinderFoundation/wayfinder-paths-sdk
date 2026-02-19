"""Contract deployment, escape-hatch injection, and Etherscan verification.

Uses the SDK's existing transaction infrastructure (``send_transaction``,
``web3_from_chain_id``) so nonce management, gas pricing, broadcast, receipt
waiting, and Gorlami fork detection all work automatically.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any

import httpx
from loguru import logger
from web3 import AsyncWeb3

from wayfinder_paths.core.config import get_etherscan_api_key
from wayfinder_paths.core.constants.chains import ETHERSCAN_V2_API_URL
from wayfinder_paths.core.utils.abi_caster import cast_args, get_constructor_inputs
from wayfinder_paths.core.utils.etherscan import get_etherscan_transaction_link
from wayfinder_paths.core.utils.retry import exponential_backoff_s
from wayfinder_paths.core.utils.solidity import (
    SOLC_VERSION,
    compile_solidity_standard_json,
)
from wayfinder_paths.core.utils.transaction import send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

_ESCAPE_HATCH_SNIPPET = """
    // --- Escape Hatch (injected) ---
    function escapeHatch(address token, uint256 amount) external onlyOwner {
        if (token == address(0)) {
            payable(owner()).transfer(amount);
        } else {
            IERC20(token).transfer(owner(), amount);
        }
    }
"""

_IERC20_IMPORT = 'import "@openzeppelin/contracts/token/ERC20/IERC20.sol";'
_OWNABLE_IMPORT = 'import "@openzeppelin/contracts/access/Ownable.sol";'


_IMPORT_STMT_RE = re.compile(r'^\s*import\s+[^;]+;\s*$', re.MULTILINE)
_PRAGMA_STMT_RE = re.compile(r"^\s*pragma\s+solidity\b[^;]*;\s*$", re.MULTILINE)


def _find_contract_body_span(source_code: str, *, contract_name: str) -> tuple[int, int]:
    """Return ``(open_brace_index, close_brace_index)`` for *contract_name*."""
    m = re.search(
        rf"^\s*contract\s+{re.escape(contract_name)}\b", source_code, flags=re.MULTILINE
    )
    if not m:
        raise ValueError(f"Contract '{contract_name}' not found in source")

    open_brace = source_code.find("{", m.end())
    if open_brace == -1:
        raise ValueError(f"Could not find '{{' for contract '{contract_name}'")

    depth = 0
    for idx in range(open_brace, len(source_code)):
        ch = source_code[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return open_brace, idx

    raise ValueError(f"Could not find matching '}}' for contract '{contract_name}'")


def add_escape_hatch(source_code: str, *, contract_name: str) -> str:
    """Inject an ``onlyOwner`` escape-hatch function for fund recovery.

    Adds ``Ownable`` inheritance and an ``IERC20`` import if not already
    present, then inserts the ``escapeHatch()`` function before the last
    closing brace of the specified contract.
    """
    code = source_code

    open_brace, close_brace = _find_contract_body_span(code, contract_name=contract_name)

    # Insert required imports (after last import if present, else after pragma).
    insert_at = 0
    imports = list(_IMPORT_STMT_RE.finditer(code))
    if imports:
        insert_at = imports[-1].end()
    else:
        pragma = _PRAGMA_STMT_RE.search(code)
        if pragma:
            insert_at = pragma.end()

    to_insert: list[str] = []
    if _IERC20_IMPORT not in code:
        to_insert.append(_IERC20_IMPORT)
    if _OWNABLE_IMPORT not in code:
        to_insert.append(_OWNABLE_IMPORT)
    if to_insert:
        insert_block = "\n" + "\n".join(to_insert) + "\n"
        code = code[:insert_at] + insert_block + code[insert_at:]
        # Adjust spans if we inserted before the contract.
        if insert_at <= open_brace:
            delta = len(insert_block)
            open_brace += delta
            close_brace += delta

    # Add Ownable inheritance to the target contract header if missing.
    header = code[:open_brace]
    m = re.search(
        rf"(^\s*contract\s+{re.escape(contract_name)}\b)([^{{\n]*?)$",
        header,
        flags=re.MULTILINE,
    )
    if not m:
        raise ValueError(f"Could not locate contract header for '{contract_name}'")

    header_line = m.group(0)
    if re.search(r"\bOwnable\b", header_line) and not re.search(
        r"\bOwnable\s*\(", header_line
    ):
        # OZ v5 Ownable requires an initial owner argument. If the contract
        # already inherits Ownable but doesn't pass args, default to deployer.
        new_header_line = re.sub(
            r"\bOwnable\b", "Ownable(msg.sender)", header_line, count=1
        )
        header = header[: m.start()] + new_header_line + header[m.end() :]
        code = header + code[open_brace:]
        open_brace, close_brace = _find_contract_body_span(
            code, contract_name=contract_name
        )
    elif "Ownable" not in header_line:
        if re.search(r"\bis\b", header_line):
            new_header_line = re.sub(
                r"\bis\b\s*",
                "is Ownable(msg.sender), ",
                header_line,
                count=1,
            )
        else:
            new_header_line = header_line.rstrip() + " is Ownable(msg.sender)"
        header = header[: m.start()] + new_header_line + header[m.end() :]
        code = header + code[open_brace:]
        # Recompute spans (header length changed).
        open_brace, close_brace = _find_contract_body_span(
            code, contract_name=contract_name
        )

    # Insert escapeHatch before the contract's closing brace (idempotent).
    body = code[open_brace:close_brace]
    if re.search(r"\bfunction\s+escapeHatch\s*\(", body):
        return code

    code = code[:close_brace] + _ESCAPE_HATCH_SNIPPET + "\n" + code[close_brace:]

    return code


async def build_deploy_transaction(
    *,
    abi: list[dict[str, Any]],
    bytecode: str,
    constructor_args: list[Any] | None = None,
    from_address: str,
    chain_id: int,
) -> dict[str, Any]:
    """Build an unsigned contract-creation transaction.

    Casts constructor args via ``abi_caster`` and encodes them into the
    deployment bytecode.
    """
    async with web3_from_chain_id(chain_id) as w3:
        contract = w3.eth.contract(abi=abi, bytecode=bytecode)

        args: list[Any] = []
        if constructor_args:
            ctor_inputs = get_constructor_inputs(abi)
            args = cast_args(constructor_args, ctor_inputs) if ctor_inputs else constructor_args

        tx = await contract.constructor(*args).build_transaction(
            {
                "chainId": chain_id,
                "from": AsyncWeb3.to_checksum_address(from_address),
                "value": 0,
            }
        )

    # Remove fields that send_transaction() will set
    tx.pop("gas", None)
    tx.pop("gasPrice", None)
    tx.pop("nonce", None)

    return dict(tx)


async def deploy_contract(
    *,
    source_code: str,
    contract_name: str,
    source_filename: str = "Contract.sol",
    constructor_args: list[Any] | None = None,
    from_address: str,
    chain_id: int,
    sign_callback: Callable[..., Any],
    verify: bool = True,
    escape_hatch: bool = False,
    etherscan_api_key: str | None = None,
    project_root: str | None = None,
) -> dict[str, Any]:
    """Full deployment pipeline: compile -> deploy -> verify.

    Returns ``{"tx_hash", "contract_address", "abi", "bytecode"}``.
    """
    code = source_code
    if escape_hatch:
        code = add_escape_hatch(code, contract_name=contract_name)

    # Compile once using standard JSON input so verification can reuse the same input.
    std_json = compile_solidity_standard_json(
        code,
        source_filename=source_filename,
        project_root=project_root,
    )
    contracts = (
        (std_json.get("output") or {}).get("contracts", {}).get(source_filename, {})
    )
    if not isinstance(contracts, dict) or contract_name not in contracts:
        available = list(contracts.keys()) if isinstance(contracts, dict) else []
        raise ValueError(
            f"Contract '{contract_name}' not found in compilation output. "
            f"Available: {available}"
        )

    contract_artifact = contracts[contract_name]
    abi = contract_artifact.get("abi", [])
    evm = contract_artifact.get("evm", {})
    bytecode_obj = ""
    if isinstance(evm, dict):
        bc = evm.get("bytecode", {})
        if isinstance(bc, dict):
            bytecode_obj = bc.get("object") or ""
    bytecode = str(bytecode_obj)
    if bytecode and not bytecode.startswith("0x"):
        bytecode = "0x" + bytecode
    if not bytecode or bytecode == "0x":
        raise RuntimeError(f"Compiled bytecode for '{contract_name}' is empty")

    # Build + broadcast the deploy transaction
    tx = await build_deploy_transaction(
        abi=abi,
        bytecode=bytecode,
        constructor_args=constructor_args,
        from_address=from_address,
        chain_id=chain_id,
    )

    tx_hash = await send_transaction(tx, sign_callback, wait_for_receipt=True)

    # Get contract address from receipt
    async with web3_from_chain_id(chain_id) as w3:
        receipt = await w3.eth.get_transaction_receipt(tx_hash)

    contract_address = receipt.get("contractAddress")
    if not contract_address:
        raise RuntimeError(
            f"Deploy tx {tx_hash} succeeded but no contractAddress in receipt"
        )

    result: dict[str, Any] = {
        "tx_hash": tx_hash,
        "contract_address": contract_address,
        "abi": abi,
        "bytecode": bytecode,
    }

    explorer_link = get_etherscan_transaction_link(chain_id, tx_hash)
    if explorer_link:
        result["explorer_url"] = explorer_link

    # Verify on Etherscan (best-effort, don't fail the deploy)
    if verify:
        try:
            # Encode constructor args for verification
            encoded_args = None
            if constructor_args:
                async with web3_from_chain_id(chain_id) as w3:
                    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
                    ctor_inputs = get_constructor_inputs(abi)
                    args = cast_args(constructor_args, ctor_inputs) if ctor_inputs else constructor_args
                    # The constructor data = bytecode + encoded args
                    # So encoded args = full data - bytecode
                    full_data = contract.constructor(*args).data_in_transaction
                    encoded_args = full_data[len(bytecode) :]
                    if encoded_args.startswith("0x"):
                        encoded_args = encoded_args[2:]

            verified = await verify_on_etherscan(
                chain_id=chain_id,
                contract_address=contract_address,
                standard_json_input=std_json["input"],
                contract_name=contract_name,
                source_filename=source_filename,
                constructor_args_encoded=encoded_args,
                etherscan_api_key=etherscan_api_key,
            )
            result["verified"] = verified
        except Exception as exc:
            logger.warning(f"Etherscan verification failed (non-fatal): {exc}")
            result["verified"] = False
            result["verification_error"] = str(exc)

    return result


async def verify_on_etherscan(
    *,
    chain_id: int,
    contract_address: str,
    standard_json_input: dict[str, Any],
    contract_name: str,
    source_filename: str = "Contract.sol",
    constructor_args_encoded: str | None = None,
    compiler_version: str = f"v{SOLC_VERSION}+commit.8a97fa7a",
    etherscan_api_key: str | None = None,
) -> bool:
    """Verify a contract on Etherscan using standard-JSON-input mode.

    Uses the Etherscan V2 unified API endpoint with ``chainid`` parameter.
    Retries with exponential backoff when Etherscan reports pending status.
    """
    api_key = etherscan_api_key or get_etherscan_api_key()
    if not api_key:
        raise ValueError(
            "Etherscan API key required for verification. "
            "Set system.etherscan_api_key in config.json or ETHERSCAN_API_KEY env var, "
            "or call deploy with verify=False to skip verification."
        )

    # Fully qualified contract name: "Contract.sol:MyContract"
    fq_name = f"{source_filename}:{contract_name}"

    payload = {
        "apikey": api_key,
        "module": "contract",
        "action": "verifysourcecode",
        "sourceCode": json.dumps(standard_json_input),
        "codeformat": "solidity-standard-json-input",
        "contractaddress": contract_address,
        "contractname": fq_name,
        "compilerversion": compiler_version,
    }

    if constructor_args_encoded:
        payload["constructorArguements"] = constructor_args_encoded  # Etherscan's typo is intentional

    # Etherscan V2 requires chainid as a query parameter (not only in form data).
    # Submission can race explorer indexing, so retry a few times if contract code
    # isn't located yet.
    guid: str | None = None
    submit_attempts = 10
    check_attempts = 10
    max_delay_s = 30.0

    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(submit_attempts):
            resp = await client.post(
                ETHERSCAN_V2_API_URL,
                params={"chainid": str(chain_id)},
                data=payload,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") == "1":
                guid = str(data.get("result") or "").strip() or None
                if not guid:
                    raise RuntimeError("Etherscan verification returned empty GUID")
                logger.info(f"Etherscan verification submitted, GUID: {guid}")
                break

            msg = str(
                data.get("result", "") or data.get("message", "") or "Unknown error"
            )
            msg_l = msg.lower()
            if "already verified" in msg_l:
                logger.info(
                    f"Contract already verified on Etherscan: {contract_address}"
                )
                return True
            if (
                "unable to locate contractcode" in msg_l
                or "unable to locate contract code" in msg_l
            ):
                delay = exponential_backoff_s(
                    attempt, base_delay_s=1, max_delay_s=max_delay_s
                )
                logger.debug(
                    f"Etherscan hasn't indexed contract code yet; retrying in {delay}s "
                    f"(attempt {attempt + 1}/{submit_attempts})..."
                )
                await asyncio.sleep(delay)
                continue

            raise RuntimeError(f"Etherscan verification submission failed: {msg}")

        if not guid:
            raise RuntimeError(
                "Etherscan verification submission failed: explorer did not index contract code in time"
            )

        # Poll for result with exponential backoff
        check_payload = {
            "apikey": api_key,
            "module": "contract",
            "action": "checkverifystatus",
            "guid": guid,
        }

        for attempt in range(check_attempts):
            delay = exponential_backoff_s(
                attempt, base_delay_s=1, max_delay_s=max_delay_s
            )
            await asyncio.sleep(delay)

            resp = await client.get(
                ETHERSCAN_V2_API_URL,
                params={"chainid": str(chain_id), **check_payload},
            )
            resp.raise_for_status()
            data = resp.json()

            result_msg = str(data.get("result", ""))

            if data.get("status") == "1":
                logger.info(f"Contract verified on Etherscan: {contract_address}")
                return True

            if "already verified" in result_msg.lower():
                logger.info(
                    f"Contract already verified on Etherscan: {contract_address}"
                )
                return True

            if "pending" in result_msg.lower():
                logger.debug(f"Verification pending (attempt {attempt + 1})...")
                continue

            raise RuntimeError(f"Etherscan verification failed: {result_msg}")

        raise RuntimeError(
            f"Etherscan verification timed out after {check_attempts} attempts"
        )
