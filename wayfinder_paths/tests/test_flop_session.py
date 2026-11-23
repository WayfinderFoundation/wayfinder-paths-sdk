"""A direct FLOP session against an in-process miner and chain.

The fake miner holds a real sr25519 enclave key and signs real leaves; the fake chain
applies the pallet's own checks to `settle` and `force_settle`. Nothing touches a network.
"""

import asyncio
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
import sr25519

from wayfinder_paths.core.flop import miner as miner_wire
from wayfinder_paths.core.flop import wire
from wayfinder_paths.core.flop.account import (
    account_id_from_address,
    address_from_account_id,
)
from wayfinder_paths.core.flop.chain import (
    PRECISIONS,
    Channel,
    ChannelView,
    FlopChainError,
    Submitted,
)
from wayfinder_paths.core.flop.miner import (
    CHALLENGE_HEADER,
    MinerEndpoint,
    MinerError,
    MinerSession,
    MinerTurnError,
    TurnReceipt,
    decode_text_turn_output,
    decode_turn_receipt,
    encode_model_identity,
    encode_signed_frames,
    encode_turn_receipt,
    from_hex,
    to_hex,
    validate_endpoint,
)
from wayfinder_paths.core.flop.session import (
    DirectSessionError,
    ReceiptError,
    ReceiptVerifier,
    SessionKey,
    encode_chat_turn,
    run_direct_session,
)
from wayfinder_paths.core.flop.signer import LocalFlopSigner

MODEL = "qwen/qwen2.5-0.5b-instruct"
IDENTITY = wire.ModelIdentity(
    requested_slug=MODEL,
    registry_model="Qwen/Qwen2.5-0.5B-Instruct",
    registry_version="1",
    model_hash=wire.blake2_256(b"model"),
    precision=2,
    decode_policy_hash=wire.blake2_256(b"policy"),
    measured_root=wire.blake2_256(b"root"),
)
MINER_ADDRESS = address_from_account_id(wire.blake2_256(b"miner"))
ENDPOINT = "https://miner.example"
ESCROW = 10**15
BASE_PER_TURN = 1
ACK_WINDOW = 5
CAP = 10**12
START_BLOCK = 1000
SIGNED_PATHS = [
    "/session/info",
    "/session/open",
    "/session/turn",
    "/session/ack",
    "/session/checkpoint",
    "/session/close",
]


def signed_receipt(
    enclave: tuple[bytes, bytes],
    channel_id: bytes,
    turn_input: bytes,
    output: bytes,
    identity: wire.ModelIdentity,
    *,
    turn_index: int = 0,
    prior_leaves: tuple[bytes, ...] = (),
    g_n: int = 4321,
    signed_input: bytes | None = None,
) -> TurnReceipt:
    """What an honest miner returns: a V3 leaf signed by its enclave key."""
    receipt = TurnReceipt(
        leaf_version=wire.LeafVersion.V3,
        turn_index=turn_index,
        h_in=wire.blake2_256(turn_input if signed_input is None else signed_input),
        h_out=wire.turn_evidence_hash(output, 11, 7, "stop", identity),
        output=output,
        g_n=g_n,
        miner_recv_ms=1_000,
        miner_done_ms=1_250,
        latency_ms=250,
        enclave_sig=b"",
        running_root=b"",
        decode_policy_hash=identity.decode_policy_hash,
        prompt_tokens=11,
        completion_tokens=7,
        finish_reason="stop",
        model_identity=identity,
        h_ids=wire.h_ids([1, 2, 3], [4, 5]),
    )
    leaf_hash = leaf_of(receipt, channel_id).hash()
    return replace(
        receipt,
        enclave_sig=sr25519.sign(enclave, leaf_hash),
        running_root=wire.merkle_root([*prior_leaves, leaf_hash]),
    )


def leaf_of(receipt: TurnReceipt, channel_id: bytes) -> wire.Leaf:
    return wire.Leaf(
        version=receipt.leaf_version,
        channel_id=channel_id,
        turn_index=receipt.turn_index,
        h_in=receipt.h_in,
        h_out=receipt.h_out,
        g_n=receipt.g_n,
        decode_policy_hash=receipt.decode_policy_hash,
        h_ids=receipt.h_ids or wire.ZERO_HASH,
        toploc_commitment_hash=receipt.toploc_commitment_hash or wire.ZERO_HASH,
        miner_recv_ms=receipt.miner_recv_ms,
        miner_done_ms=receipt.miner_done_ms,
        latency_ms=receipt.latency_ms,
    )


class FakeChain:
    """Finalized state trails inclusion by one block, as it does on the real chain:
    a call lands in block `block + 1` and is only visible once `block` reaches it."""

    genesis_hash = bytes(32)

    def __init__(self, enclave_key: bytes, *, acks: bool = True) -> None:
        self.enclave_key = enclave_key
        self.acks = acks
        self.block = START_BLOCK
        self.now = 1_800_000_000_000
        self.channel_id = b""
        self.ack_deadline = 0
        self.history: list[tuple[int, Channel]] = []
        self.events: dict[int, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failing: set[str] = set()
        self.retracted: set[str] = set()
        self.settled_gn_skew = 0
        self.settled_cooperative = True
        self.settle_tx = ""

    # ---- test controls --------------------------------------------------------------

    def now_ms(self) -> int:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += int(seconds * 1000)
        self.block += 1
        best = self.best
        if self.acks and best and best.state == "PendingAck":
            self._set(
                replace(
                    best,
                    state="Open",
                    enclave_key=self.enclave_key,
                    opened_at=self.block + 1,
                    authored_opened_at=self.block + 1,
                    snapshot_cap=CAP,
                    current_cap=CAP,
                )
            )

    @property
    def best(self) -> Channel | None:
        return self.history[-1][1] if self.history else None

    def submitted(self, function: str) -> list[dict[str, Any]]:
        return [args for name, args in self.calls if name == function]

    def functions(self) -> list[str]:
        return [name for name, _ in self.calls]

    def _set(self, channel: Channel) -> None:
        self.history.append((self.block + 1, channel))

    def _emit(self, event_id: str, **attributes: Any) -> Submitted:
        number = self.block + 1
        event = {
            "module_id": "ComputeChannel",
            "event_id": event_id,
            "attributes": {"channel_id": to_hex(self.channel_id), **attributes},
        }
        self.events.setdefault(number, []).append({**event, "extrinsic_idx": 1})
        return Submitted(
            extrinsic_hash=to_hex(wire.blake2_256(f"{event_id}{number}".encode())),
            block_hash=to_hex(wire.blake2_256(str(number).encode())),
            block_number=number,
            fee=0,
            events=[event],
        )

    # ---- the FlopChain surface the session uses -------------------------------------

    async def finalized_block(self) -> int:
        return self.block

    async def constant(self, module: str, name: str) -> int:
        return {
            ("Babe", "ExpectedBlockTime"): 1000,
            ("ComputeChannel", "BasePerTurn"): BASE_PER_TURN,
        }[module, name]

    async def resolve_model_identity(self, model: str, *, advertised=None):
        return replace(IDENTITY, requested_slug=model)

    async def channel(self, channel_id: bytes) -> ChannelView:
        assert channel_id == self.channel_id
        visible = [c for included, c in self.history if included <= self.block]
        return ChannelView(block=self.block, channel=visible[-1] if visible else None)

    async def events_at(self, block_number: int) -> list[dict[str, Any]]:
        assert block_number <= self.block, "read past the finalized head"
        return self.events.get(block_number, [])

    async def submit(self, module: str, function: str, args: dict[str, Any], signer):
        assert module == "ComputeChannel"
        self.calls.append((function, args))
        if function in self.failing:
            raise FlopChainError(f"ComputeChannel.{function} failed: BadState")
        history, events = (
            list(self.history),
            {k: list(v) for k, v in self.events.items()},
        )
        if function == "force_open":
            submitted = self._force_open(args, signer)
        else:
            assert args["channel_id"] == to_hex(self.channel_id)
            submitted = getattr(self, f"_{function}")(args)
        if function in self.retracted:
            # Included in a block that the finalized chain then leaves out.
            self.history, self.events = history, events
        return submitted

    def _force_open(self, args: dict[str, Any], signer) -> Submitted:
        self.channel_id = wire.channel_id(
            self.genesis_hash,
            signer.account_id,
            account_id_from_address(args["miner"]),
            args["nonce"],
        )
        self.ack_deadline = self.block + 1 + ACK_WINDOW
        self._set(
            Channel(
                state="PendingAck",
                agent=signer.address,
                miner=args["miner"],
                agent_key=from_hex(args["agent_key"], 32),
                enclave_key=bytes(32),
                model_hash=from_hex(args["model_hash"], 32),
                measured_root=from_hex(args["measured_root"], 32),
                decode_policy_hash=from_hex(args["decode_policy_hash"], 32),
                precision=PRECISIONS.index(args["precision"]),
                escrow=args["escrow"],
                opened_at=self.block + 1,
                max_duration=args["sla"]["max_duration"],
                authored_opened_at=self.block + 1,
                snapshot_cap=None,
                current_cap=None,
            )
        )
        return self._emit(
            "ForceOpenRequested", ack_deadline=self.ack_deadline, escrow=args["escrow"]
        )

    def _require(self, condition: bool, function: str) -> Channel:
        if not condition or self.best is None:
            raise FlopChainError(f"ComputeChannel.{function} failed: BadState")
        return self.best

    def _expire_force_open(self, args: dict[str, Any]) -> Submitted:
        channel = self._require(
            self.best.state == "PendingAck" and self.block + 1 > self.ack_deadline,
            "expire_force_open",
        )
        self._set(replace(channel, state="Closed"))
        return self._emit("ForceOpenExpired", refunded=channel.escrow, failed_acks=1)

    def _timeout(self, args: dict[str, Any]) -> Submitted:
        best = self.best
        channel = self._require(
            best.state == "Open"
            and self.block + 1 > best.opened_at + best.max_duration,
            "timeout",
        )
        self._set(replace(channel, state="Closed"))
        return self._emit("TimedOutRefunded", refunded=channel.escrow)

    def _force_settle(self, args: dict[str, Any]) -> Submitted:
        """The pallet's gates: receipt signature at the tariff, each turn's enclave
        signature and Merkle path, the aggregate, and tariff within escrow."""
        channel = self._require(self.best.state == "Open", "force_settle")
        final_root = from_hex(args["final_root"], 32)
        turns = args["verified_turns"]
        payable = wire.force_settle_payable(
            BASE_PER_TURN, len(turns), args["aggregate_gn"]
        )
        valid = (
            args["transcript_ref"] is None
            and from_hex(args["claimed_measured_root"]) == channel.measured_root
            and payable <= channel.escrow
            and sum(turn["g_n"] for turn in turns) == args["aggregate_gn"]
            and sr25519.verify(
                from_hex(args["agent_receipt_sig"], 64),
                wire.receipt_message(
                    self.channel_id, final_root, args["aggregate_gn"], payable
                ),
                channel.agent_key,
            )
        )
        for turn in turns:
            leaf = wire.Leaf(
                version=wire.LeafVersion[turn["leaf_version"]],
                channel_id=self.channel_id,
                turn_index=turn["turn_index"],
                h_in=from_hex(turn["h_in"]),
                h_out=from_hex(turn["h_out"]),
                g_n=turn["g_n"],
                decode_policy_hash=from_hex(turn["decode_policy_hash"]),
                h_ids=from_hex(turn["h_ids"]),
                toploc_commitment_hash=from_hex(turn["toploc_commitment_hash"]),
                miner_recv_ms=turn["miner_recv_ms"],
                miner_done_ms=turn["miner_done_ms"],
                latency_ms=turn["latency_ms"],
            ).hash()
            path = [(from_hex(s), left) for s, left in turn["merkle_path"]]
            valid = (
                valid
                and from_hex(turn["decode_policy_hash"]) == channel.decode_policy_hash
                and sr25519.verify(
                    from_hex(turn["enclave_sig"]), leaf, channel.enclave_key
                )
                and wire.root_from_path(leaf, path) == final_root
            )
        self._require(valid, "force_settle")
        self._set(replace(channel, state="Settling"))
        return self._emit(
            "SettlementPosted",
            final_root=args["final_root"],
            aggregate_gn=args["aggregate_gn"],
            challenge_end=self.block + 1 + 100,
        )

    def miner_settle(self, claim: dict[str, Any]) -> str:
        """Cooperative `settle`, submitted by the miner: the receipt must be co-signed
        for the whole escrow."""
        channel = self._require(self.best.state == "Open", "settle")
        aggregate_gn = int(claim["aggregateGn"])
        valid = int(claim["payable"]) == channel.escrow and sr25519.verify(
            from_hex(claim["agentReceiptSig"], 64),
            wire.receipt_message(
                self.channel_id,
                from_hex(claim["finalRoot"], 32),
                aggregate_gn,
                channel.escrow,
            ),
            channel.agent_key,
        )
        self._require(valid, "settle")
        self._set(replace(channel, state="Closed"))
        miner_paid = channel.escrow * 85 // 100
        self.settle_tx = self._emit(
            "Settled",
            miner_paid=miner_paid,
            burned=channel.escrow - miner_paid,
            aggregate_gn=aggregate_gn + self.settled_gn_skew,
            cooperative=self.settled_cooperative,
        ).extrinsic_hash
        return self.settle_tx


class FakeMiner:
    def __init__(self, chain: FakeChain, enclave: tuple[bytes, bytes]) -> None:
        self.chain = chain
        self.enclave = enclave
        self.signing_key = enclave
        self.identity = replace(IDENTITY, backend_model_name="qwen-backend")
        self.info: dict[str, Any] = {
            "minerAddress": MINER_ADDRESS,
            "enclaveKey": to_hex(enclave[0]),
            "modelIdentity": encode_model_identity(self.identity),
            "is_ready": True,
        }
        self.challenge = "5c" * 32
        self.reply = "Hello from the miner ✓"
        self.g_n = 4321
        self.stream = False
        self.framed = False
        self.signs_other_input = False
        self.close = "settle"
        self.requests: list[httpx.Request] = []
        self.turn_inputs: list[bytes] = []
        self.acks: list[dict[str, Any]] = []
        self.checkpoints: list[dict[str, Any]] = []
        self.transport = httpx.MockTransport(self.handle)

    def paths(self, *, signed: bool = True) -> list[str]:
        return [
            request.url.path
            for request in self.requests
            if ("x-flop-signature" in request.headers) == signed
        ]

    def authentic(self, request: httpx.Request, body: str | None = None) -> bool:
        headers = request.headers
        message = wire.session_request_message(
            headers["x-flop-channel"],
            request.method,
            request.url.path,
            request.content.decode() if body is None else body,
            headers["x-flop-timestamp"],
            headers["x-flop-nonce"],
            headers["x-flop-challenge"],
        )
        return (
            headers["x-flop-channel"] == to_hex(self.chain.channel_id)
            and headers["x-flop-challenge"] == self.challenge
            and sr25519.verify(
                from_hex(headers["x-flop-signature"], 64),
                message,
                self.chain.best.agent_key,
            )
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if "x-flop-signature" not in request.headers:
            return httpx.Response(
                401, headers={CHALLENGE_HEADER: self.challenge}, json={"error": "auth"}
            )
        if not self.authentic(request):
            return httpx.Response(403, json={"error": "bad request signature"})
        if request.method == "GET":
            return httpx.Response(200, json=self.info)
        payload = json.loads(request.content)
        route = request.url.path.removeprefix("/session/")
        return getattr(self, f"_{route}")(payload)

    def _open(self, payload: dict[str, Any]) -> httpx.Response:
        valid = payload == {
            "channelId": to_hex(self.chain.channel_id),
            "agentKey": to_hex(self.chain.best.agent_key),
        }
        return httpx.Response(200 if valid else 400, json={"ok": valid})

    def _turn(self, payload: dict[str, Any]) -> httpx.Response:
        turn_input = from_hex(payload["input"])
        self.turn_inputs.append(turn_input)
        text = self.reply.encode()
        frames = [("output", text[:5]), ("output", text[5:])]
        receipt = signed_receipt(
            self.signing_key,
            self.chain.channel_id,
            turn_input,
            encode_signed_frames(frames) if self.framed else text,
            self.identity,
            g_n=self.g_n,
            signed_input=b"another request" if self.signs_other_input else None,
        )
        if not self.stream:
            return httpx.Response(200, json=encode_turn_receipt(receipt))
        lines = [
            {"type": "chunk", "sequence": i, "kind": kind, "data": to_hex(data)}
            for i, (kind, data) in enumerate(frames)
        ]
        lines.append(
            {
                "type": "terminal",
                "sequence": len(lines),
                "receipt": encode_turn_receipt(receipt),
            }
        )
        return ndjson(lines)

    def _ack(self, payload: dict[str, Any]) -> httpx.Response:
        valid = sr25519.verify(
            from_hex(payload["agentSig"], 64),
            wire.turn_ack_message(
                self.chain.channel_id,
                payload["turnIndex"],
                from_hex(payload["leafHash"], 32),
                int(payload["agentSendMs"]),
                int(payload["agentRecvMs"]),
            ),
            self.chain.best.agent_key,
        )
        self.acks.append(payload)
        return httpx.Response(200 if valid else 400, json={"ok": valid})

    def _checkpoint(self, payload: dict[str, Any]) -> httpx.Response:
        valid = sr25519.verify(
            from_hex(payload["agentReceiptSig"], 64),
            wire.receipt_message(
                self.chain.channel_id,
                from_hex(payload["finalRoot"], 32),
                int(payload["aggregateGn"]),
                int(payload["payable"]),
            ),
            self.chain.best.agent_key,
        )
        self.checkpoints.append(payload)
        return httpx.Response(200 if valid else 400, json={"ok": valid})

    def _close(self, payload: dict[str, Any]) -> httpx.Response:
        if self.close == "no_submitter":
            return httpx.Response(200, json={"ok": True, "settled": None})
        if self.close == "refuses":
            return httpx.Response(503, json={"error": "try later"})
        if self.close == "refuses_after_timeout":
            self.chain._set(replace(self.chain.best, state="Closed"))
            self.chain._emit("TimedOutRefunded", refunded=ESCROW)
            return httpx.Response(503, json={"error": "channel closed"})
        tx_hash = self.chain.miner_settle(payload)
        if self.close == "settles_then_errors":
            return httpx.Response(500, text="lost the response")
        return httpx.Response(200, json={"ok": True, "settled": {"txHash": tx_hash}})


class Chunks(httpx.AsyncByteStream):
    """A body that arrives in pieces, as it does off a socket."""

    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def ndjson(lines: list[dict[str, Any]], *, chunk_bytes: int = 37) -> httpx.Response:
    body = "".join(json.dumps(line) + "\n" for line in lines).encode()
    pieces = [body[i : i + chunk_bytes] for i in range(0, len(body), chunk_bytes)]
    return httpx.Response(
        200,
        headers={"content-type": "application/x-ndjson"},
        stream=Chunks(*pieces),
    )


@pytest.fixture(autouse=True)
async def finalize_abandoned_streams():
    """A response abandoned mid-stream is closed at once, but httpx's inner stream
    generators are finalized on the next loop turn; give them that turn."""
    yield
    await asyncio.sleep(0)


@pytest.fixture
def enclave() -> tuple[bytes, bytes]:
    return sr25519.pair_from_seed(bytes([7]) * 32)


@pytest.fixture
def chain(enclave) -> FakeChain:
    return FakeChain(enclave[0])


@pytest.fixture
def miner(chain, enclave) -> FakeMiner:
    return FakeMiner(chain, enclave)


async def run(chain: FakeChain, miner: FakeMiner, /, **overrides: Any):
    arguments: dict[str, Any] = {
        "chain": chain,
        "signer": LocalFlopSigner(bytes([1]) * 32),
        "miner": MINER_ADDRESS,
        "model": MODEL,
        "endpoint": ENDPOINT,
        "prompt": "Say hello.",
        "max_flop_raw": ESCROW,
        "sleep": chain.sleep,
        "now_ms": chain.now_ms,
        "transport": miner.transport,
    }
    return await run_direct_session(**{**arguments, **overrides})


# ---- the whole flow -----------------------------------------------------------------


async def test_happy_path_settles_cooperatively(chain, miner):
    result = await run(chain, miner)

    assert result.reply == miner.reply
    assert result.miner == MINER_ADDRESS
    assert result.channel_id == to_hex(chain.channel_id)
    assert result.settlement == "settled"
    assert result.settle_tx == chain.settle_tx
    assert chain.events[result.settlement_block][0]["event_id"] == "Settled"
    assert (result.escrow, result.payable) == (ESCROW, ESCROW)
    assert result.aggregate_gn == miner.g_n
    assert result.miner_paid + result.burned == ESCROW
    assert result.challenge_end is None
    [receipt] = result.receipts
    assert (receipt.prompt_tokens, receipt.completion_tokens) == (11, 7)
    assert (receipt.finish_reason, receipt.g_n) == ("stop", miner.g_n)

    assert chain.functions() == ["force_open"]
    [opened] = chain.submitted("force_open")
    assert opened["sla"] == {"max_turns": 1, "max_duration": 120, "reattest_every": 1}
    assert (opened["escrow"], opened["precision"]) == (ESCROW, "BF16")
    assert chain.best.state == "Closed"

    assert miner.paths(signed=False) == ["/session/info"]
    assert miner.paths() == SIGNED_PATHS
    [sent] = miner.turn_inputs
    assert json.loads(sent) == {
        "version": "flop.chat-turn.v1",
        "model": MODEL,
        "messages": [{"role": "user", "content": "Say hello."}],
        "max_tokens": 256,
    }
    [ack] = miner.acks
    assert (ack["turnIndex"], ack["timingStatus"]) == (0, "Eligible")
    assert ack["leafHash"] == to_hex(leaf_of(receipt, chain.channel_id).hash())
    [checkpoint] = miner.checkpoints
    assert checkpoint["throughTurn"] == 0
    assert int(checkpoint["payable"]) == wire.force_settle_payable(
        BASE_PER_TURN, 1, miner.g_n
    )


async def test_streamed_and_framed_turn_is_accepted(chain, miner):
    miner.stream = True
    miner.framed = True

    history = [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "hi"},
    ]

    result = await run(chain, miner, prompt=None, messages=history)

    assert json.loads(miner.turn_inputs[0])["messages"] == history

    assert result.reply == miner.reply
    assert result.settlement == "settled"


async def test_requests_are_signed_by_the_session_key_over_the_exact_body(chain, miner):
    await run(chain, miner)

    session_key = from_hex(chain.submitted("force_open")[0]["agent_key"], 32)
    assert session_key == chain.best.agent_key
    signed = [r for r in miner.requests if "x-flop-signature" in r.headers]
    assert len(signed) == len(SIGNED_PATHS)
    for request in signed:
        assert miner.authentic(request)
        assert not miner.authentic(request, body=request.content.decode() + " ")
        assert int(request.headers["x-flop-timestamp"]) == pytest.approx(
            chain.now, abs=200_000
        )
    assert len({r.headers["x-flop-nonce"] for r in signed}) == len(signed)
    assert miner.requests[0].headers.get("x-flop-channel") is None


async def test_nothing_but_the_session_signature_is_sent_to_the_miner(
    chain, miner, monkeypatch
):
    monkeypatch.setenv("WAYFINDER_API_KEY", "wk_secret")

    await run(chain, miner)

    allowed = {
        "host",
        "accept",
        "accept-encoding",
        "connection",
        "user-agent",
        "content-type",
        "content-length",
        "x-flop-channel",
        "x-flop-timestamp",
        "x-flop-nonce",
        "x-flop-challenge",
        "x-flop-signature",
    }
    for request in miner.requests:
        assert set(request.headers) <= allowed
        assert b"wk_secret" not in request.content
        assert request.url.host == "miner.example"


async def test_receipt_from_the_wrong_enclave_key_is_rejected_and_escrow_recovered(
    chain, miner
):
    miner.signing_key = sr25519.pair_from_seed(bytes([9]) * 32)

    with pytest.raises(DirectSessionError) as failure:
        await run(chain, miner)

    assert isinstance(failure.value.__cause__, ReceiptError)
    assert failure.value.__cause__.kind == "BadEnclaveSignature"
    assert failure.value.channel_id == chain.channel_id
    assert failure.value.recovery == "refunded"
    assert miner.acks == [] and miner.checkpoints == []
    assert "/session/close" not in miner.paths()
    assert chain.functions() == ["force_open", "timeout"]
    assert chain.best.state == "Closed"


async def test_receipt_for_a_different_request_is_rejected(chain, miner):
    miner.signs_other_input = True

    with pytest.raises(DirectSessionError) as failure:
        await run(chain, miner)

    assert failure.value.__cause__.kind == "InputHashMismatch"
    assert miner.acks == []
    assert chain.functions() == ["force_open", "timeout"]


def mismatched_enclave_key(miner: FakeMiner) -> None:
    miner.info["enclaveKey"] = to_hex(sr25519.pair_from_seed(bytes([9]) * 32)[0])


def mismatched_measured_root(miner: FakeMiner) -> None:
    miner.info["modelIdentity"]["measuredRoot"] = to_hex(wire.blake2_256(b"other"))


def mismatched_model(miner: FakeMiner) -> None:
    miner.info["modelIdentity"]["requestedSlug"] = "some/other-model"


def another_miner(miner: FakeMiner) -> None:
    miner.info["minerAddress"] = address_from_account_id(wire.blake2_256(b"other"))


def not_ready(miner: FakeMiner) -> None:
    miner.info["is_ready"] = False


def no_identity(miner: FakeMiner) -> None:
    del miner.info["modelIdentity"]


@pytest.mark.parametrize(
    "lie",
    [
        mismatched_enclave_key,
        mismatched_measured_root,
        mismatched_model,
        another_miner,
        not_ready,
        no_identity,
    ],
    ids=lambda lie: lie.__name__,
)
async def test_info_that_contradicts_the_finalized_channel_stops_before_the_prompt(
    chain, miner, lie
):
    lie(miner)

    with pytest.raises(DirectSessionError, match="/session/info") as failure:
        await run(chain, miner)

    assert miner.turn_inputs == []
    assert miner.paths() == ["/session/info"]
    assert chain.functions() == ["force_open", "timeout"]
    assert failure.value.recovery == "refunded"


async def test_finalized_channel_that_differs_from_the_request_is_refused(chain, miner):
    original = chain._force_open

    def open_with_other_model(args, signer):
        return original({**args, "model_hash": to_hex(bytes([3]) * 32)}, signer)

    chain._force_open = open_with_other_model

    with pytest.raises(DirectSessionError, match="channel identity mismatch"):
        await run(chain, miner)

    assert miner.requests == []


async def test_miner_that_never_acknowledges_gets_the_force_open_expired(chain, miner):
    chain.acks = False

    with pytest.raises(
        DirectSessionError, match="miner acknowledgement timed out"
    ) as failure:
        await run(chain, miner)

    assert failure.value.channel_id == chain.channel_id
    assert failure.value.recovery == "refunded"
    assert chain.functions() == ["force_open", "expire_force_open"]
    assert chain.submitted("expire_force_open") == [
        {"channel_id": to_hex(chain.channel_id)}
    ]
    assert chain.best.state == "Closed"
    assert miner.requests == []


async def test_miner_without_a_submitter_is_force_settled(chain, miner):
    miner.close = "no_submitter"

    result = await run(chain, miner)

    assert result.reply == miner.reply
    assert result.settlement == "posted"
    assert chain.functions() == ["force_open", "force_settle"]
    [settle] = chain.submitted("force_settle")
    [receipt] = result.receipts
    payable = wire.force_settle_payable(BASE_PER_TURN, 1, miner.g_n)
    assert result.payable == payable
    assert (result.aggregate_gn, settle["aggregate_gn"]) == (miner.g_n, miner.g_n)
    assert result.challenge_end > result.settlement_block
    assert result.settle_tx == to_hex(
        wire.blake2_256(f"SettlementPosted{result.settlement_block}".encode())
    )
    leaf_hash = leaf_of(receipt, chain.channel_id).hash()
    assert settle["final_root"] == to_hex(wire.merkle_root([leaf_hash]))
    assert settle["claimed_measured_root"] == to_hex(IDENTITY.measured_root)
    assert settle["transcript_ref"] is None
    assert sr25519.verify(
        from_hex(settle["agent_receipt_sig"], 64),
        wire.receipt_message(chain.channel_id, leaf_hash, miner.g_n, payable),
        chain.best.agent_key,
    )
    assert settle["verified_turns"] == [
        {
            "leaf_version": "V3",
            "turn_index": 0,
            "h_in": to_hex(wire.blake2_256(miner.turn_inputs[0])),
            "h_out": to_hex(receipt.h_out),
            "g_n": miner.g_n,
            "decode_policy_hash": to_hex(IDENTITY.decode_policy_hash),
            "h_ids": to_hex(receipt.h_ids),
            "toploc_commitment_hash": to_hex(wire.ZERO_HASH),
            "miner_recv_ms": 1_000,
            "miner_done_ms": 1_250,
            "latency_ms": 250,
            "enclave_sig": to_hex(receipt.enclave_sig),
            "merkle_path": wire.merkle_path([leaf_hash], 0),
        }
    ]
    assert chain.best.state == "Settling"


async def test_failed_force_settle_is_reported_as_unknown(chain, miner):
    miner.close = "no_submitter"
    chain.failing = {"force_settle"}

    with pytest.raises(
        DirectSessionError, match="force settlement submission outcome unknown"
    ) as failure:
        await run(chain, miner)

    assert failure.value.recovery is None
    assert chain.functions() == ["force_open", "force_settle"]


async def test_force_settle_that_is_never_finalized_is_reported_as_unknown(
    chain, miner
):
    miner.close = "no_submitter"
    chain.retracted = {"force_settle"}

    with pytest.raises(
        DirectSessionError, match="force settlement submission outcome unknown"
    ) as failure:
        await run(chain, miner)

    assert "was not finalized within 60 seconds" in str(
        failure.value.__cause__.exceptions[1]
    )
    assert chain.best.state == "Open"


async def test_force_open_that_is_never_finalized_still_names_the_channel(chain, miner):
    chain.retracted = {"force_open"}

    with pytest.raises(DirectSessionError, match="on-chain recovery failed") as failure:
        await run(chain, miner)

    assert failure.value.channel_id == chain.channel_id
    assert chain.functions() == ["force_open"]
    assert miner.requests == []


async def test_close_the_miner_refuses_is_not_raced_with_a_force_settle(chain, miner):
    miner.close = "refuses"

    with pytest.raises(
        DirectSessionError, match="cooperative settlement outcome unknown"
    ) as failure:
        await run(chain, miner)

    assert failure.value.channel_id == chain.channel_id
    assert chain.functions() == ["force_open"]


async def test_settlement_is_read_from_the_chain_when_the_miner_reply_is_lost(
    chain, miner
):
    miner.close = "settles_then_errors"

    result = await run(chain, miner)

    assert result.settlement == "settled"
    assert result.settle_tx is None
    assert chain.events[result.settlement_block][0]["event_id"] == "Settled"


@pytest.mark.parametrize(
    "difference", [{"settled_gn_skew": 1}, {"settled_cooperative": False}], ids=str
)
async def test_finalized_settlement_that_differs_from_the_claim_is_not_accepted(
    chain, miner, difference
):
    vars(chain).update(difference)

    with pytest.raises(
        DirectSessionError, match="cooperative settlement outcome unknown"
    ):
        await run(chain, miner)

    assert chain.functions() == ["force_open"]


async def test_channel_that_closed_without_settling_is_reported(chain, miner):
    miner.close = "refuses_after_timeout"

    with pytest.raises(
        DirectSessionError, match="channel terminal outcome: TimedOutRefunded"
    ) as failure:
        await run(chain, miner)

    assert failure.value.channel_id == chain.channel_id
    assert chain.functions() == ["force_open"]


async def test_tariff_above_the_escrow_is_refused(chain, miner):
    miner.g_n = ESCROW

    with pytest.raises(
        DirectSessionError, match="turn tariff exceeds escrow"
    ) as failure:
        await run(chain, miner)

    assert failure.value.__cause__.kind == "TariffExceedsEscrow"
    assert miner.acks == []
    assert chain.functions() == ["force_open", "timeout"]


async def test_failed_recovery_is_reported_with_the_channel(chain, miner):
    miner.signing_key = sr25519.pair_from_seed(bytes([9]) * 32)
    chain.failing = {"timeout"}

    with pytest.raises(DirectSessionError, match="on-chain recovery failed") as failure:
        await run(chain, miner)

    assert failure.value.channel_id == chain.channel_id
    assert failure.value.recovery is None
    session_error, recovery_error = failure.value.__cause__.exceptions
    assert isinstance(session_error, ReceiptError)
    assert isinstance(recovery_error, FlopChainError)


# ---- refused before any FLOP is escrowed --------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://miner.example",
        "http://10.0.0.5:8645",
        "http://localhost.miner.example",
        "ftp://miner.example",
        "miner.example",
        "https://user:secret@miner.example",
        "http://127.0.0.1@miner.example",
        "https://miner.example/?key=1",
        "https://miner.example/#frag",
        "https://miner.example\\@other.example",
        "https://miner.example:notaport",
    ],
)
async def test_unsafe_endpoint_is_refused_before_anything_is_submitted(
    chain, miner, endpoint
):
    with pytest.raises(ValueError):
        await run(chain, miner, endpoint=endpoint)

    assert chain.calls == [] and miner.requests == []


@pytest.mark.parametrize(
    ("endpoint", "base_url"),
    [
        ("https://miner.example/", "https://miner.example"),
        ("https://miner.example:8443/base/", "https://miner.example:8443/base"),
        ("http://localhost:8645", "http://localhost:8645"),
        ("http://127.0.0.1:8645/", "http://127.0.0.1:8645"),
        ("http://[::1]:8645", "http://[::1]:8645"),
    ],
)
def test_https_and_loopback_endpoints_are_accepted(endpoint, base_url):
    assert validate_endpoint(endpoint) == base_url


@pytest.mark.parametrize(
    "arguments",
    [
        {"prompt": None},
        {"messages": [{"role": "user", "content": "hi"}]},
        {"prompt": None, "messages": []},
        {"prompt": None, "messages": [{"role": "wizard", "content": "hi"}]},
        {"prompt": None, "messages": [{"role": "user"}]},
        {"max_tokens": 0},
        {"max_tokens": True},
        {"max_flop_raw": 0},
        {"miner": "not-an-address"},
    ],
    ids=str,
)
async def test_invalid_arguments_are_refused_before_anything_is_submitted(
    chain, miner, arguments
):
    with pytest.raises(ValueError):
        await run(chain, miner, **arguments)

    assert chain.calls == [] and miner.requests == []


def test_chat_turn_is_canonical_json():
    encoded = encode_chat_turn(
        "m", [{"role": "user", "content": 'héllo "q"\n', "name": None}], 5
    )

    assert (
        encoded
        == (
            '{"max_tokens":5,"messages":[{"content":"héllo \\"q\\"\\n","name":null,'
            '"role":"user"}],"model":"m","version":"flop.chat-turn.v1"}'
        ).encode()
    )


# ---- receipt verification -----------------------------------------------------------


@pytest.fixture
def verifier(enclave) -> ReceiptVerifier:
    return ReceiptVerifier(
        channel_id=wire.blake2_256(b"channel"),
        enclave_key=enclave[0],
        key=SessionKey(bytes([5]) * 32),
        decode_policy_hash=IDENTITY.decode_policy_hash,
        measured_root=IDENTITY.measured_root,
        model_identity=IDENTITY,
        escrow=ESCROW,
        base_per_turn=BASE_PER_TURN,
    )


CHANNEL = wire.blake2_256(b"channel")
TURN_INPUT = b'{"turn":0}'


def honest(enclave, **overrides: Any) -> TurnReceipt:
    return signed_receipt(enclave, CHANNEL, TURN_INPUT, b"out", IDENTITY, **overrides)


def test_verifier_keeps_the_running_root_and_proves_every_turn(verifier, enclave):
    leaves: list[bytes] = []
    for index, g_n in enumerate([10, 20, 30]):
        turn_input = f"turn {index}".encode()
        receipt = signed_receipt(
            enclave,
            CHANNEL,
            turn_input,
            f"reply {index}".encode(),
            IDENTITY,
            turn_index=index,
            prior_leaves=tuple(leaves),
            g_n=g_n,
        )
        ack = verifier.accept(turn_input, receipt, 5_000, 5_300)
        leaves.append(ack.leaf_hash)
        assert sr25519.verify(
            ack.agent_sig,
            wire.turn_ack_message(CHANNEL, index, ack.leaf_hash, 5_000, 5_300),
            SessionKey(bytes([5]) * 32).public_key,
        )
        assert (ack.timing_status, ack.scored_latency_ms) == ("Eligible", 300)

    assert verifier.turns_verified == 3
    assert verifier.aggregate_gn == 60
    assert verifier.root() == wire.merkle_root(leaves)
    for index, turn in enumerate(verifier.verified_turns()):
        path = [(from_hex(s), left) for s, left in turn["merkle_path"]]
        assert path == wire.merkle_path(leaves, index)
        assert wire.root_from_path(leaves[index], path) == verifier.root()
    force = verifier.force_claim()
    assert force.payable == wire.force_settle_payable(BASE_PER_TURN, 3, 60)
    assert verifier.cooperative_claim().payable == ESCROW
    assert verifier.checkpoint().through_turn == 2


def tampered_wire(enclave, **changes: Any) -> TurnReceipt:
    return decode_turn_receipt({**encode_turn_receipt(honest(enclave)), **changes})


@pytest.mark.parametrize(
    ("forge", "kind"),
    [
        (
            lambda e: honest(sr25519.pair_from_seed(bytes([9]) * 32)),
            "BadEnclaveSignature",
        ),
        (lambda e: honest(e, signed_input=b"other"), "InputHashMismatch"),
        (lambda e: tampered_wire(e, hIn=to_hex(bytes(32))), "InputHashMismatch"),
        (lambda e: tampered_wire(e, output=to_hex(b"edited")), "OutputHashMismatch"),
        (lambda e: tampered_wire(e, completionTokens="9"), "OutputHashMismatch"),
        (lambda e: tampered_wire(e, finishReason="length"), "OutputHashMismatch"),
        (lambda e: tampered_wire(e, gN="1"), "BadEnclaveSignature"),
        (lambda e: tampered_wire(e, leafVersion="V2"), "BadEnclaveSignature"),
        (lambda e: tampered_wire(e, latencyMs="251"), "BadEnclaveSignature"),
        (lambda e: tampered_wire(e, runningRoot=to_hex(bytes(32))), "RootMismatch"),
        (lambda e: honest(e, turn_index=1), "UnexpectedTurnIndex"),
        (lambda e: honest(e, g_n=ESCROW), "TariffExceedsEscrow"),
        (
            lambda e: tampered_wire(e, decodePolicyHash=to_hex(bytes([1]) * 32)),
            "DecodePolicyMismatch",
        ),
        (
            lambda e: signed_receipt(
                e, CHANNEL, TURN_INPUT, b"out", replace(IDENTITY, registry_version="2")
            ),
            "ModelIdentityMismatch",
        ),
        (
            lambda e: signed_receipt(
                e, CHANNEL, TURN_INPUT, b"FLOP-TURN-FRAMES-V9\x00", IDENTITY
            ),
            "OutputHashMismatch",
        ),
    ],
)
def test_verifier_rejects_a_forged_receipt_and_keeps_nothing(
    verifier, enclave, forge, kind
):
    with pytest.raises(ReceiptError) as rejected:
        verifier.accept(TURN_INPUT, forge(enclave), 5_000, 5_300)

    assert rejected.value.kind == kind
    assert verifier.turns_verified == 0
    assert verifier.aggregate_gn == 0
    assert verifier.root() == wire.ZERO_HASH


def test_verifier_rejects_partial_provider_evidence(verifier, enclave):
    wire_form = encode_turn_receipt(honest(enclave))
    del wire_form["promptTokens"]

    with pytest.raises(ReceiptError, match="partial provider evidence"):
        verifier.accept(TURN_INPUT, decode_turn_receipt(wire_form), 5_000, 5_300)


def test_verifier_rejects_impossible_timing(verifier, enclave):
    with pytest.raises(ReceiptError, match="AgentClockReversed"):
        verifier.accept(TURN_INPUT, honest(enclave), 5_300, 5_000)


# ---- the wire -----------------------------------------------------------------------


def test_receipt_wire_round_trip(enclave):
    receipt = replace(honest(enclave), toploc_commitment_hash=bytes([4]) * 32)

    wire_form = json.loads(json.dumps(encode_turn_receipt(receipt)))

    assert decode_turn_receipt(wire_form) == receipt
    assert wire_form["leafVersion"] == "V3"
    assert wire_form["turnIndex"] == 0
    assert wire_form["gN"] == "4321"
    assert wire_form["promptTokens"] == "11"
    assert wire_form["latencyMs"] == "250"
    assert wire_form["hIn"] == "0x" + receipt.h_in.hex()
    assert wire_form["output"] == "0x" + b"out".hex()
    assert wire_form["modelIdentity"]["modelHash"] == to_hex(IDENTITY.model_hash)
    assert wire_form["modelIdentity"]["precision"] == 2


def test_receipt_wire_round_trip_without_optional_fields():
    receipt = TurnReceipt(
        leaf_version=wire.LeafVersion.V1,
        turn_index=3,
        h_in=bytes([1]) * 32,
        h_out=wire.blake2_256(b""),
        output=b"",
        g_n=0,
        miner_recv_ms=1,
        miner_done_ms=2,
        latency_ms=1,
        enclave_sig=bytes(64),
        running_root=bytes(32),
    )

    wire_form = encode_turn_receipt(receipt)

    assert set(wire_form) == {
        "leafVersion",
        "turnIndex",
        "hIn",
        "hOut",
        "output",
        "gN",
        "minerRecvMs",
        "minerDoneMs",
        "latencyMs",
        "enclaveSig",
        "runningRoot",
    }
    assert wire_form["output"] == "0x"
    assert decode_turn_receipt(wire_form) == receipt
    assert receipt.evidence_hash() == receipt.h_out


@pytest.mark.parametrize(
    "changes",
    [
        {"leafVersion": "V4"},
        {"leafVersion": 3},
        {"turnIndex": "0"},
        {"hIn": "abcd"},
        {"hOut": "0x1234"},
        {"enclaveSig": "0x" + "00" * 63},
        {"gN": "-1"},
        {"gN": "0x10"},
        {"gN": 1.5},
        {"promptTokens": None},
        {"modelIdentity": {"requestedSlug": "x"}},
        {"output": "0xzz"},
    ],
    ids=str,
)
def test_malformed_receipt_wire_is_refused(enclave, changes):
    with pytest.raises(MinerError, match="malformed turn receipt"):
        decode_turn_receipt({**encode_turn_receipt(honest(enclave)), **changes})


def test_text_output_is_unwrapped_from_signed_frames():
    framed = encode_signed_frames([("output", b"Hel"), ("output", b"lo")])

    assert framed.startswith(b"FLOP-TURN-FRAMES-V1\x00\x00\x00\x00\x00\x03Hel")
    assert decode_text_turn_output(framed) == b"Hello"
    assert decode_text_turn_output(b"plain") == b"plain"
    with pytest.raises(ValueError, match="tool frames"):
        decode_text_turn_output(encode_signed_frames([("tool_call", b"{}")]))
    with pytest.raises(ValueError, match="unsupported signed output frame version"):
        decode_text_turn_output(b"FLOP-TURN-FRAMES-V9\x00")
    with pytest.raises(ValueError, match="truncated"):
        decode_text_turn_output(framed[:-1])


def scripted_session(respond) -> MinerSession:
    """A session past its `/session/info` handshake, talking to `respond`."""
    endpoint = MinerEndpoint(ENDPOINT, transport=httpx.MockTransport(respond))
    session = endpoint.session(CHANNEL, SessionKey(bytes([5]) * 32))
    session._challenge = "5c" * 32
    return session


def stream_of(enclave, *, output: bytes = b"Hello") -> list[dict[str, Any]]:
    receipt = honest(enclave)
    receipt = replace(
        receipt,
        output=output,
        h_out=wire.turn_evidence_hash(output, 11, 7, "stop", IDENTITY),
    )
    return [
        {"type": "chunk", "sequence": 0, "kind": "output", "data": to_hex(b"Hel")},
        {"type": "chunk", "sequence": 1, "kind": "output", "data": to_hex(b"lo")},
        {"type": "terminal", "sequence": 2, "receipt": encode_turn_receipt(receipt)},
    ]


async def test_streamed_turn_is_collected_into_its_terminal_receipt(enclave):
    lines = stream_of(enclave)

    receipt = await scripted_session(lambda request: ndjson(lines)).turn(TURN_INPUT)

    assert receipt.output == b"Hello"
    assert receipt == decode_turn_receipt(lines[-1]["receipt"])


def reordered(lines):
    return [lines[1], lines[0], lines[2]]


def without_terminal(lines):
    return lines[:2]


def after_terminal(lines):
    return [*lines, {"type": "chunk", "sequence": 3, "kind": "output", "data": "0x"}]


def missing_chunk(lines):
    return [lines[0], {**lines[2], "sequence": 1}]


def unknown_kind(lines):
    return [{**lines[0], "kind": "image"}, *lines[1:]]


@pytest.mark.parametrize(
    "corrupt",
    [reordered, without_terminal, after_terminal, missing_chunk, unknown_kind],
    ids=lambda corrupt: corrupt.__name__,
)
async def test_broken_turn_stream_is_refused(enclave, corrupt):
    lines = corrupt(stream_of(enclave))

    with pytest.raises(MinerError, match="malformed turn stream"):
        await scripted_session(lambda request: ndjson(lines)).turn(TURN_INPUT)


async def test_stream_whose_chunks_do_not_hash_to_the_receipt_is_refused(enclave):
    lines = stream_of(enclave)
    lines[-1]["receipt"]["hOut"] = to_hex(bytes(32))

    with pytest.raises(MinerError, match="terminal evidence hash mismatch"):
        await scripted_session(lambda request: ndjson(lines)).turn(TURN_INPUT)


async def test_error_frame_ends_the_turn_with_the_miners_reason(enclave):
    lines = [stream_of(enclave)[0], {"type": "error", "error": "out of\nmemory"}]

    with pytest.raises(MinerTurnError, match="miner turn failed: out of memory"):
        await scripted_session(lambda request: ndjson(lines)).turn(TURN_INPUT)


async def test_redirects_are_refused():
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"location": "https://other.example/x"})

    with pytest.raises(MinerError, match="redirect"):
        await scripted_session(redirect).open(bytes(32))
    with pytest.raises(MinerError, match="redirect"):
        await scripted_session(redirect).info()


async def test_oversized_and_compressed_responses_are_refused(monkeypatch):
    monkeypatch.setattr(miner_wire, "MAX_RESPONSE_BYTES", 64)

    def flood(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Chunks(*[b"[" + b"1," * 20] * 4, b"1]"))

    def compressed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=Chunks(b"{}")
        )

    with pytest.raises(MinerError, match="exceeds byte limit"):
        await scripted_session(flood).open(bytes(32))
    with pytest.raises(MinerError, match="exceeds byte limit"):
        await scripted_session(flood).turn(TURN_INPUT)
    with pytest.raises(MinerError, match="content encoding"):
        await scripted_session(compressed).open(bytes(32))


async def test_miner_error_text_is_bounded_and_single_line():
    def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom\n" * 500)

    with pytest.raises(MinerError) as failure:
        await scripted_session(fail).turn(TURN_INPUT)

    assert "\n" not in str(failure.value)
    assert len(str(failure.value)) < 400


async def test_miner_without_a_challenge_gets_no_signed_request():
    requests: list[httpx.Request] = []

    def no_challenge(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    endpoint = MinerEndpoint(ENDPOINT, transport=httpx.MockTransport(no_challenge))

    with pytest.raises(MinerError, match="authentication challenge"):
        await endpoint.session(CHANNEL, SessionKey(bytes([5]) * 32)).info()

    assert len(requests) == 1


# ---- a miner whose node lags ours --------------------------------------------------


def _open_refused_until(miner: FakeMiner, refusals: int, text: str) -> list[int]:
    """The miner answers /session/open with 400 `text` `refusals` times, then serves."""
    attempts: list[int] = []
    serve = miner._open

    def open_(payload: dict[str, Any]) -> httpx.Response:
        attempts.append(miner.chain.now)
        if len(attempts) <= refusals:
            return httpx.Response(400, json={"error": text})
        return serve(payload)

    miner._open = open_
    return attempts


async def test_open_waits_for_a_miner_whose_node_has_not_seen_the_channel(chain, miner):
    attempts = _open_refused_until(miner, 3, "channel not found on-chain")

    result = await run(chain, miner)

    assert result.settlement == "settled"
    assert len(attempts) == 4
    assert [b - a for a, b in zip(attempts, attempts[1:], strict=False)] == [
        2000,
        2000,
        2000,
    ]
    assert chain.functions() == ["force_open"]


async def test_open_stops_waiting_after_a_minute_and_recovers_the_escrow(chain, miner):
    attempts = _open_refused_until(miner, 10_000, "channel not found on-chain")

    with pytest.raises(DirectSessionError, match="channel not found") as failure:
        await run(chain, miner)

    assert failure.value.recovery == "refunded"
    assert attempts[-1] - attempts[0] >= 60_000
    assert len(attempts) <= 32
    assert miner.turn_inputs == []


async def test_any_other_open_refusal_is_not_retried(chain, miner):
    attempts = _open_refused_until(miner, 10_000, "agent key mismatch")

    with pytest.raises(DirectSessionError, match="agent key mismatch") as failure:
        await run(chain, miner)

    assert len(attempts) == 1
    assert failure.value.recovery == "refunded"
    assert miner.turn_inputs == []
