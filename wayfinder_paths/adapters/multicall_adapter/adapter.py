from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from hexbytes import HexBytes

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.constants.contracts import MULTICALL3_ADDRESS
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI

MULTICALL3_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "target", "type": "address"},
                    {"internalType": "bytes", "name": "callData", "type": "bytes"},
                ],
                "internalType": "struct Multicall3.Call[]",
                "name": "calls",
                "type": "tuple[]",
            }
        ],
        "name": "aggregate",
        "outputs": [
            {"internalType": "uint256", "name": "blockNumber", "type": "uint256"},
            {"internalType": "bytes[]", "name": "returnData", "type": "bytes[]"},
        ],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "addr", "type": "address"}],
        "name": "getEthBalance",
        "outputs": [
            {"internalType": "uint256", "name": "balance", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass(frozen=True)
class MulticallCall:
    target: str
    call_data: bytes | str

    def as_tuple(self) -> tuple[str, bytes | str]:
        return self.target, self.call_data


@dataclass
class MulticallResult:
    block_number: int
    return_data: Sequence[bytes]


class MulticallAdapter(BaseAdapter):
    adapter_type = "MULTICALL"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        chain_id: int | None = None,
        web3: Any | None = None,
        address: str | None = None,
        abi: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__("multicall_adapter", config)

        if web3 is None:
            raise ValueError("MulticallAdapter requires web3 instance")
        self.chain_id = int(chain_id) if chain_id is not None else None
        self.web3 = web3

        checksum_address = self.web3.to_checksum_address(address or MULTICALL3_ADDRESS)
        self.contract = self.web3.eth.contract(
            address=checksum_address, abi=abi or MULTICALL3_ABI
        )

    async def aggregate(
        self,
        calls: Iterable[MulticallCall | tuple[str, bytes | str]],
        *,
        value: int = 0,
        block_identifier: str | int | None = None,
    ) -> MulticallResult:
        calls_list = list(calls)
        if not calls_list:
            return MulticallResult(block_number=0, return_data=[])

        encoded_calls: list[tuple[str, bytes]] = []
        for call in calls_list:
            target, calldata = self._coerce_call(call)
            encoded_calls.append((target, calldata))

        call_fn = self.contract.functions.aggregate(encoded_calls).call
        if block_identifier is None:
            block_number, return_data = await call_fn({"value": int(value)})
        else:
            block_number, return_data = await call_fn(
                {"value": int(value)}, block_identifier=block_identifier
            )
        payload = tuple(self._ensure_bytes(r) for r in return_data)
        return MulticallResult(block_number=int(block_number), return_data=payload)

    def build_call(self, target: str, call_data: bytes | str) -> MulticallCall:
        checksum = self.web3.to_checksum_address(target)
        normalized = self._normalize_call_data(call_data)
        return MulticallCall(target=checksum, call_data=normalized)

    def encode_eth_balance(self, account: str) -> MulticallCall:
        calldata = self.contract.encode_abi("getEthBalance", args=[account])
        return self.build_call(self.contract.address, calldata)

    def encode_erc20_balance(self, token: str, account: str) -> MulticallCall:
        addr = self.web3.to_checksum_address(token)
        erc20 = self.web3.eth.contract(address=addr, abi=ERC20_ABI)
        calldata = erc20.encode_abi("balanceOf", args=[account])
        return self.build_call(addr, calldata)

    @staticmethod
    def decode_uint256(data: bytes | str) -> int:
        raw = MulticallAdapter._normalize_call_data(data)
        if len(raw) < 32:
            raw = raw.rjust(32, b"\x00")
        return int.from_bytes(raw[-32:], byteorder="big")

    def _coerce_call(
        self, call: MulticallCall | tuple[str, bytes | str]
    ) -> tuple[str, bytes]:
        if isinstance(call, MulticallCall):
            target = self.web3.to_checksum_address(call.target)
            calldata = self._normalize_call_data(call.call_data)
        else:
            target_str, call_data = call
            target = self.web3.to_checksum_address(target_str)
            calldata = self._normalize_call_data(call_data)
        return target, calldata

    @staticmethod
    def _normalize_call_data(data: bytes | str) -> bytes:
        if isinstance(data, bytes):
            return data
        if isinstance(data, HexBytes):
            return bytes(data)
        if isinstance(data, str):
            if data.startswith("0x"):
                return bytes.fromhex(data[2:])
            return data.encode()
        raise TypeError("Unsupported calldata type")

    @staticmethod
    def _ensure_bytes(data: bytes | str | HexBytes) -> bytes:
        if isinstance(data, bytes):
            return data
        if isinstance(data, HexBytes):
            return bytes(data)
        if isinstance(data, str):
            if data.startswith("0x"):
                return bytes.fromhex(data[2:])
            return data.encode()
        raise TypeError("Unexpected return data type from multicall")
