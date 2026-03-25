import json
import os
from typing import Any

from wayfinder_paths.core.constants.chains import CHAIN_CODE_TO_ID


def resolve_chain_id(token_info: dict[str, Any]) -> int | None:
    chain_meta = token_info.get("chain") or {}
    chain_id = chain_meta.get("id")
    if chain_id is not None:
        return int(chain_id)
    code = chain_meta.get("code")
    if code is None:
        return None
    return CHAIN_CODE_TO_ID.get(code.lower())


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
