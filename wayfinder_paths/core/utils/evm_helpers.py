import json
import os
from typing import Any

from loguru import logger

from wayfinder_paths.core.constants.chains import CHAIN_CODE_TO_ID


def resolve_chain_id(token_info: dict[str, Any]) -> int | None:
    chain_meta = token_info.get("chain") or {}
    chain_id = chain_meta.get("id")
    if chain_id is not None:
        return int(chain_id)
    return CHAIN_CODE_TO_ID.get(chain_meta.get("code").lower())


def resolve_rpc_url(
    chain_id: int | None,
    config: dict[str, Any],
    explicit_rpc_url: str | None = None,
) -> str:
    if explicit_rpc_url:
        return explicit_rpc_url
    strategy_cfg = config.get("strategy") or {}
    mapping = strategy_cfg.get("rpc_urls") if isinstance(strategy_cfg, dict) else None
    if chain_id is not None and isinstance(mapping, dict):
        by_int = mapping.get(chain_id)
        if by_int:
            if isinstance(by_int, list):
                return str(by_int[0])
            return str(by_int)
        by_str = mapping.get(str(chain_id))
        if by_str:
            if isinstance(by_str, list):
                return str(by_str[0])
            return str(by_str)
    raise ValueError("RPC URL not provided. Set strategy.rpc_urls in config.json.")


def resolve_private_key_for_from_address(
    from_address: str, config: dict[str, Any]
) -> str | None:
    from_addr_norm = (from_address or "").lower()
    main_wallet = config.get("main_wallet")
    strategy_wallet = config.get("strategy_wallet")

    main_pk = None
    strategy_pk = None
    try:
        if isinstance(main_wallet, dict):
            main_pk = main_wallet.get("private_key") or main_wallet.get(
                "private_key_hex"
            )
        if isinstance(strategy_wallet, dict):
            strategy_pk = strategy_wallet.get("private_key") or strategy_wallet.get(
                "private_key_hex"
            )
    except (AttributeError, TypeError) as e:
        logger.debug("Error resolving private keys from wallet config: %s", e)

    main_addr = None
    strategy_addr = None
    try:
        main_addr = (main_wallet or {}).get("address") or (
            (main_wallet or {}).get("evm") or {}
        ).get("address")
        strategy_addr = (strategy_wallet or {}).get("address") or (
            (strategy_wallet or {}).get("evm") or {}
        ).get("address")
    except (AttributeError, TypeError) as e:
        logger.debug("Error resolving addresses from wallet config: %s", e)

    if main_addr and from_addr_norm == (main_addr or "").lower():
        return main_pk
    if strategy_addr and from_addr_norm == (strategy_addr or "").lower():
        return strategy_pk

    # No fallback - private keys must be in config or config.json
    return None


async def _get_abi(chain_id: int, address: str) -> str | None:
    os.makedirs(f"abis/{chain_id}/", exist_ok=True)

    abi_file = f"abis/{chain_id}/{address}.json"
    if not os.path.exists(abi_file):
        raise ValueError(
            f"There is no downloaded ABI for {address} on chain {chain_id} -- please download it to ({abi_file})  (make sure to get the implementation if this address is a proxy)"
        )

    with open(abi_file) as f:
        abi = f.read()

    return abi


# We filter ABIs for Privy Policy since most of the abi is useless, and we don't wanna upload big ABIs for both size and readability reasons.
async def get_abi_filtered(
    chain_id: int, address: str, function_names: list[str]
) -> list | None:
    full_abi = await _get_abi(chain_id, address)
    if full_abi is None:
        raise Exception("Could not pull ABI, get_abi returned None")
    abi_json = json.loads(full_abi)
    filtered_abi = [
        item
        for item in abi_json
        if item.get("type") == "function" and item.get("name") in function_names
    ]
    return filtered_abi
