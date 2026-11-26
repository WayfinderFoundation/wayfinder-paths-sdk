"""Canonical compute-channel encodings (FLOP yellow paper, Appendix F.3)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import IntEnum

CHANNEL_ID_DOMAIN = b"FLOP/COMPUTE_CHANNEL/ID"
RECEIPT_DOMAIN = b"FLOP/COMPUTE_CHANNEL/RECEIPT"
TURN_EVIDENCE_DOMAIN = b"FLOP_CHAT_TURN_EVIDENCE_V1"
SESSION_REQUEST_DOMAIN = "flop-session-request-v1"
ZERO_HASH = bytes(32)


class LeafVersion(IntEnum):
    V0 = 0
    V1 = 1
    V2 = 2
    V3 = 3


def blake2_256(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=32).digest()


def _uint_le(value: int, size: int, name: str) -> bytes:
    if value < 0 or value >= 1 << (8 * size):
        raise ValueError(f"{name} does not fit u{8 * size}")
    return value.to_bytes(size, "little")


def u32le(value: int) -> bytes:
    return _uint_le(value, 4, "value")


def u64le(value: int) -> bytes:
    return _uint_le(value, 8, "value")


def u128le(value: int) -> bytes:
    return _uint_le(value, 16, "value")


def _hash32(value: bytes, name: str) -> bytes:
    if len(value) != 32:
        raise ValueError(f"{name} must be 32 bytes")
    return value


def channel_id(genesis_hash: bytes, agent: bytes, miner: bytes, nonce: int) -> bytes:
    return blake2_256(
        CHANNEL_ID_DOMAIN
        + b"\x01"
        + _hash32(genesis_hash, "genesis_hash")
        + _hash32(agent, "agent")
        + _hash32(miner, "miner")
        + u64le(nonce)
    )


def h_ids(prompt_ids: list[int], generated_ids: list[int]) -> bytes:
    return blake2_256(
        u32le(len(prompt_ids))
        + b"".join(u32le(i) for i in prompt_ids)
        + u32le(len(generated_ids))
        + b"".join(u32le(i) for i in generated_ids)
    )


@dataclass(frozen=True)
class Leaf:
    """One turn of the transcript as the miner's enclave key signed it."""

    version: LeafVersion
    channel_id: bytes
    turn_index: int
    h_in: bytes
    h_out: bytes
    g_n: int
    decode_policy_hash: bytes | None = None
    h_ids: bytes = ZERO_HASH
    toploc_commitment_hash: bytes = ZERO_HASH
    miner_recv_ms: int = 0
    miner_done_ms: int = 0
    latency_ms: int = 0

    def fields_consistent(self) -> bool:
        has_v3 = any(self.h_ids)
        has_toploc = any(self.toploc_commitment_hash)
        if self.version in (LeafVersion.V0, LeafVersion.V1):
            return self.decode_policy_hash is None and not has_v3 and not has_toploc
        if self.version is LeafVersion.V2:
            return self.decode_policy_hash is not None and not has_v3 and not has_toploc
        return self.decode_policy_hash is not None and has_v3

    def preimage(self) -> bytes:
        head = (
            _hash32(self.channel_id, "channel_id")
            + u32le(self.turn_index)
            + _hash32(self.h_in, "h_in")
            + _hash32(self.h_out, "h_out")
            + u128le(self.g_n)
        )
        timing = (
            u64le(self.miner_recv_ms)
            + u64le(self.miner_done_ms)
            + u64le(self.latency_ms)
        )
        if self.version is LeafVersion.V0:
            return head
        if self.version is LeafVersion.V1:
            return head + timing
        policy = _hash32(self.decode_policy_hash or b"", "decode_policy_hash")
        if self.version is LeafVersion.V2:
            return head + policy + timing
        return (
            head
            + policy
            + _hash32(self.h_ids, "h_ids")
            + _hash32(self.toploc_commitment_hash, "toploc_commitment_hash")
            + timing
        )

    def hash(self) -> bytes:
        return blake2_256(self.preimage())


def turn_ack_message(
    channel: bytes, turn_index: int, leaf_hash: bytes, send_ms: int, recv_ms: int
) -> bytes:
    return (
        _hash32(channel, "channel_id")
        + u32le(turn_index)
        + _hash32(leaf_hash, "leaf_hash")
        + u64le(send_ms)
        + u64le(recv_ms)
    )


def receipt_message(
    channel: bytes, final_root: bytes, aggregate_gn: int, payable: int
) -> bytes:
    return (
        RECEIPT_DOMAIN
        + b"\x01"
        + _hash32(channel, "channel_id")
        + _hash32(final_root, "final_root")
        + u128le(aggregate_gn)
        + u128le(payable)
    )


def force_settle_payable(base_per_turn: int, turn_count: int, aggregate_gn: int) -> int:
    return base_per_turn * turn_count + aggregate_gn


def _hash_pair(left: bytes, right: bytes) -> bytes:
    return blake2_256(left + right)


def merkle_root(leaves: list[bytes]) -> bytes:
    if not leaves:
        return ZERO_HASH
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [_hash_pair(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def merkle_path(leaves: list[bytes], index: int) -> list[tuple[bytes, bool]]:
    """Siblings from leaf to root as (hash, sibling_is_left)."""
    if not 0 <= index < len(leaves):
        raise IndexError("leaf index out of range")
    path: list[tuple[bytes, bool]] = []
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        sibling = index ^ 1
        path.append((level[sibling], sibling < index))
        level = [_hash_pair(level[i], level[i + 1]) for i in range(0, len(level), 2)]
        index //= 2
    return path


def root_from_path(leaf: bytes, path: list[tuple[bytes, bool]]) -> bytes:
    node = leaf
    for sibling, sibling_is_left in path:
        node = (
            _hash_pair(sibling, node) if sibling_is_left else _hash_pair(node, sibling)
        )
    return node


def _length_prefixed(value: bytes) -> bytes:
    return u32le(len(value)) + value


@dataclass(frozen=True)
class ModelIdentity:
    requested_slug: str
    registry_model: str
    registry_version: str
    model_hash: bytes
    precision: int
    decode_policy_hash: bytes
    measured_root: bytes
    backend_model_name: str = ""


def turn_evidence_hash(
    output: bytes,
    prompt_tokens: int,
    completion_tokens: int,
    finish_reason: str,
    identity: ModelIdentity,
) -> bytes:
    if not 0 <= identity.precision <= 255:
        raise ValueError("model precision must fit u8")
    return blake2_256(
        TURN_EVIDENCE_DOMAIN
        + _length_prefixed(output)
        + u64le(prompt_tokens)
        + u64le(completion_tokens)
        + _length_prefixed(finish_reason.encode())
        + _length_prefixed(identity.requested_slug.encode())
        + _length_prefixed(identity.registry_model.encode())
        + _length_prefixed(identity.registry_version.encode())
        + _hash32(identity.model_hash, "model_hash")
        + bytes([identity.precision])
        + _hash32(identity.decode_policy_hash, "decode_policy_hash")
        + _hash32(identity.measured_root, "measured_root")
        + _length_prefixed(identity.backend_model_name.encode())
    )


def session_request_message(
    channel_hex: str,
    method: str,
    path: str,
    body: str,
    timestamp: str,
    nonce_hex: str,
    challenge: str,
) -> bytes:
    """The exact bytes a miner checks on a channel-key signed HTTP request.

    The reference client builds this with JavaScript's JSON.stringify, so the
    separators must be compact and non-ASCII must stay unescaped.
    """
    body_hash = "0x" + blake2_256(body.encode()).hex()
    return json.dumps(
        [
            SESSION_REQUEST_DOMAIN,
            channel_hex,
            method,
            path,
            body_hash,
            timestamp,
            nonce_hex,
            challenge,
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
