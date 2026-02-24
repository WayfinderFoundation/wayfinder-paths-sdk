"""Contract deployment and Etherscan verification.

Uses the SDK's existing transaction infrastructure (``send_transaction``,
``web3_from_chain_id``) so nonce management, gas pricing, broadcast, receipt
waiting, and Gorlami fork detection all work automatically.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
from eth_utils import remove_0x_prefix
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
    extract_abi_and_bytecode,
)
from wayfinder_paths.core.utils.transaction import send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id


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
            args = (
                cast_args(constructor_args, ctor_inputs)
                if ctor_inputs
                else constructor_args
            )

        tx = await contract.constructor(*args).build_transaction(
            {
                "chainId": chain_id,
                "from": AsyncWeb3.to_checksum_address(from_address),
                "value": 0,
            }
        )

    return dict(tx)


async def deploy_contract(
    *,
    source_code: str,
    contract_name: str,
    constructor_args: list[Any] | None = None,
    from_address: str,
    chain_id: int,
    sign_callback: Callable[..., Any],
    verify: bool = True,
    etherscan_api_key: str | None = None,
) -> dict[str, Any]:
    """Full deployment pipeline: compile -> deploy -> verify.

    Returns ``{"tx_hash", "contract_address", "abi", "bytecode"}``.
    """
    # Compile once using standard JSON input so verification can reuse the same input.
    std_json = compile_solidity_standard_json(source_code)
    output = std_json.get("output") if isinstance(std_json.get("output"), dict) else {}
    abi, bytecode = extract_abi_and_bytecode(output, contract_name=contract_name)
    if not bytecode or bytecode == "0x":
        raise RuntimeError(f"Compiled bytecode for '{contract_name}' is empty")

    tx = await build_deploy_transaction(
        abi=abi,
        bytecode=bytecode,
        constructor_args=constructor_args,
        from_address=from_address,
        chain_id=chain_id,
    )

    tx_hash = await send_transaction(tx, sign_callback, wait_for_receipt=True)

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
        "standard_json_input": std_json["input"],
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
                bytecode_hex = remove_0x_prefix(bytecode)

                deploy_data = tx.get("data") or tx.get("input")
                if isinstance(deploy_data, (bytes, bytearray)):
                    deploy_data_hex = deploy_data.hex()
                else:
                    deploy_data_hex = remove_0x_prefix(str(deploy_data or ""))

                if deploy_data_hex.startswith(bytecode_hex):
                    encoded_args = deploy_data_hex[len(bytecode_hex) :] or None
                else:
                    # Fallback: reconstruct constructor calldata if the tx builder
                    # produced unexpected formatting.
                    async with web3_from_chain_id(chain_id) as w3:
                        contract = w3.eth.contract(abi=abi, bytecode=bytecode)
                        ctor_inputs = get_constructor_inputs(abi)
                        args = (
                            cast_args(constructor_args, ctor_inputs)
                            if ctor_inputs
                            else constructor_args
                        )
                        full_data_hex = remove_0x_prefix(
                            contract.constructor(*args).data_in_transaction
                        )
                        encoded_args = full_data_hex[len(bytecode_hex) :] or None

            verified = await verify_on_etherscan(
                chain_id=chain_id,
                contract_address=contract_address,
                standard_json_input=std_json["input"],
                contract_name=contract_name,
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
        payload["constructorArguements"] = (
            constructor_args_encoded  # Etherscan's typo is intentional
        )

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
