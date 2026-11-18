"""The HTTP wire between a compute channel's agent and its miner.

Ported from the FLOP reference client (`transport-http`, `session-auth`). Every request is
authenticated with the channel's own session key and nothing else: no Wayfinder API key,
bearer token or cookie is ever sent to a miner, and nothing a miner returns is trusted
until `session.ReceiptVerifier` has checked it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from wayfinder_paths.core.flop.wire import (
    LeafVersion,
    ModelIdentity,
    blake2_256,
    session_request_message,
    turn_evidence_hash,
)

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
CHALLENGE_HEADER = "x-flop-session-challenge"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
PROBE_TIMEOUT_SECONDS = 10
REQUEST_TIMEOUT_SECONDS = 60
TURN_TIMEOUT_SECONDS = 60
ERROR_TEXT_CHARS = 300
TX_HASH = re.compile(r"0x[0-9a-fA-F]{64}")

SIGNED_FRAMES_FAMILY = b"FLOP-TURN-FRAMES-"
SIGNED_FRAMES_V1 = b"FLOP-TURN-FRAMES-V1\x00"
SIGNED_FRAMES_V2 = b"FLOP-TURN-FRAMES-V2\x00"
FRAME_KINDS = ("output", "tool_call")


class MinerError(Exception):
    pass


class MinerTimeoutError(MinerError):
    pass


class MinerTurnError(MinerError):
    """The miner reported a failed turn mid-stream. Its text is unsigned: diagnosis only."""


class RequestSigner(Protocol):
    def sign(self, message: bytes) -> bytes: ...


def wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


def to_hex(value: bytes) -> str:
    return "0x" + value.hex()


def from_hex(value: Any, size: int | None = None) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("expected 0x-prefixed hex")
    raw = bytes.fromhex(value[2:])
    if size is not None and len(raw) != size:
        raise ValueError(f"expected {size} bytes, got {len(raw)}")
    return raw


def _uint(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("expected an unsigned integer")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise ValueError("expected an unsigned integer")


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a string")
    return value


def compact_json(value: Any) -> str:
    """JSON as JavaScript's JSON.stringify writes it; the request signature covers it."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def validate_endpoint(endpoint: str) -> str:
    base_url = endpoint.strip().removesuffix("/")
    # URL parsers disagree about these, and the disagreement can move the host.
    if "\\" in base_url or any(c.isspace() for c in base_url):
        raise ValueError("miner endpoint must not contain whitespace or backslashes")
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if not host or parsed.port == 0:
        raise ValueError("miner endpoint must be an absolute URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("miner endpoint must not carry credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("miner endpoint must not carry a query or fragment")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and host in LOOPBACK_HOSTS
    ):
        raise ValueError("miner endpoint must use HTTPS or loopback HTTP")
    return base_url


# ---- receipts -----------------------------------------------------------------------


@dataclass(frozen=True)
class TurnReceipt:
    """A miner's claim about one turn, exactly as it arrived. Unverified."""

    leaf_version: LeafVersion
    turn_index: int
    h_in: bytes
    h_out: bytes
    output: bytes
    g_n: int
    miner_recv_ms: int
    miner_done_ms: int
    latency_ms: int
    enclave_sig: bytes
    running_root: bytes
    decode_policy_hash: bytes | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    model_identity: ModelIdentity | None = None
    h_ids: bytes | None = None
    toploc_commitment_hash: bytes | None = None

    def evidence_hash(self) -> bytes:
        """What `h_out` must be: token counts, finish reason and model identity are
        bound into it whenever the miner reports them (all four or none)."""
        if (
            self.prompt_tokens is not None
            and self.completion_tokens is not None
            and self.finish_reason is not None
            and self.model_identity is not None
        ):
            return turn_evidence_hash(
                self.output,
                self.prompt_tokens,
                self.completion_tokens,
                self.finish_reason,
                self.model_identity,
            )
        if (
            self.prompt_tokens is None
            and self.completion_tokens is None
            and self.finish_reason is None
            and self.model_identity is None
        ):
            return blake2_256(self.output)
        raise ValueError("partial provider evidence")


def encode_model_identity(identity: ModelIdentity) -> dict[str, Any]:
    return {
        "requestedSlug": identity.requested_slug,
        "registryModel": identity.registry_model,
        "registryVersion": identity.registry_version,
        "modelHash": to_hex(identity.model_hash),
        "precision": identity.precision,
        "decodePolicyHash": to_hex(identity.decode_policy_hash),
        "measuredRoot": to_hex(identity.measured_root),
        "backendModelName": identity.backend_model_name,
    }


def decode_model_identity(wire: Any) -> ModelIdentity:
    precision = wire["precision"]
    if isinstance(precision, bool) or not isinstance(precision, int):
        raise ValueError("model precision must be an integer")
    return ModelIdentity(
        requested_slug=_text(wire["requestedSlug"]),
        registry_model=_text(wire["registryModel"]),
        registry_version=_text(wire["registryVersion"]),
        model_hash=from_hex(wire["modelHash"], 32),
        precision=precision,
        decode_policy_hash=from_hex(wire["decodePolicyHash"], 32),
        measured_root=from_hex(wire["measuredRoot"], 32),
        # The reference hashes an absent name as the empty string.
        backend_model_name=_text(wire.get("backendModelName", "")),
    )


def encode_turn_receipt(receipt: TurnReceipt) -> dict[str, Any]:
    wire: dict[str, Any] = {
        "leafVersion": receipt.leaf_version.name,
        "turnIndex": receipt.turn_index,
        "hIn": to_hex(receipt.h_in),
        "hOut": to_hex(receipt.h_out),
        "output": to_hex(receipt.output),
        "gN": str(receipt.g_n),
        "minerRecvMs": str(receipt.miner_recv_ms),
        "minerDoneMs": str(receipt.miner_done_ms),
        "latencyMs": str(receipt.latency_ms),
        "enclaveSig": to_hex(receipt.enclave_sig),
        "runningRoot": to_hex(receipt.running_root),
    }
    if receipt.decode_policy_hash is not None:
        wire["decodePolicyHash"] = to_hex(receipt.decode_policy_hash)
    if receipt.prompt_tokens is not None:
        wire["promptTokens"] = str(receipt.prompt_tokens)
    if receipt.completion_tokens is not None:
        wire["completionTokens"] = str(receipt.completion_tokens)
    if receipt.finish_reason is not None:
        wire["finishReason"] = receipt.finish_reason
    if receipt.model_identity is not None:
        wire["modelIdentity"] = encode_model_identity(receipt.model_identity)
    if receipt.h_ids is not None:
        wire["hIds"] = to_hex(receipt.h_ids)
    if receipt.toploc_commitment_hash is not None:
        wire["toplocCommitmentHash"] = to_hex(receipt.toploc_commitment_hash)
    return wire


def decode_turn_receipt(wire: Any) -> TurnReceipt:
    try:
        return _decode_turn_receipt(wire)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise MinerError(f"malformed turn receipt: {error!r}") from error


def _decode_turn_receipt(wire: Any) -> TurnReceipt:
    version = wire["leafVersion"]
    if not isinstance(version, str) or version not in LeafVersion.__members__:
        raise ValueError(f"unsupported transcript leaf version: {version!r}")
    turn_index = wire["turnIndex"]
    if isinstance(turn_index, bool) or not isinstance(turn_index, int):
        raise ValueError("turn index must be an integer")

    def optional(key: str, decode: Callable[[Any], Any]) -> Any:
        return decode(wire[key]) if key in wire else None

    def optional_hash(key: str) -> bytes | None:
        return from_hex(wire[key], 32) if wire.get(key) else None

    return TurnReceipt(
        leaf_version=LeafVersion[version],
        turn_index=turn_index,
        h_in=from_hex(wire["hIn"], 32),
        h_out=from_hex(wire["hOut"], 32),
        output=from_hex(wire["output"]),
        g_n=_uint(wire["gN"]),
        miner_recv_ms=_uint(wire["minerRecvMs"]),
        miner_done_ms=_uint(wire["minerDoneMs"]),
        latency_ms=_uint(wire["latencyMs"]),
        enclave_sig=from_hex(wire["enclaveSig"], 64),
        running_root=from_hex(wire["runningRoot"], 32),
        decode_policy_hash=optional_hash("decodePolicyHash"),
        prompt_tokens=optional("promptTokens", _uint),
        completion_tokens=optional("completionTokens", _uint),
        finish_reason=optional("finishReason", _text),
        model_identity=optional("modelIdentity", decode_model_identity),
        h_ids=optional_hash("hIds"),
        toploc_commitment_hash=optional_hash("toplocCommitmentHash"),
    )


# ---- signed output frames -----------------------------------------------------------


def signed_frames_offset(output: bytes) -> int | None:
    """Where the signed frames start, or None for plain output bytes."""
    if output.startswith(SIGNED_FRAMES_V1):
        return len(SIGNED_FRAMES_V1)
    if output.startswith(SIGNED_FRAMES_V2):
        # V2 carries a rendering context that only a chat-turn v2 request may receive;
        # this client sends v1 requests, for which the reference rejects it too.
        raise ValueError("rendering context requires chat v2 input")
    if output.startswith(SIGNED_FRAMES_FAMILY):
        raise ValueError("unsupported signed output frame version")
    return None


def encode_signed_frames(frames: list[tuple[str, bytes]]) -> bytes:
    if not frames:
        raise ValueError("signed output frame sequence is empty")
    return SIGNED_FRAMES_V1 + b"".join(
        bytes([FRAME_KINDS.index(kind)]) + len(data).to_bytes(4, "big") + data
        for kind, data in frames
    )


def decode_signed_frames(output: bytes) -> list[tuple[str, bytes]]:
    offset = signed_frames_offset(output)
    if offset is None:
        raise ValueError("unsupported signed output frame version")
    frames: list[tuple[str, bytes]] = []
    while offset < len(output):
        if len(output) - offset < 5:
            raise ValueError("truncated signed output frame header")
        if output[offset] >= len(FRAME_KINDS):
            raise ValueError("unknown signed output frame kind")
        kind = FRAME_KINDS[output[offset]]
        length = int.from_bytes(output[offset + 1 : offset + 5], "big")
        offset += 5
        if length > len(output) - offset:
            raise ValueError("truncated signed output frame data")
        frames.append((kind, output[offset : offset + length]))
        offset += length
    if not frames:
        raise ValueError("signed output frame sequence is empty")
    return frames


def decode_text_turn_output(output: bytes) -> bytes:
    if signed_frames_offset(output) is None:
        return output
    frames = decode_signed_frames(output)
    if any(kind != "output" for kind, _ in frames):
        raise ValueError("text turn contains tool frames")
    return b"".join(data for _, data in frames)


class _TurnStream:
    """Strict collector for a streamed turn: chunks in sequence, then one terminal
    receipt whose output the chunks must rebuild byte for byte."""

    def __init__(self) -> None:
        self._parts: list[tuple[str, bytes]] = []
        self._terminal: TurnReceipt | None = None
        self._count = 0

    def add(self, line: bytes) -> None:
        frame = json.loads(line)
        if not isinstance(frame, dict):
            raise ValueError("turn frame is not an object")
        if frame.get("type") == "error":
            reason = " ".join(str(frame.get("error", "unspecified")).split())
            raise MinerTurnError(f"miner turn failed: {reason[:ERROR_TEXT_CHARS]}")
        if self._terminal is not None:
            raise ValueError("post-terminal frame")
        sequence = frame.get("sequence")
        if isinstance(sequence, bool) or sequence != self._count:
            raise ValueError(
                f"chunk sequence mismatch: expected {self._count}, got {sequence!r}"
            )
        self._count += 1
        if frame.get("type") == "chunk":
            if frame.get("kind") not in FRAME_KINDS:
                raise ValueError("unknown signed output frame kind")
            self._parts.append((frame["kind"], from_hex(frame["data"])))
        elif frame.get("type") == "terminal":
            self._terminal = decode_turn_receipt(frame["receipt"])
        else:
            raise ValueError(f"unknown turn frame type: {frame.get('type')!r}")

    def receipt(self) -> TurnReceipt:
        terminal = self._terminal
        if terminal is None:
            raise ValueError("missing terminal receipt")
        framed = signed_frames_offset(terminal.output) is not None
        has_tool_frame = any(kind == "tool_call" for kind, _ in self._parts)
        tool_turn = terminal.finish_reason == "tool_calls"
        if not framed and (has_tool_frame or tool_turn):
            raise ValueError("tool turn lacks signed frame encoding")
        if framed and tool_turn and not has_tool_frame:
            raise ValueError("tool turn lacks signed tool frames")
        output = (
            encode_signed_frames(self._parts)
            if framed
            else b"".join(data for _, data in self._parts)
        )
        if output != terminal.output:
            raise ValueError("chunk reconstruction does not match terminal output")
        if terminal.evidence_hash() != terminal.h_out:
            raise ValueError("terminal evidence hash mismatch")
        return terminal


# ---- session messages ---------------------------------------------------------------


@dataclass(frozen=True)
class MinerSessionInfo:
    miner_address: str
    enclave_key: bytes
    model_identity: ModelIdentity | None
    is_ready: bool


@dataclass(frozen=True)
class TurnAck:
    turn_index: int
    leaf_hash: bytes
    agent_send_ms: int
    agent_recv_ms: int
    agent_sig: bytes
    timing_status: str
    scored_latency_ms: int


@dataclass(frozen=True)
class Checkpoint:
    final_root: bytes
    aggregate_gn: int
    payable: int
    through_turn: int
    agent_receipt_sig: bytes


@dataclass(frozen=True)
class SettlementClaim:
    channel_id: bytes
    final_root: bytes
    aggregate_gn: int
    payable: int
    agent_receipt_sig: bytes
    claimed_measured_root: bytes


@dataclass(frozen=True)
class MinerSettled:
    """The miner's word that it submitted `settle`. Unverified: read the finalized event."""

    tx_hash: str | None


def _decode_info(wire: Any) -> MinerSessionInfo:
    identity = wire.get("modelIdentity")
    return MinerSessionInfo(
        miner_address=_text(wire["minerAddress"]),
        enclave_key=from_hex(wire["enclaveKey"], 32),
        model_identity=decode_model_identity(identity) if identity else None,
        is_ready=wire.get("is_ready") is True,
    )


def sign_session_request(
    key: RequestSigner,
    channel_id: bytes,
    method: str,
    path: str,
    body: str,
    challenge: str,
    *,
    timestamp_ms: int,
    nonce: bytes,
) -> dict[str, str]:
    channel = to_hex(channel_id)
    timestamp = str(timestamp_ms)
    nonce_hex = to_hex(nonce)
    message = session_request_message(
        channel, method, path, body, timestamp, nonce_hex, challenge
    )
    return {
        "x-flop-channel": channel,
        "x-flop-timestamp": timestamp,
        "x-flop-nonce": nonce_hex,
        "x-flop-challenge": challenge,
        "x-flop-signature": to_hex(key.sign(message)),
    }


def _error_text(body: bytes) -> str:
    return " ".join(body.decode(errors="replace").split())[:ERROR_TEXT_CHARS]


async def _read_capped(response: httpx.Response) -> bytes:
    body = bytearray()
    async with aclosing(response.aiter_bytes()) as chunks:
        async for chunk in chunks:
            body += chunk
            if len(body) > MAX_RESPONSE_BYTES:
                raise MinerError("miner response exceeds byte limit")
    return bytes(body)


def _refuse_unsafe(response: httpx.Response) -> None:
    if response.is_redirect or 300 <= response.status_code < 400:
        raise MinerError(f"{response.request.url} redirected; redirects are refused")
    # Refusing compression keeps the byte limit a limit on what is actually buffered.
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise MinerError("miner response uses an unsupported content encoding")


class MinerEndpoint:
    def __init__(
        self, endpoint: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.base_url = validate_endpoint(endpoint)
        self._transport = transport

    def client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=timeout,
            follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
        )

    def session(
        self,
        channel_id: bytes,
        key: RequestSigner,
        *,
        now_ms: Callable[[], int] = wall_clock_ms,
    ) -> MinerSession:
        return MinerSession(self, channel_id, key, now_ms=now_ms)


class MinerSession:
    """One channel's requests to its miner, each signed by the channel's session key."""

    def __init__(
        self,
        endpoint: MinerEndpoint,
        channel_id: bytes,
        key: RequestSigner,
        *,
        now_ms: Callable[[], int] = wall_clock_ms,
    ) -> None:
        self._endpoint = endpoint
        self._channel_id = channel_id
        self._key = key
        self._now_ms = now_ms
        self._challenge: str | None = None

    def _signed(self, method: str, path: str, body: str) -> dict[str, str]:
        if not self._challenge:
            raise MinerError(
                "miner did not advertise a session authentication challenge"
            )
        return sign_session_request(
            self._key,
            self._channel_id,
            method,
            path,
            body,
            self._challenge,
            timestamp_ms=self._now_ms(),
            nonce=os.urandom(16),
        )

    async def _exchange(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: str | None = None,
    ) -> tuple[httpx.Response, bytes]:
        url = self._endpoint.base_url + path
        content = body.encode() if body is not None else None
        try:
            async with (
                asyncio.timeout(REQUEST_TIMEOUT_SECONDS),
                self._endpoint.client(REQUEST_TIMEOUT_SECONDS) as client,
                client.stream(
                    method, url, headers=headers, content=content
                ) as response,
            ):
                _refuse_unsafe(response)
                return response, await _read_capped(response)
        except (TimeoutError, httpx.TimeoutException) as error:
            raise MinerTimeoutError(
                f"{url} timed out after {REQUEST_TIMEOUT_SECONDS}s"
            ) from error

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        url = self._endpoint.base_url + path
        body = compact_json(payload)
        headers = {
            "Content-Type": "application/json",
            # The reference signs the URL's whole path here, including any base prefix.
            **self._signed("POST", urlparse(url).path, body),
        }
        response, raw = await self._exchange("POST", path, headers=headers, body=body)
        if not response.is_success:
            raise MinerError(f"{url} -> {response.status_code} {_error_text(raw)}")
        try:
            return json.loads(raw)
        except ValueError as error:
            raise MinerError(f"{url} returned malformed JSON") from error

    async def _probe_challenge(self) -> str | None:
        """An unauthenticated request whose only use is the challenge header it returns."""
        url = self._endpoint.base_url + "/session/info"
        try:
            async with (
                asyncio.timeout(PROBE_TIMEOUT_SECONDS),
                self._endpoint.client(PROBE_TIMEOUT_SECONDS) as client,
                client.stream("GET", url) as response,
            ):
                _refuse_unsafe(response)
                return response.headers.get(CHALLENGE_HEADER) or None
        except (TimeoutError, httpx.TimeoutException) as error:
            raise MinerTimeoutError(f"{url} timed out") from error

    async def info(self) -> MinerSessionInfo:
        self._challenge = await self._probe_challenge()
        response, raw = await self._exchange(
            "GET", "/session/info", headers=self._signed("GET", "/session/info", "")
        )
        if not response.is_success:
            raise MinerError(f"miner /session/info -> {response.status_code}")
        try:
            return _decode_info(json.loads(raw))
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise MinerError(f"malformed miner /session/info: {error!r}") from error

    async def open(self, agent_key: bytes) -> None:
        await self._post(
            "/session/open",
            {"channelId": to_hex(self._channel_id), "agentKey": to_hex(agent_key)},
        )

    async def turn(self, turn_input: bytes) -> TurnReceipt:
        url = self._endpoint.base_url + "/session/turn"
        body = compact_json(
            {"channelId": to_hex(self._channel_id), "input": to_hex(turn_input)}
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/x-ndjson",
            **self._signed("POST", "/session/turn", body),
        }
        try:
            # The body is read inside the deadline: a miner that stalls mid-stream
            # must not hold the turn open.
            async with (
                asyncio.timeout(TURN_TIMEOUT_SECONDS),
                self._endpoint.client(TURN_TIMEOUT_SECONDS) as client,
                client.stream(
                    "POST", url, headers=headers, content=body.encode()
                ) as response,
            ):
                _refuse_unsafe(response)
                content_type = response.headers.get("content-type", "")
                if response.is_success and "application/x-ndjson" in content_type:
                    return await _read_turn_stream(response)
                raw = await _read_capped(response)
        except (TimeoutError, httpx.TimeoutException) as error:
            raise MinerTimeoutError(
                f"turn timed out after {TURN_TIMEOUT_SECONDS}s"
            ) from error
        except (KeyError, TypeError, ValueError) as error:
            raise MinerError(f"malformed turn stream: {error}") from error
        failure = f"{url} -> {response.status_code} {_error_text(raw)}"
        if not response.is_success:
            raise MinerError(failure)
        try:
            return decode_turn_receipt(json.loads(raw))
        except ValueError as error:
            raise MinerError(failure) from error

    async def ack(self, ack: TurnAck) -> None:
        await self._post(
            "/session/ack",
            {
                "channelId": to_hex(self._channel_id),
                "turnIndex": ack.turn_index,
                "leafHash": to_hex(ack.leaf_hash),
                "agentSendMs": str(ack.agent_send_ms),
                "agentRecvMs": str(ack.agent_recv_ms),
                "agentSig": to_hex(ack.agent_sig),
                "timingStatus": ack.timing_status,
                "scoredLatencyMs": str(ack.scored_latency_ms),
            },
        )

    async def checkpoint(self, checkpoint: Checkpoint) -> None:
        await self._post(
            "/session/checkpoint",
            {
                "channelId": to_hex(self._channel_id),
                "finalRoot": to_hex(checkpoint.final_root),
                "aggregateGn": str(checkpoint.aggregate_gn),
                "payable": str(checkpoint.payable),
                "throughTurn": checkpoint.through_turn,
                "agentReceiptSig": to_hex(checkpoint.agent_receipt_sig),
            },
        )

    async def close(self, claim: SettlementClaim) -> MinerSettled | None:
        """Hand the co-signed claim to the miner, who alone may submit `settle`.

        None means the miner will not submit it (it has no chain submitter).
        """
        response = await self._post(
            "/session/close",
            {
                "channelId": to_hex(claim.channel_id),
                "finalRoot": to_hex(claim.final_root),
                "aggregateGn": str(claim.aggregate_gn),
                "payable": str(claim.payable),
                "agentReceiptSig": to_hex(claim.agent_receipt_sig),
                "claimedMeasuredRoot": to_hex(claim.claimed_measured_root),
                "transcriptRef": None,
            },
        )
        settled = response.get("settled") if isinstance(response, dict) else None
        if not settled:
            return None
        tx_hash = settled.get("txHash") if isinstance(settled, dict) else None
        well_formed = isinstance(tx_hash, str) and TX_HASH.fullmatch(tx_hash)
        return MinerSettled(tx_hash=tx_hash if well_formed else None)


async def _read_turn_stream(response: httpx.Response) -> TurnReceipt:
    stream = _TurnStream()
    pending: list[bytes] = []
    size = 0

    def end_line() -> None:
        line = b"".join(pending)
        pending.clear()
        if line.strip():
            stream.add(line)

    async with aclosing(response.aiter_bytes()) as chunks:
        async for chunk in chunks:
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise MinerError("miner response exceeds byte limit")
            *ends, rest = chunk.split(b"\n")
            for end in ends:
                pending.append(end)
                end_line()
            pending.append(rest)
    end_line()
    return stream.receipt()
