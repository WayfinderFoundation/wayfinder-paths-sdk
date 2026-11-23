"""A direct session: one chat turn bought from a named miner by the user's own FLOP account.

Ported from the FLOP reference client's `runDirectSession` (force-open path). The account
escrows FLOP in a compute channel, the miner serves the turn over HTTPS, and nothing the
miner says is believed until its receipt verifies against the enclave key and the model
identity committed on chain. The channel is then settled, or the escrow recovered.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, NoReturn

import httpx
import sr25519

from wayfinder_paths.core.flop.account import account_id_from_address
from wayfinder_paths.core.flop.chain import (
    PRECISIONS,
    Channel,
    FlopChain,
    FlopChainError,
    Submitted,
)
from wayfinder_paths.core.flop.miner import (
    Checkpoint,
    MinerEndpoint,
    MinerRefusedError,
    MinerSession,
    MinerSettled,
    SettlementClaim,
    TurnAck,
    TurnReceipt,
    decode_text_turn_output,
    from_hex,
    signed_frames_offset,
    to_hex,
    wall_clock_ms,
)
from wayfinder_paths.core.flop.signer import FlopSigner
from wayfinder_paths.core.flop.wire import (
    ZERO_HASH,
    Leaf,
    ModelIdentity,
    blake2_256,
    force_settle_payable,
    merkle_path,
    merkle_root,
    receipt_message,
    turn_ack_message,
)

CHAT_TURN_VERSION = "flop.chat-turn.v1"
CHAT_ROLES = ("developer", "system", "user", "assistant", "tool")
# Bounds how long escrow can sit behind a miner that never produces output.
SESSION_LIFETIME_MS = 120_000
POLL_SECONDS = 1.0
FINALITY_WAIT_MS = 60_000
# The miner reads its own node, which the RPC pool serves while up to ~30 blocks behind
# ours, so it can refuse a channel we already see open. Its refusal says so; wait for it.
MINER_CATCH_UP_MS = 60_000
MINER_CATCH_UP_POLL_SECONDS = 2.0
RECONCILE_MS = 60_000
TIMING_PAD_MS = 250 + 100
TERMINAL_EVENTS = (
    "Settled",
    "TimedOutRefunded",
    "ForceOpenExpired",
    "AuditDataForfeited",
)


class ReceiptError(Exception):
    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind


class DirectSessionError(Exception):
    """The session failed after its channel was opened: FLOP may still be escrowed in
    `channel_id`. `recovery` is "refunded" or "force_settled" when this run got the
    escrow released or the verified work posted, and None when it could not."""

    def __init__(
        self, message: str, channel_id: bytes, *, recovery: str | None = None
    ) -> None:
        super().__init__(f"{message} (channel {to_hex(channel_id)})")
        self.channel_id = channel_id
        self.recovery = recovery


class _ReconciliationUnknown(Exception):
    pass


class _TerminalOutcome(Exception):
    pass


class SessionKey:
    """The channel's throwaway sr25519 key: it signs requests to the miner and co-signs
    receipts, and is committed on chain as the channel's `agent_key`."""

    def __init__(self, seed: bytes) -> None:
        if len(seed) != 32:
            raise ValueError("seed must be 32 bytes")
        self._pair = sr25519.pair_from_seed(seed)
        self.public_key: bytes = self._pair[0]

    @classmethod
    def generate(cls) -> SessionKey:
        return cls(os.urandom(32))

    def sign(self, message: bytes) -> bytes:
        return sr25519.sign(self._pair, message)


def canonical_json(value: Any) -> str:
    """Sorted keys, no whitespace, non-ASCII unescaped (the reference's `canonicalJson`)."""
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("chat turn object keys must be strings")
        return (
            "{"
            + ",".join(
                f"{json.dumps(key, ensure_ascii=False)}:{canonical_json(value[key])}"
                for key in sorted(value)
            )
            + "}"
        )
    if isinstance(value, list | tuple):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def encode_chat_turn(
    model: str, messages: list[dict[str, Any]], max_tokens: int
) -> bytes:
    if not isinstance(model, str) or not model:
        raise ValueError("chat turn model is required")
    if not isinstance(messages, list) or not messages:
        raise ValueError("chat turn messages are required")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in CHAT_ROLES:
            raise ValueError("chat turn message has an invalid role")
        if not isinstance(message.get("content", ...), str | list | None):
            raise ValueError("chat turn message content must be text, parts, or null")
    return canonical_json(
        {
            "version": CHAT_TURN_VERSION,
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
    ).encode()


def _timing(receipt: TurnReceipt, send_ms: int, recv_ms: int) -> tuple[str, int]:
    """The timing verdict the agent co-signs: (status, scored latency in ms)."""
    if recv_ms < send_ms:
        raise ReceiptError("TimingInvalid", "AgentClockReversed")
    if receipt.miner_done_ms < receipt.miner_recv_ms:
        raise ReceiptError("TimingInvalid", "MinerClockReversed")
    if receipt.latency_ms != receipt.miner_done_ms - receipt.miner_recv_ms:
        raise ReceiptError("TimingInvalid", "LatencyMismatch")
    observed = recv_ms - send_ms
    if observed + TIMING_PAD_MS < receipt.latency_ms:
        return "Ambiguous", receipt.latency_ms
    return "Eligible", min(observed, receipt.latency_ms + TIMING_PAD_MS)


class ReceiptVerifier:
    """The agent's copy of the transcript. A receipt is folded in and co-signed only if
    it verifies, so the root and aggregate it signs for are ones it rebuilt itself."""

    def __init__(
        self,
        *,
        channel_id: bytes,
        enclave_key: bytes,
        key: SessionKey,
        decode_policy_hash: bytes,
        measured_root: bytes,
        model_identity: ModelIdentity | None,
        escrow: int,
        base_per_turn: int,
    ) -> None:
        self._channel_id = channel_id
        self._enclave_key = enclave_key
        self._key = key
        self._decode_policy_hash = decode_policy_hash
        self._measured_root = measured_root
        self._model_identity = model_identity
        self._escrow = escrow
        self._base_per_turn = base_per_turn
        self._turns: list[TurnReceipt] = []
        self._leaves: list[bytes] = []
        self.aggregate_gn = 0

    @property
    def turns_verified(self) -> int:
        return len(self._turns)

    def root(self) -> bytes:
        return merkle_root(self._leaves)

    def accept(
        self,
        turn_input: bytes,
        receipt: TurnReceipt,
        agent_send_ms: int,
        agent_recv_ms: int,
    ) -> TurnAck:
        """Verify a receipt for the request bytes we sent, fold it in and co-sign it."""
        aggregate_gn = self.aggregate_gn + receipt.g_n
        # The pallet rejects a settlement whose tariff exceeds escrow, so a turn that
        # cannot be settled is refused before it is acknowledged.
        tariff = force_settle_payable(
            self._base_per_turn, len(self._turns) + 1, aggregate_gn
        )
        if tariff > self._escrow:
            raise ReceiptError("TariffExceedsEscrow", "turn tariff exceeds escrow")
        leaf_hash = self._verified_leaf(turn_input, receipt)
        timing_status, scored_latency_ms = _timing(
            receipt, agent_send_ms, agent_recv_ms
        )
        if receipt.g_n >= 1 << 64 or aggregate_gn >= 1 << 128:
            raise ReceiptError("MeterOverflow")
        if merkle_root([*self._leaves, leaf_hash]) != receipt.running_root:
            raise ReceiptError("RootMismatch")
        self._turns.append(receipt)
        self._leaves.append(leaf_hash)
        self.aggregate_gn = aggregate_gn
        return TurnAck(
            turn_index=receipt.turn_index,
            leaf_hash=leaf_hash,
            agent_send_ms=agent_send_ms,
            agent_recv_ms=agent_recv_ms,
            agent_sig=self._key.sign(
                turn_ack_message(
                    self._channel_id,
                    receipt.turn_index,
                    leaf_hash,
                    agent_send_ms,
                    agent_recv_ms,
                )
            ),
            timing_status=timing_status,
            scored_latency_ms=scored_latency_ms,
        )

    def _verified_leaf(self, turn_input: bytes, receipt: TurnReceipt) -> bytes:
        if receipt.turn_index != len(self._turns):
            raise ReceiptError(
                "UnexpectedTurnIndex",
                f"expected {len(self._turns)}, got {receipt.turn_index}",
            )
        pinned = any(self._decode_policy_hash)
        if pinned and receipt.decode_policy_hash != self._decode_policy_hash:
            raise ReceiptError(
                "DecodePolicyMismatch",
                "receipt decode policy does not match the channel-pinned policy",
            )
        h_in = blake2_256(turn_input)
        # The signature below already covers our own h_in; this also refuses a receipt
        # whose stated h_in differs, which would poison a later force_settle.
        if receipt.h_in != h_in:
            raise ReceiptError(
                "InputHashMismatch", "receipt does not commit to the request sent"
            )
        try:
            signed_frames_offset(receipt.output)
            expected_h_out = receipt.evidence_hash()
        except ValueError as error:
            raise ReceiptError("OutputHashMismatch", str(error)) from error
        if (
            self._model_identity is not None
            and receipt.model_identity != self._model_identity
        ):
            raise ReceiptError("ModelIdentityMismatch")
        if receipt.h_out != expected_h_out:
            raise ReceiptError("OutputHashMismatch")
        leaf = Leaf(
            version=receipt.leaf_version,
            channel_id=self._channel_id,
            turn_index=receipt.turn_index,
            h_in=h_in,
            h_out=receipt.h_out,
            g_n=receipt.g_n,
            decode_policy_hash=receipt.decode_policy_hash,
            h_ids=receipt.h_ids or ZERO_HASH,
            toploc_commitment_hash=receipt.toploc_commitment_hash or ZERO_HASH,
            miner_recv_ms=receipt.miner_recv_ms,
            miner_done_ms=receipt.miner_done_ms,
            latency_ms=receipt.latency_ms,
        )
        if not leaf.fields_consistent():
            raise ReceiptError("BadEnclaveSignature", "fields do not fit leaf version")
        try:
            leaf_hash = leaf.hash()
            signed = sr25519.verify(receipt.enclave_sig, leaf_hash, self._enclave_key)
        except ValueError as error:
            raise ReceiptError("BadEnclaveSignature", str(error)) from error
        if not signed:
            raise ReceiptError("BadEnclaveSignature")
        return leaf_hash

    def _claim(self, payable: int) -> SettlementClaim:
        final_root = self.root()
        return SettlementClaim(
            channel_id=self._channel_id,
            final_root=final_root,
            aggregate_gn=self.aggregate_gn,
            payable=payable,
            agent_receipt_sig=self._key.sign(
                receipt_message(
                    self._channel_id, final_root, self.aggregate_gn, payable
                )
            ),
            claimed_measured_root=self._measured_root,
        )

    def cooperative_claim(self) -> SettlementClaim:
        """Co-sign a cooperative `settle`, which pays out the whole reserved escrow."""
        return self._claim(self._escrow)

    def force_claim(self) -> SettlementClaim:
        """Co-sign the verified turns at the force-settle tariff."""
        return self._claim(
            force_settle_payable(
                self._base_per_turn, len(self._turns), self.aggregate_gn
            )
        )

    def checkpoint(self) -> Checkpoint:
        claim = self.force_claim()
        return Checkpoint(
            final_root=claim.final_root,
            aggregate_gn=claim.aggregate_gn,
            payable=claim.payable,
            through_turn=len(self._turns) - 1,
            agent_receipt_sig=claim.agent_receipt_sig,
        )

    def verified_turns(self) -> list[dict[str, Any]]:
        """The `VerifiedTurn` proofs `force_settle` takes, one per verified turn."""
        return [
            {
                "leaf_version": receipt.leaf_version.name,
                "turn_index": receipt.turn_index,
                "h_in": to_hex(receipt.h_in),
                "h_out": to_hex(receipt.h_out),
                "g_n": receipt.g_n,
                "decode_policy_hash": to_hex(receipt.decode_policy_hash or ZERO_HASH),
                "h_ids": to_hex(receipt.h_ids or ZERO_HASH),
                "toploc_commitment_hash": to_hex(
                    receipt.toploc_commitment_hash or ZERO_HASH
                ),
                "miner_recv_ms": receipt.miner_recv_ms,
                "miner_done_ms": receipt.miner_done_ms,
                "latency_ms": receipt.latency_ms,
                "enclave_sig": to_hex(receipt.enclave_sig),
                "merkle_path": [
                    (to_hex(sibling), sibling_is_left)
                    for sibling, sibling_is_left in merkle_path(self._leaves, index)
                ],
            }
            for index, receipt in enumerate(self._turns)
        ]


@dataclass(frozen=True)
class DirectSessionResult:
    """`settlement` is "settled" when the miner settled cooperatively, which pays the
    whole escrow (`payable == escrow`), and "posted" when this account force-settled:
    then `payable` is the tariff for the verified work, and the rest of the escrow
    returns only when someone calls `finalize` after block `challenge_end`.

    `settle_tx` of a cooperative settlement is the miner's own report; what was checked
    is the finalized event at `settlement_block`."""

    reply: str
    receipts: list[TurnReceipt]
    miner: str
    channel_id: str
    settlement: Literal["settled", "posted"]
    settle_tx: str | None
    settlement_block: int
    escrow: int
    aggregate_gn: int
    payable: int
    miner_paid: int | None = None
    burned: int | None = None
    challenge_end: int | None = None


@dataclass(frozen=True)
class _Settlement:
    kind: Literal["settled", "posted"]
    tx: str | None
    block: int
    payable: int
    miner_paid: int | None = None
    burned: int | None = None
    challenge_end: int | None = None


def _registry_identity(identity: ModelIdentity) -> tuple[Any, ...]:
    """Everything the chain's registry fixes. The backend model name is the miner's
    own label: it is taken as advertised and then pinned on every receipt."""
    return (
        identity.registry_model,
        identity.registry_version,
        identity.model_hash,
        identity.measured_root,
        identity.decode_policy_hash,
        identity.precision,
    )


def _is_channel_event(
    event: dict[str, Any], channel_id: bytes, names: tuple[str, ...]
) -> bool:
    if event["module_id"] != "ComputeChannel" or event["event_id"] not in names:
        return False
    attributes = event["attributes"]
    subject = attributes.get("channel_id") if isinstance(attributes, dict) else None
    return isinstance(subject, str) and subject.lower() == to_hex(channel_id)


class _DirectSession:
    """Everything after the channel exists on chain. A channel opened from a signed
    offer would enter here with `expected_enclave_key` set and an open event that
    carries no ack deadline."""

    def __init__(
        self,
        *,
        chain: FlopChain,
        signer: FlopSigner,
        channel_id: bytes,
        miner: str,
        model: str,
        identity: ModelIdentity,
        key: SessionKey,
        escrow: int,
        base_per_turn: int,
        endpoint: MinerEndpoint,
        expected_enclave_key: bytes | None,
        sleep: Callable[[float], Awaitable[None]],
        now_ms: Callable[[], int],
    ) -> None:
        self.chain = chain
        self.signer = signer
        self.channel_id = channel_id
        self.miner = miner
        self.model = model
        self.identity = identity
        self.key = key
        self.escrow = escrow
        self.base_per_turn = base_per_turn
        self.expected_enclave_key = expected_enclave_key
        self.sleep = sleep
        self.now_ms = now_ms
        self.miner_session: MinerSession = endpoint.session(
            channel_id, key, now_ms=now_ms
        )
        self.ack_deadline: int | None = None
        self.verifier: ReceiptVerifier | None = None
        self.force_outcome_unknown = False
        self.recovery: str | None = None

    # ---- chain ----------------------------------------------------------------------

    async def _finalized_event(
        self, names: tuple[str, ...], from_block: int, wait_ms: int
    ) -> tuple[int, dict[str, Any]] | None:
        """The first event for this channel in finalized blocks from `from_block` on."""
        deadline = self.now_ms() + wait_ms
        next_block = from_block
        while True:
            head = await self.chain.finalized_block()
            while next_block <= head:
                for event in await self.chain.events_at(next_block):
                    if _is_channel_event(event, self.channel_id, names):
                        return next_block, event
                next_block += 1
            if self.now_ms() >= deadline:
                return None
            await self.sleep(POLL_SECONDS)

    async def _confirmed(
        self, submitted: Submitted, event_name: str
    ) -> tuple[int, dict[str, Any]]:
        """`chain.submit` returns at inclusion, and an included block can still be
        retracted. Channel state is read at the finalized head, so each call is
        followed to its finalized event (block, attributes) before anything is
        decided on it."""
        found = await self._finalized_event(
            (event_name,), submitted.block_number, FINALITY_WAIT_MS
        )
        if found is None:
            raise RuntimeError(
                f"ComputeChannel.{event_name} from block {submitted.block_number} "
                f"was not finalized within {FINALITY_WAIT_MS // 1000} seconds"
            )
        return found[0], found[1]["attributes"]

    async def _submit(
        self, function: str, event_name: str, args: dict[str, Any] | None = None
    ) -> tuple[Submitted, int, dict[str, Any]]:
        submitted = await self.chain.submit(
            "ComputeChannel",
            function,
            {"channel_id": to_hex(self.channel_id), **(args or {})},
            self.signer,
        )
        return submitted, *await self._confirmed(submitted, event_name)

    async def _refund(self) -> None:
        """A channel with no verified turn cannot be settled: wait for its on-chain
        deadline and take the escrow back."""
        while True:
            view = await self.chain.channel(self.channel_id)
            channel = view.channel
            if channel is None:
                raise RuntimeError("opened channel disappeared from finalized state")
            if channel.state == "Closed":
                return
            if (
                channel.state == "PendingAck"
                and self.ack_deadline is not None
                and view.block > self.ack_deadline
            ):
                await self._submit("expire_force_open", "ForceOpenExpired")
                self.recovery = "refunded"
                return
            if (
                channel.state == "Open"
                and view.block > channel.opened_at + channel.max_duration
            ):
                await self._submit("timeout", "TimedOutRefunded")
                self.recovery = "refunded"
                return
            if channel.state not in ("PendingAck", "Open"):
                raise RuntimeError(f"cannot refund channel in {channel.state}")
            await self.sleep(POLL_SECONDS)

    async def _wait_for_settlement(self, verifier: ReceiptVerifier) -> int:
        """Block until the pallet's calibrated work allowance admits the verified work;
        returns the finalized block from which a settlement can appear."""
        gn = verifier.aggregate_gn
        while True:
            view = await self.chain.channel(self.channel_id)
            channel = view.channel
            if channel is None or channel.state != "Open":
                raise RuntimeError("cannot settle without a finalized Open channel")
            cap = (
                0
                if channel.snapshot_cap is None or channel.current_cap is None
                else min(channel.snapshot_cap, channel.current_cap)
            )
            expired = view.block > channel.opened_at + channel.max_duration
            if expired or (gn > 0 and (cap <= 0 or gn > cap * channel.max_duration)):
                await self._refund()
                raise RuntimeError(
                    "channel refunded: verified work cannot settle within its "
                    "calibrated lifetime"
                )
            elapsed = (
                view.block - channel.authored_opened_at
                if view.block > channel.authored_opened_at
                else 1
            )
            if gn <= cap * min(elapsed, channel.max_duration):
                return view.block
            await self.sleep(POLL_SECONDS)

    async def _force_close(self, verifier: ReceiptVerifier) -> _Settlement:
        await self._wait_for_settlement(verifier)
        try:
            claim = verifier.force_claim()
            submitted, block, posted = await self._submit(
                "force_settle",
                "SettlementPosted",
                {
                    "final_root": to_hex(claim.final_root),
                    "aggregate_gn": claim.aggregate_gn,
                    "claimed_measured_root": to_hex(claim.claimed_measured_root),
                    "agent_receipt_sig": to_hex(claim.agent_receipt_sig),
                    "transcript_ref": None,
                    "verified_turns": verifier.verified_turns(),
                },
            )
        except Exception:
            self.force_outcome_unknown = True
            raise
        return _Settlement(
            kind="posted",
            tx=submitted.extrinsic_hash,
            block=block,
            payable=claim.payable,
            challenge_end=int(posted["challenge_end"]),
        )

    async def _cooperative_close(
        self, verifier: ReceiptVerifier, scan_from: int
    ) -> _Settlement:
        claim = verifier.cooperative_claim()
        told: MinerSettled | None = None
        delivery_error: Exception | None = None
        try:
            told = await self.miner_session.close(claim)
        except Exception as error:
            delivery_error = error
        if told is None and delivery_error is None:
            raise RuntimeError("miner has no cooperative chain submitter")
        # A failed delivery proves nothing: the miner may hold the claim and settle.
        # Only the finalized chain says what happened to the channel.
        try:
            found = await self._finalized_event(
                TERMINAL_EVENTS, scan_from, RECONCILE_MS
            )
            if found is not None:
                return self._settled(claim, told, *found)
        except _TerminalOutcome:
            raise
        except Exception as scan_error:
            errors = [e for e in (delivery_error, scan_error) if e is not None]
            raise _ReconciliationUnknown(
                "cooperative close and reconciliation failed"
            ) from ExceptionGroup("cooperative close and reconciliation", errors)
        raise _ReconciliationUnknown(
            f"cooperative settlement was not finalized within {RECONCILE_MS // 1000} "
            "seconds"
        ) from delivery_error

    def _settled(
        self,
        claim: SettlementClaim,
        told: MinerSettled | None,
        block: int,
        event: dict[str, Any],
    ) -> _Settlement:
        if event["event_id"] != "Settled":
            raise _TerminalOutcome(
                f"channel terminal outcome: {event['event_id']} (block {block}, "
                f"extrinsic {event.get('extrinsic_idx')})"
            )
        attributes = event["attributes"]
        if (
            int(attributes["aggregate_gn"]) != claim.aggregate_gn
            or attributes["cooperative"] is not True
        ):
            raise RuntimeError("finalized settlement does not match the verified claim")
        return _Settlement(
            kind="settled",
            tx=told.tx_hash if told is not None else None,
            block=block,
            payable=claim.payable,
            miner_paid=int(attributes["miner_paid"]),
            burned=int(attributes["burned"]),
        )

    # ---- flow -----------------------------------------------------------------------

    async def _open_with_miner(self) -> None:
        """Hand the miner the session. Until its node sees the open channel it answers
        400 "channel not found on-chain"; that is retried for MINER_CATCH_UP_MS, and
        any other refusal fails at once (the caller then recovers the escrow)."""
        deadline = self.now_ms() + MINER_CATCH_UP_MS
        while True:
            try:
                await self.miner_session.open(self.key.public_key)
                return
            except MinerRefusedError as refused:
                if not refused.channel_not_seen_yet or self.now_ms() >= deadline:
                    raise
            await self.sleep(MINER_CATCH_UP_POLL_SECONDS)

    async def _wait_open(self) -> Channel:
        while True:
            view = await self.chain.channel(self.channel_id)
            channel = view.channel
            if channel is None:
                raise RuntimeError("opened channel missing from finalized state")
            if channel.state == "Open":
                return channel
            if channel.state != "PendingAck":
                raise RuntimeError(f"unexpected channel state {channel.state}")
            if self.ack_deadline is not None and view.block > self.ack_deadline:
                raise RuntimeError("miner acknowledgement timed out")
            await self.sleep(POLL_SECONDS)

    def _check_channel(self, opened: Channel) -> None:
        identity = self.identity
        if (
            account_id_from_address(opened.miner) != account_id_from_address(self.miner)
            or opened.agent_key != self.key.public_key
            or opened.model_hash != identity.model_hash
            or opened.measured_root != identity.measured_root
            or opened.decode_policy_hash != identity.decode_policy_hash
            or opened.precision != identity.precision
            or opened.escrow != self.escrow
        ):
            raise RuntimeError("finalized channel identity mismatch")
        if (
            self.expected_enclave_key is not None
            and opened.enclave_key != self.expected_enclave_key
        ):
            raise RuntimeError("offer enclave key mismatch")

    async def _check_miner(self, opened: Channel) -> ModelIdentity:
        """What the miner says it serves must be what the finalized channel committed."""
        info = await self.miner_session.info()
        if (
            account_id_from_address(info.miner_address)
            != account_id_from_address(self.miner)
            or info.enclave_key != opened.enclave_key
        ):
            raise RuntimeError("miner /session/info enclave key or miner mismatch")
        advertised = info.model_identity
        if (
            advertised is None
            or not info.is_ready
            or advertised.requested_slug != self.model
            or _registry_identity(advertised) != _registry_identity(self.identity)
        ):
            raise RuntimeError(
                "miner /session/info model identity mismatch or not ready"
            )
        return advertised

    async def run(
        self, opened_by: Submitted, open_event: str, turn_input: bytes
    ) -> DirectSessionResult:
        try:
            return await self._run(opened_by, open_event, turn_input)
        except Exception as cause:
            await self._fail(cause)

    async def _run(
        self, opened_by: Submitted, open_event: str, turn_input: bytes
    ) -> DirectSessionResult:
        _, requested = await self._confirmed(opened_by, open_event)
        if "ack_deadline" in requested:
            self.ack_deadline = int(requested["ack_deadline"])
        opened = await self._wait_open()
        self._check_channel(opened)
        advertised = await self._check_miner(opened)
        verifier = self.verifier = ReceiptVerifier(
            channel_id=self.channel_id,
            enclave_key=opened.enclave_key,
            key=self.key,
            decode_policy_hash=self.identity.decode_policy_hash,
            measured_root=self.identity.measured_root,
            model_identity=advertised,
            escrow=self.escrow,
            base_per_turn=self.base_per_turn,
        )
        await self._open_with_miner()

        agent_send_ms = self.now_ms()
        receipt = await self.miner_session.turn(turn_input)
        ack = verifier.accept(turn_input, receipt, agent_send_ms, self.now_ms())
        await self.miner_session.ack(ack)
        await self.miner_session.checkpoint(verifier.checkpoint())

        scan_from = await self._wait_for_settlement(verifier)
        try:
            settlement = await self._cooperative_close(verifier, scan_from)
        except (_ReconciliationUnknown, _TerminalOutcome):
            raise
        except Exception as close_error:
            try:
                view = await self.chain.channel(self.channel_id)
            except Exception as snapshot_error:
                raise ExceptionGroup(
                    "cooperative close and finalized state query failed",
                    [close_error, snapshot_error],
                ) from None
            if view.channel is None or view.channel.state != "Open":
                raise
            try:
                settlement = await self._force_close(verifier)
            except Exception as fallback_error:
                raise ExceptionGroup(
                    f"cooperative close and fallback failed: {fallback_error}",
                    [close_error, fallback_error],
                ) from None
        return DirectSessionResult(
            reply=decode_text_turn_output(receipt.output).decode(errors="replace"),
            receipts=[receipt],
            miner=self.miner,
            channel_id=to_hex(self.channel_id),
            settlement=settlement.kind,
            settle_tx=settlement.tx,
            settlement_block=settlement.block,
            escrow=self.escrow,
            aggregate_gn=verifier.aggregate_gn,
            payable=settlement.payable,
            miner_paid=settlement.miner_paid,
            burned=settlement.burned,
            challenge_end=settlement.challenge_end,
        )

    async def _fail(self, cause: Exception) -> NoReturn:
        """Never leave a failure behind a live channel without trying to recover it."""
        if self.force_outcome_unknown:
            raise DirectSessionError(
                "force settlement submission outcome unknown; inspect finalized "
                "channel before recovery",
                self.channel_id,
            ) from cause
        if isinstance(cause, _ReconciliationUnknown):
            raise DirectSessionError(
                "cooperative settlement outcome unknown; inspect finalized history "
                "before recovery",
                self.channel_id,
            ) from cause
        if isinstance(cause, _TerminalOutcome):
            raise DirectSessionError(str(cause), self.channel_id) from cause
        try:
            verifier = self.verifier
            if verifier is not None and verifier.turns_verified > 0:
                view = await self.chain.channel(self.channel_id)
                channel = view.channel
                if (
                    channel is not None
                    and channel.state == "Open"
                    and view.block > channel.opened_at + channel.max_duration
                ):
                    await self._refund()
                elif channel is None or channel.state != "Closed":
                    await self._force_close(verifier)
                    self.recovery = "force_settled"
            else:
                await self._refund()
        except Exception as recovery_error:
            raise DirectSessionError(
                "direct session failed; on-chain recovery failed", self.channel_id
            ) from ExceptionGroup(
                "session and recovery errors", [cause, recovery_error]
            )
        raise DirectSessionError(
            str(cause) or type(cause).__name__, self.channel_id, recovery=self.recovery
        ) from cause


async def run_direct_session(
    *,
    chain: FlopChain,
    signer: FlopSigner,
    miner: str,
    model: str,
    endpoint: str,
    max_flop_raw: int,
    messages: list[dict[str, Any]] | None = None,
    prompt: str | None = None,
    max_tokens: int = 256,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now_ms: Callable[[], int] = wall_clock_ms,
    transport: httpx.AsyncBaseTransport | None = None,
) -> DirectSessionResult:
    """Run one chat turn (a prompt or a whole message history) on exactly the named
    miner, paid from `signer`'s account.

    `max_flop_raw` is escrowed in full. A cooperative close pays all of it to the
    miner; only a forced settlement prices the turn at the tariff and returns the rest.
    """
    if (prompt is None) == (messages is None):
        raise ValueError("supply exactly one of prompt or messages")
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or max_tokens <= 0
    ):
        raise ValueError("max_tokens must be a positive integer")
    if max_flop_raw <= 0:
        raise ValueError("max_flop_raw must be positive raw FLOP units")
    # Everything that can be refused without the chain is refused before FLOP is escrowed.
    turn_input = encode_chat_turn(
        model,
        messages if messages is not None else [{"role": "user", "content": prompt}],
        max_tokens,
    )
    miner_endpoint = MinerEndpoint(endpoint, transport=transport)
    account_id_from_address(miner)

    identity = await chain.resolve_model_identity(model)
    block_ms = await chain.constant("Babe", "ExpectedBlockTime")
    if block_ms <= 0:
        raise FlopChainError("invalid chain block time")
    base_per_turn = await chain.constant("ComputeChannel", "BasePerTurn")
    key = SessionKey.generate()

    opened_by = await chain.submit(
        "ComputeChannel",
        "force_open",
        {
            "miner": miner,
            "model_hash": to_hex(identity.model_hash),
            "measured_root": to_hex(identity.measured_root),
            "decode_policy_hash": to_hex(identity.decode_policy_hash),
            "precision": PRECISIONS[identity.precision],
            "agent_key": to_hex(key.public_key),
            "sla": {
                "max_turns": 1,
                "max_duration": -(-SESSION_LIFETIME_MS // block_ms),
                "reattest_every": 1,
            },
            "escrow": max_flop_raw,
            "nonce": int.from_bytes(os.urandom(8), "big"),
        },
        signer,
    )
    requested = opened_by.event("ComputeChannel", "ForceOpenRequested")
    if requested is None:
        raise FlopChainError("force_open emitted no ComputeChannel.ForceOpenRequested")
    session = _DirectSession(
        chain=chain,
        signer=signer,
        channel_id=from_hex(requested["channel_id"], 32),
        miner=miner,
        model=model,
        identity=identity,
        key=key,
        escrow=max_flop_raw,
        base_per_turn=base_per_turn,
        endpoint=miner_endpoint,
        expected_enclave_key=None,
        sleep=sleep,
        now_ms=now_ms,
    )
    return await session.run(opened_by, "ForceOpenRequested", turn_input)
