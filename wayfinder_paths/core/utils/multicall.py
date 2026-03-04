from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from eth_utils.abi import collapse_if_tuple
from web3 import AsyncWeb3

from wayfinder_paths.adapters.multicall_adapter.adapter import MulticallAdapter
from wayfinder_paths.core.constants.contracts import MULTICALL3_ADDRESS


@dataclass(frozen=True)
class Call:
    contract: Any
    fn_name: str
    args: tuple[Any, ...] = ()
    postprocess: Callable[[Any], Any] | None = None


def _decode_output(web3: AsyncWeb3, contract: Any, fn_name: str, data: bytes) -> Any:
    fn = contract.get_function_by_name(fn_name)
    outputs = fn.abi.get("outputs") or []
    types = [
        collapse_if_tuple(o)
        for o in outputs
        if isinstance(o, dict) and o.get("type") is not None
    ]
    decoded = web3.codec.decode(types, data) if types else ()
    if len(decoded) == 1:
        return decoded[0]
    return decoded


async def _multicall3_supported(
    web3: AsyncWeb3, *, address: str = MULTICALL3_ADDRESS
) -> bool:
    try:
        code = await web3.eth.get_code(web3.to_checksum_address(address))
    except Exception:
        return False
    return len(code) > 0


async def read_only_calls_multicall_or_gather(
    *,
    web3: AsyncWeb3,
    chain_id: int | None,
    calls: Sequence[Call],
    block_identifier: str | int = "latest",
    chunk_size: int = 0,
) -> list[Any]:
    """
    Execute read-only contract calls using Multicall3 when possible, with a safe fallback (asyncio.gather).

    Notes:
    - All calls share the same `block_identifier` (matching typical gather usage).
    - Use `chunk_size` to avoid oversized multicall payloads (0 disables chunking).
    """

    if not calls:
        return []

    async def _fallback(chunk: Sequence[Call]) -> list[Any]:
        coros: list[Awaitable[Any]] = []
        for c in chunk:
            fn = getattr(c.contract.functions, c.fn_name)
            coros.append(fn(*c.args).call(block_identifier=block_identifier))
        results = list(await asyncio.gather(*coros))
        out: list[Any] = []
        for spec, value in zip(chunk, results, strict=True):
            out.append(spec.postprocess(value) if spec.postprocess else value)
        return out

    supported = await _multicall3_supported(web3)
    if not supported:
        return await _fallback(calls)

    batches: list[Sequence[Call]]
    if chunk_size and chunk_size > 0 and len(calls) > chunk_size:
        batches = [calls[i : i + chunk_size] for i in range(0, len(calls), chunk_size)]
    else:
        batches = [calls]

    out_all: list[Any] = []
    for batch in batches:
        try:
            mc = MulticallAdapter(web3=web3, chain_id=chain_id)
            mc_calls = []
            for c in batch:
                calldata = c.contract.encode_abi(c.fn_name, args=list(c.args))
                mc_calls.append(mc.build_call(c.contract.address, calldata))

            res = await mc.aggregate(mc_calls, block_identifier=block_identifier)
            decoded: list[Any] = []
            for spec, data in zip(batch, res.return_data, strict=True):
                value = _decode_output(web3, spec.contract, spec.fn_name, data)
                decoded.append(spec.postprocess(value) if spec.postprocess else value)
            out_all.extend(decoded)
        except Exception:
            out_all.extend(await _fallback(batch))

    return out_all
