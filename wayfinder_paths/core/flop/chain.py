"""The FLOP chain: reads, and transactions signed by the user's FLOP account."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from substrateinterface import SubstrateInterface
from substrateinterface.exceptions import SubstrateRequestException
from websocket import WebSocketException

from wayfinder_paths.core.flop.signer import FlopSigner
from wayfinder_paths.core.flop.wire import ModelIdentity, blake2_256, u32le

DEVNET_RPC_URL = "wss://rpc.devnet.flop.network"
DEVNET_GENESIS_HASH = (
    "0x99c172c095340ace980631f9123544c6bebd4839f3437e463533422d8be2d631"
)
ECDSA_SIGNATURE_VERSION = 2
IMMORTAL_ERA = "00"
PRECISIONS = ("FP32", "FP16", "BF16", "INT8", "INT4", "Mixed")
# What the transaction payload carries after the call, in this chain's signed-extension
# order. A runtime that changes it fails the self-check in `_unsigned_payload`.
EXPECTED_SIGNED_EXTENSIONS = (
    "CheckNonZeroSender",
    "CheckSpecVersion",
    "CheckTxVersion",
    "CheckGenesis",
    "CheckMortality",
    "CheckNonce",
    "CheckWeight",
    "ChargeTransactionPayment",
    "CheckMetadataHash",
)


class FlopChainError(Exception):
    pass


@dataclass(frozen=True)
class Balance:
    free: int
    reserved: int


@dataclass(frozen=True)
class Channel:
    state: str
    agent: str
    miner: str
    agent_key: bytes
    enclave_key: bytes
    model_hash: bytes
    measured_root: bytes
    decode_policy_hash: bytes
    precision: int
    escrow: int
    opened_at: int
    max_duration: int
    authored_opened_at: int
    snapshot_cap: int | None
    current_cap: int | None


@dataclass(frozen=True)
class ChannelView:
    block: int
    channel: Channel | None


@dataclass(frozen=True)
class Submitted:
    extrinsic_hash: str
    block_hash: str
    block_number: int
    fee: int
    events: list[dict[str, Any]]

    def event(self, module: str, name: str) -> dict[str, Any] | None:
        for event in self.events:
            if event["module_id"] == module and event["event_id"] == name:
                return event["attributes"]
        return None


def _bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    text = str(value)
    return bytes.fromhex(text.removeprefix("0x"))


def _compact(value: int) -> bytes:
    if value < 1 << 6:
        return bytes([value << 2])
    if value < 1 << 14:
        return ((value << 2) | 1).to_bytes(2, "little")
    if value < 1 << 30:
        return ((value << 2) | 2).to_bytes(4, "little")
    raw = value.to_bytes((value.bit_length() + 7) // 8, "little")
    return bytes([((len(raw) - 4) << 2) | 3]) + raw


class FlopChain:
    def __init__(
        self,
        rpc_url: str = DEVNET_RPC_URL,
        expected_genesis_hash: str = DEVNET_GENESIS_HASH,
    ) -> None:
        self.rpc_url = rpc_url
        self.expected_genesis_hash = expected_genesis_hash.lower()
        self._substrate: SubstrateInterface | None = None

    # ---- connection -----------------------------------------------------------------

    def _connect(self) -> SubstrateInterface:
        if self._substrate is None:
            substrate = SubstrateInterface(url=self.rpc_url, ss58_format=42)
            genesis = substrate.get_block_hash(0).lower()
            if genesis != self.expected_genesis_hash:
                substrate.close()
                raise FlopChainError(
                    f"{self.rpc_url} is not the expected FLOP network (genesis {genesis})"
                )
            substrate.init_runtime()
            extensions = tuple(
                e["identifier"]
                for e in substrate.metadata.value[1]["V14"]["extrinsic"][
                    "signed_extensions"
                ]
            )
            if extensions != EXPECTED_SIGNED_EXTENSIONS:
                substrate.close()
                raise FlopChainError(
                    "the FLOP runtime changed its transaction format; update the SDK"
                )
            self._substrate = substrate
        return self._substrate

    def close(self) -> None:
        if self._substrate is not None:
            self._substrate.close()
            self._substrate = None

    def _retrying(self, operation):
        """A dropped websocket is reconnected once; a second failure is the caller's."""
        try:
            return operation(self._connect())
        except (
            ConnectionError,
            OSError,
            WebSocketException,
            SubstrateRequestException,
        ) as first:
            if isinstance(first, SubstrateRequestException) and "1010" in str(first):
                raise
            self.close()
            return operation(self._connect())

    async def _run(self, operation):
        return await asyncio.to_thread(self._retrying, operation)

    # ---- reads ----------------------------------------------------------------------

    @property
    def genesis_hash(self) -> bytes:
        return _bytes(self.expected_genesis_hash)

    async def finalized_block(self) -> int:
        def read(s: SubstrateInterface) -> int:
            return s.get_block_header(finalized_only=True)["header"]["number"]

        return await self._run(read)

    async def balance(self, address: str) -> Balance:
        def read(s: SubstrateInterface) -> Balance:
            data = s.query("System", "Account", [address]).value["data"]
            return Balance(free=int(data["free"]), reserved=int(data["reserved"]))

        return await self._run(read)

    async def constant(self, module: str, name: str) -> int:
        return await self._run(lambda s: int(s.get_constant(module, name).value))

    async def channel(self, channel_id: bytes) -> ChannelView:
        return await self._run(lambda s: self._channel_view(s, channel_id))

    def _channel_view(self, s: SubstrateInterface, channel_id: bytes) -> ChannelView:
        head = s.get_chain_finalised_head()
        block = s.get_block_header(block_hash=head)["header"]["number"]
        key = "0x" + channel_id.hex()

        def at(module: str, item: str, params: list[Any]) -> Any:
            return s.query(module, item, params, block_hash=head).value

        raw = at("ComputeChannel", "Channels", [key])
        if raw is None:
            return ChannelView(block=block, channel=None)
        policy = at("ComputeChannel", "ChannelDecodePolicies", [key])
        authored = at("ComputeChannel", "ChannelAuthoredOpenedAt", [key])
        snapshot = at("ComputeChannel", "ChannelCalibrationSnapshots", [key])
        accepted = at("HwCalibration", "BenchmarkBurstCaps", [raw["miner"]])
        lease = int(s.get_constant("HwCalibration", "CalibrationLeaseBlocks").value)
        current_cap = None
        if accepted is not None and block < int(accepted["accepted_at"]) + lease:
            current_cap = int(accepted["cap_gflops_per_sec"])
        return ChannelView(
            block=block,
            channel=Channel(
                state=raw["state"]
                if isinstance(raw["state"], str)
                else next(iter(raw["state"])),
                agent=raw["agent"],
                miner=raw["miner"],
                agent_key=_bytes(raw["agent_key"]),
                enclave_key=_bytes(raw["enclave_key"]),
                model_hash=_bytes(raw["model_hash"]),
                measured_root=_bytes(raw["measured_root"]),
                decode_policy_hash=_bytes(policy) if policy else b"",
                precision=PRECISIONS.index(raw["precision"]),
                escrow=int(raw["escrow"]),
                opened_at=int(raw["opened_at"]),
                max_duration=int(raw["sla"]["max_duration"]),
                authored_opened_at=int(
                    authored if authored is not None else raw["opened_at"]
                ),
                snapshot_cap=int(snapshot["cap_gflops_per_sec"]) if snapshot else None,
                current_cap=current_cap,
            ),
        )

    async def resolve_model_identity(
        self, model: str, *, advertised: ModelIdentity | None = None
    ) -> ModelIdentity:
        """The one verified registry entry `model` names; ambiguity is an error."""
        return await self._run(lambda s: self._resolve_identity(s, model, advertised))

    def _resolve_identity(
        self, s: SubstrateInterface, model: str, advertised: ModelIdentity | None
    ) -> ModelIdentity:
        head = s.get_chain_finalised_head()
        wanted = model.lower()
        candidates: list[ModelIdentity] = []
        for storage_key, entry in s.query_map(
            "ModelRegistry", "Models", block_hash=head
        ):
            value = entry.value
            if value is None:
                continue
            model_hash = _bytes(storage_key.value)
            name = _text(value["model_name"])
            if advertised is not None:
                if model_hash != advertised.model_hash:
                    continue
            elif not (
                "0x" + model_hash.hex() == wanted
                or name.lower() == wanted
                or name.split("/")[-1].lower() == wanted.split("/")[-1]
            ):
                continue
            if _variant(value["status"]) != "Verified":
                continue
            key = "0x" + model_hash.hex()
            policies = s.query(
                "ModelRegistry", "AllowedDecodePolicies", [key], block_hash=head
            ).value
            if not policies:
                continue
            allowed = [_bytes(p) for p in policies]
            if advertised is not None:
                policy = advertised.decode_policy_hash
            else:
                policy = allowed[0] if len(allowed) == 1 else None
            if policy is None or policy not in allowed:
                continue
            for precision_name in value["supported_precisions"]:
                code = PRECISIONS.index(_variant(precision_name))
                if advertised is not None and code != advertised.precision:
                    continue
                root = s.query(
                    "ModelRegistry",
                    "MeasuredRoots",
                    [key, PRECISIONS[code]],
                    block_hash=head,
                ).value
                if root is None:
                    continue
                measured_root = _bytes(root)
                if advertised is not None and measured_root != advertised.measured_root:
                    continue
                candidates.append(
                    ModelIdentity(
                        requested_slug=model,
                        registry_model=name,
                        registry_version=_text(value["version"]),
                        model_hash=model_hash,
                        precision=code,
                        decode_policy_hash=policy,
                        measured_root=measured_root,
                    )
                )
        if len(candidates) != 1:
            raise FlopChainError(
                f"{model!r} does not name exactly one verified model on FLOP "
                f"({len(candidates)} matches)"
            )
        return candidates[0]

    # ---- transactions ---------------------------------------------------------------

    async def submit(
        self, module: str, function: str, args: dict[str, Any], signer: FlopSigner
    ) -> Submitted:
        call, nonce, payload = await self._run(
            lambda s: self._prepare(s, module, function, args, signer.address)
        )
        signature = await signer.sign_payload(payload)
        return await asyncio.to_thread(
            self._send, call, nonce, signature, signer.account_id
        )

    def _prepare(
        self,
        s: SubstrateInterface,
        module: str,
        function: str,
        args: dict[str, Any],
        address: str,
    ) -> tuple[Any, int, bytes]:
        call = s.compose_call(module, function, args)
        nonce = s.get_account_nonce(address)
        return call, nonce, self._unsigned_payload(s, call, nonce)

    def _unsigned_payload(self, s: SubstrateInterface, call: Any, nonce: int) -> bytes:
        """The full payload, unhashed, so the backend can check what it is asked to sign."""
        payload = (
            bytes(call.data.data)
            + bytes.fromhex(IMMORTAL_ERA)
            + _compact(nonce)
            + _compact(0)
            + b"\x00"
            + u32le(s.runtime_version)
            + u32le(s.transaction_version)
            + self.genesis_hash
            + self.genesis_hash
            + b"\x00"
        )
        reference = bytes(
            s.generate_signature_payload(call=call, era=IMMORTAL_ERA, nonce=nonce).data
        )
        expected = payload if len(payload) <= 256 else blake2_256(payload)
        if reference != expected:
            raise FlopChainError(
                "the FLOP runtime changed its transaction format; update the SDK"
            )
        return payload

    def _send(
        self, call: Any, nonce: int, signature: bytes, account_id: bytes
    ) -> Submitted:
        # The RPC drops idle sockets, and signing can take a moment: make sure the
        # connection is alive before the one call that must not be sent twice.
        s = self._retrying(lambda live: live.rpc_request("system_health", []) and live)
        extrinsic = s.runtime_config.create_scale_object(
            type_string="Extrinsic", metadata=s.metadata
        )
        extrinsic.encode(
            {
                "account_id": "0x" + account_id.hex(),
                "signature": "0x" + signature.hex(),
                "call_function": call.value["call_function"],
                "call_module": call.value["call_module"],
                "call_args": call.value["call_args"],
                "nonce": nonce,
                "era": IMMORTAL_ERA,
                "tip": 0,
                "asset_id": {"tip": 0, "asset_id": None},
                "mode": "Disabled",
                "signature_version": ECDSA_SIGNATURE_VERSION,
            }
        )
        receipt = s.submit_extrinsic(extrinsic, wait_for_inclusion=True)
        if not receipt.is_success:
            error = receipt.error_message or {}
            raise FlopChainError(
                f"{call.value['call_module']}.{call.value['call_function']} failed: "
                f"{error.get('name', 'unknown error')}"
            )
        return Submitted(
            extrinsic_hash=receipt.extrinsic_hash,
            block_hash=receipt.block_hash,
            block_number=s.get_block_number(receipt.block_hash),
            fee=int(receipt.total_fee_amount or 0),
            events=[e.value["event"] for e in receipt.triggered_events],
        )

    async def events_at(self, block_number: int) -> list[dict[str, Any]]:
        def read(s: SubstrateInterface) -> list[dict[str, Any]]:
            block_hash = s.get_block_hash(block_number)
            return [
                {**e.value["event"], "extrinsic_idx": e.value.get("extrinsic_idx")}
                for e in s.get_events(block_hash)
            ]

        return await self._run(read)


def _text(value: Any) -> str:
    if isinstance(value, str):
        if value.startswith("0x"):
            return bytes.fromhex(value[2:]).decode()
        return value
    return bytes(value).decode()


def _variant(value: Any) -> str:
    return value if isinstance(value, str) else next(iter(value))
