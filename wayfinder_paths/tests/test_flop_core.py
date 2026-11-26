"""FLOP account helpers, local state, faucet and gateway clients (no network)."""

import asyncio
import hashlib
import json
import stat
from pathlib import Path

import httpx
import pytest
from eth_keys import keys as eth_keys

from wayfinder_paths.core.flop import account, faucet, gateway, state
from wayfinder_paths.core.flop.networks import DEVNET, TESTNET
from wayfinder_paths.core.flop.signer import LocalFlopSigner

FIXTURES = Path(__file__).parent / "fixtures" / "flop"
# A throwaway devnet key's public half and the address the chain derived for it.
COMPRESSED_KEY = "027765f496ae000d2ba3861c708deaa593cd4386c46a5a8e6ed2ac7fa06744d39d"
ADDRESS = "5CY2922WBXSso1pELFnqQWZy7DeKvLS63P6Cz488X2h1NFzq"


@pytest.fixture(autouse=True)
def flop_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYFINDER_FLOP_STATE_DIR", str(tmp_path / "flop"))
    return tmp_path / "flop"


def _fake_http(monkeypatch, module, handler):
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", client)


# ---- account ------------------------------------------------------------------------


def test_address_derivation_matches_the_chain():
    account_id = account.account_id_from_public_key(bytes.fromhex(COMPRESSED_KEY))
    assert account.address_from_account_id(account_id) == ADDRESS
    assert account.account_id_from_address(ADDRESS) == account_id


def test_signing_digest_hashes_long_payloads_twice():
    short, long = b"a" * 256, b"a" * 257
    blake = lambda b: hashlib.blake2b(b, digest_size=32).digest()  # noqa: E731
    assert account.signing_digest(short) == blake(short)
    assert account.signing_digest(long) == blake(blake(long))


def test_local_signer_signature_recovers_to_its_own_account():
    signer = LocalFlopSigner(bytes(range(1, 33)))
    vectors = json.loads((FIXTURES / "signing-payloads.json").read_text())["vectors"]
    payload = bytes.fromhex(vectors[0]["payload"][2:])
    signature = asyncio.run(signer.sign_payload(payload))
    recovered = account.recover_compressed_public_key(
        account.signing_digest(payload), signature
    )
    assert account.account_id_from_public_key(recovered) == signer.account_id


def test_normalise_signature_maps_ethereum_v():
    body = bytes(64)
    assert account.normalise_signature(body + b"\x1b")[64] == 0
    assert account.normalise_signature(body + b"\x1c")[64] == 1
    assert account.normalise_signature(body + b"\x01")[64] == 1
    with pytest.raises(ValueError):
        account.normalise_signature(body)


@pytest.mark.parametrize(
    ("flop", "raw"),
    [
        ("0", 0),
        ("1", 10**18),
        ("0.05", 5 * 10**16),
        ("2.989462518515", 2989462518515000000),
    ],
)
def test_flop_amount_round_trip(flop, raw):
    assert account.flop_to_raw(flop) == raw
    assert account.raw_to_flop(raw) == flop


def test_flop_amount_rejects_more_than_18_decimals():
    with pytest.raises(ValueError):
        account.flop_to_raw("0.0000000000000000001")


# ---- state --------------------------------------------------------------------------


def test_state_file_is_private(flop_state_dir):
    state.update_state(gateway={"addr": {"key": "secret"}})
    mode = stat.S_IMODE((flop_state_dir / "state.json").stat().st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(flop_state_dir.stat().st_mode) == 0o700


def test_spend_ledger_keeps_only_today():
    day_one, day_two = 1_800_000_000, 1_800_000_000 + 86_400
    assert state.add_spend(5, now=day_one) == 5
    assert state.add_spend(7, now=day_one) == 12
    assert state.spent_today(now=day_one) == 12
    assert state.spent_today(now=day_two) == 0
    assert state.add_spend(1, now=day_two) == 1
    assert list(state.read_state()["spend"]) == [state.utc_day(day_two)]


def test_activity_log_is_bounded():
    for i in range(state.MAX_ACTIVITY_ENTRIES + 5):
        state.record_activity({"n": i})
    log = state.read_state()["activity"]
    assert len(log) == state.MAX_ACTIVITY_ENTRIES
    assert log[-1]["n"] == state.MAX_ACTIVITY_ENTRIES + 4


# ---- faucet -------------------------------------------------------------------------


class _Address:
    address = ADDRESS


DEV = _Address()


def _identity(network, genesis=None):
    return httpx.Response(
        200,
        json={"network": network.name, "genesisHash": genesis or network.genesis_hash},
    )


def test_faucet_grant_is_recorded_and_not_asked_again_within_a_day(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/chain":
            return _identity(DEVNET)
        assert request.url.host == "faucet.devnet.flop.network"
        if request.url.path == "/limits":
            return httpx.Response(200, json={"faucet": {"status": "open"}})
        assert json.loads(request.content) == {"address": ADDRESS}
        return httpx.Response(
            200,
            json={
                "success": True,
                "amount": 25,
                "tier": "anonymous",
                "txHash": "0xabc",
            },
        )

    _fake_http(monkeypatch, faucet, handler)
    first = asyncio.run(faucet.collect_if_due(DEV, DEVNET, now=1_000_000))
    assert first.granted and first.amount_flop == 25 and first.tx_hash == "0xabc"
    again = asyncio.run(faucet.collect_if_due(DEV, DEVNET, now=1_000_000 + 3600))
    assert not again.granted and again.reason == "not_due"
    assert again.retry_at == 1_000_000 + faucet.GRANT_COOLDOWN_SECONDS
    assert calls == [("GET", "/chain"), ("GET", "/limits"), ("POST", "/fund")]
    later = asyncio.run(
        faucet.collect_if_due(
            DEV, DEVNET, now=1_000_000 + faucet.GRANT_COOLDOWN_SECONDS
        )
    )
    assert later.granted


def test_faucet_refusal_is_recorded_with_its_wait_and_not_retried(monkeypatch):
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/chain":
            return _identity(DEVNET)
        if request.url.path == "/limits":
            return httpx.Response(200, json={"faucet": {"status": "open"}})
        posts.append(1)
        return httpx.Response(
            429,
            json={
                "error": "This address was funded recently.",
                "reason": "address_cooldown",
                "action": "retry_later",
                "retryAfterSeconds": 7200,
            },
        )

    _fake_http(monkeypatch, faucet, handler)
    refused = asyncio.run(faucet.collect_if_due(DEV, DEVNET, now=5_000))
    assert not refused.granted
    assert (refused.reason, refused.action, refused.retry_at) == (
        "address_cooldown",
        "retry_later",
        5_000 + 7200,
    )
    asyncio.run(faucet.collect_if_due(DEV, DEVNET, now=5_000 + 60))
    assert len(posts) == 1


@pytest.mark.parametrize("answer", ["timeout", "html"])
def test_faucet_answer_that_cannot_be_read_is_unknown_and_held_off(monkeypatch, answer):
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/chain":
            return _identity(DEVNET)
        if request.url.path == "/limits":
            return httpx.Response(200, json={"faucet": {"status": "open"}})
        posts.append(1)
        if answer == "timeout":
            raise httpx.ReadTimeout("no answer", request=request)
        return httpx.Response(502, text="<html>bad gateway</html>")

    _fake_http(monkeypatch, faucet, handler)
    result = asyncio.run(faucet.collect_if_due(DEV, DEVNET, now=5_000))
    assert not result.granted
    assert (result.reason, result.retry_at) == (
        "unknown",
        5_000 + faucet.UNKNOWN_HOLD_SECONDS,
    )
    again = asyncio.run(faucet.collect_if_due(DEV, DEVNET, now=5_000 + 60))
    assert again.reason == "not_due"
    assert len(posts) == 1


def test_faucet_is_not_asked_when_closed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/chain":
            return _identity(DEVNET)
        assert request.url.path == "/limits"
        return httpx.Response(200, json={"faucet": {"status": "paused"}})

    _fake_http(monkeypatch, faucet, handler)
    result = asyncio.run(faucet.collect_if_due(DEV, DEVNET, now=1))
    assert not result.granted and result.reason == "faucet_closed"


# ---- testnet agent claims --------------------------------------------------------------


class _CountingSigner(LocalFlopSigner):
    def __init__(self, key: bytes) -> None:
        super().__init__(key)
        self.claims: list[tuple[str, str]] = []

    async def sign_faucet_claim(self, message: str, nonce: str) -> bytes:
        self.claims.append((message, nonce))
        return await super().sign_faucet_claim(message, nonce)


def _testnet_faucet(monkeypatch, answer, *, limits=None, genesis=None):
    """A testnet faucet whose claim route answers with `answer(body)`; returns the posts."""
    posts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "faucet.flop.network"
        if request.url.path == "/chain":
            return _identity(TESTNET, genesis)
        if request.url.path == "/limits":
            return httpx.Response(
                200,
                json=limits
                or {
                    "faucet": {
                        "status": "open",
                        "agent": {"claimPath": faucet.AGENT_CLAIM_PATH},
                    }
                },
            )
        assert (request.method, request.url.path) == ("POST", faucet.AGENT_CLAIM_PATH)
        assert request.headers["content-type"] == "application/json"
        body = json.loads(request.content)
        posts.append(body)
        return answer(body, request)

    _fake_http(monkeypatch, faucet, handler)
    return posts


def _answer(status):
    def answer(body, request):
        return httpx.Response(
            200,
            json={
                "success": True,
                "amount": 10,
                "txHash": "0x" + "ab" * 32,
                "status": status,
            },
        )

    return answer


_granted = _answer("finalized")


def test_a_testnet_claim_signs_the_versioned_text_with_the_account_key(monkeypatch):
    signer = _CountingSigner(bytes([7]) * 32)
    posts = _testnet_faucet(monkeypatch, _granted)
    result = asyncio.run(faucet.collect_if_due(signer, TESTNET, now=1_700_000_000))
    assert result.granted and result.amount_flop == 10
    assert result.transfer_status == "finalized"
    (body,) = posts
    nonce = "1700000000000"
    assert body["address"] == signer.address and body["nonce"] == nonce
    message = f"flop-faucet-agent/v1|{TESTNET.genesis_hash}|{signer.address}|{nonce}"
    assert signer.claims == [(message, nonce)]
    signature = bytes.fromhex(body["signature"][2:])
    recovered = account.recover_compressed_public_key(
        account.blake2_256(message.encode()), signature
    )
    assert account.account_id_from_public_key(recovered) == signer.account_id
    record = state.read_state()["faucet"][signer.address]
    assert (
        record["pending_claim"] is None and record["last_tx_hash"] == "0x" + "ab" * 32
    )


@pytest.mark.parametrize("status", ["included", None])
def test_a_transfer_that_is_not_final_is_no_grant_until_it_is(monkeypatch, status):
    signer = _CountingSigner(bytes([11]) * 32)
    answers = iter([_answer(status), _answer("finalized")])
    posts = _testnet_faucet(
        monkeypatch, lambda body, request: next(answers)(body, request)
    )
    start = 30_000

    first = asyncio.run(faucet.collect_if_due(signer, TESTNET, now=start))
    assert not first.granted and first.reason == "transfer_not_final"
    assert first.tx_hash == "0x" + "ab" * 32 and first.action == "retry_later"
    record = state.read_state()["faucet"][signer.address]
    # No grant recorded and no 24h cooldown; the claim and its transaction are kept.
    assert "last_grant_at" not in record
    assert record["pending_claim"]["tx_hash"] == "0x" + "ab" * 32
    assert faucet.next_eligible_at(signer.address) == (
        start + faucet.TRANSFER_PENDING_HOLD_SECONDS
    )

    later = start + faucet.TRANSFER_PENDING_HOLD_SECONDS
    final = asyncio.run(faucet.collect_if_due(signer, TESTNET, now=later))
    assert final.granted and final.transfer_status == "finalized"
    # The same claim was repeated: same nonce and signature, and our tx hash stays local.
    assert len(posts) == 2 and posts[0] == posts[1]
    assert set(posts[1]) == {"address", "nonce", "signature"}
    assert len(signer.claims) == 1
    record = state.read_state()["faucet"][signer.address]
    assert record["pending_claim"] is None and record["last_grant_at"] == later
    assert faucet.next_eligible_at(signer.address) == (
        later + faucet.GRANT_COOLDOWN_SECONDS
    )


def test_an_unclear_claim_is_repeated_with_the_same_nonce_and_signature(monkeypatch):
    signer = _CountingSigner(bytes([8]) * 32)
    answers = iter(["timeout", "grant"])

    def answer(body, request):
        if next(answers) == "timeout":
            raise httpx.ReadTimeout("no answer", request=request)
        return _granted(body, request)

    posts = _testnet_faucet(monkeypatch, answer)
    first = asyncio.run(faucet.collect_if_due(signer, TESTNET, now=10_000))
    assert first.reason == "unknown"
    assert (
        asyncio.run(faucet.collect_if_due(signer, TESTNET, now=10_060)).reason
        == "not_due"
    )
    later = 10_000 + faucet.UNKNOWN_HOLD_SECONDS
    assert asyncio.run(faucet.collect_if_due(signer, TESTNET, now=later)).granted
    assert len(posts) == 2 and posts[0] == posts[1]
    assert len(signer.claims) == 1


def test_a_retry_later_claim_keeps_its_nonce(monkeypatch):
    signer = _CountingSigner(bytes([9]) * 32)
    answers = iter(["busy", "grant"])

    def answer(body, request):
        if next(answers) == "busy":
            return httpx.Response(
                503,
                json={
                    "success": False,
                    "error": "Indexer unavailable; retry later",
                    "reason": "indexer_unavailable",
                    "action": "retry_later",
                    "retryAfterSeconds": 60,
                },
            )
        return _granted(body, request)

    posts = _testnet_faucet(monkeypatch, answer)
    busy = asyncio.run(faucet.collect_if_due(signer, TESTNET, now=20_000))
    assert (busy.reason, busy.retry_at) == ("indexer_unavailable", 20_060)
    assert asyncio.run(faucet.collect_if_due(signer, TESTNET, now=20_060)).granted
    assert posts[0] == posts[1] and len(signer.claims) == 1


def test_the_next_claim_waits_for_a_spend_when_the_faucet_asks_for_one(monkeypatch):
    signer = _CountingSigner(bytes([10]) * 32)
    answers = iter(["spend", "grant"])

    def answer(body, request):
        if next(answers) == "spend":
            return httpx.Response(
                403,
                json={
                    "success": False,
                    "error": "Spend the last grant first",
                    "reason": "agent_spend_missing",
                    "action": "fix_request",
                },
            )
        return _granted(body, request)

    posts = _testnet_faucet(monkeypatch, answer)
    refused = asyncio.run(faucet.collect_if_due(signer, TESTNET, now=30_000))
    assert (refused.reason, refused.action) == ("agent_spend_missing", "spend_first")
    assert faucet.waiting_for_spend(signer.address)
    held = asyncio.run(faucet.collect_if_due(signer, TESTNET, now=30_000 + 10 * 86400))
    assert held.reason == "agent_spend_missing" and len(posts) == 1
    state.add_spend(5 * 10**16, now=30_500)
    assert not faucet.waiting_for_spend(signer.address)
    assert asyncio.run(faucet.collect_if_due(signer, TESTNET, now=31_000)).granted
    assert len(posts) == 2 and posts[0]["nonce"] != posts[1]["nonce"]


def test_a_faucet_serving_another_genesis_is_not_signed_for(monkeypatch):
    signer = _CountingSigner(bytes([11]) * 32)
    posts = _testnet_faucet(monkeypatch, _granted, genesis="0x" + "11" * 32)
    with pytest.raises(faucet.FaucetIdentityError):
        asyncio.run(faucet.collect_if_due(signer, TESTNET, now=1))
    assert posts == [] and signer.claims == []


def test_a_claim_signature_from_another_key_is_never_sent(monkeypatch):
    signer = _CountingSigner(bytes([12]) * 32)
    other = LocalFlopSigner(bytes([13]) * 32)

    async def wrong(message, nonce):
        return await other.sign_faucet_claim(message, nonce)

    signer.sign_faucet_claim = wrong
    posts = _testnet_faucet(monkeypatch, _granted)
    with pytest.raises(ValueError, match="not the account's"):
        asyncio.run(faucet.collect_if_due(signer, TESTNET, now=1))
    assert posts == []


def test_a_testnet_faucet_without_agent_claims_is_not_asked(monkeypatch):
    signer = _CountingSigner(bytes([14]) * 32)
    posts = _testnet_faucet(
        monkeypatch, _granted, limits={"faucet": {"status": "open", "agent": None}}
    )
    result = asyncio.run(faucet.collect_if_due(signer, TESTNET, now=1))
    assert result.reason == "faucet_closed" and posts == [] and signer.claims == []


def test_a_per_ip_claim_refusal_says_the_address_is_shared(monkeypatch):
    signer = _CountingSigner(bytes([15]) * 32)

    def answer(body, request):
        return httpx.Response(
            429,
            json={
                "success": False,
                "error": "This IP funded recently; retry in 947 min",
                "reason": "ip_cooldown",
                "action": "retry_later",
                "retryAfterSeconds": 56807,
            },
        )

    _testnet_faucet(monkeypatch, answer)
    refused = asyncio.run(faucet.collect_if_due(signer, TESTNET, now=1))
    assert refused.action == "retry_later" and refused.retry_at == 56808
    assert "Shells share outbound addresses" in refused.message


def test_claim_nonces_are_canonical_decimals():
    with pytest.raises(ValueError):
        faucet.agent_claim_message(TESTNET.genesis_hash, ADDRESS, "0123")
    with pytest.raises(ValueError):
        faucet.agent_claim_message(TESTNET.genesis_hash, ADDRESS, "1" * 20)


# ---- gateway ------------------------------------------------------------------------


def test_gateway_enrollment_signs_the_challenge_and_stores_the_key(monkeypatch):
    signer = LocalFlopSigner(bytes(range(1, 33)))
    message = f"api.flop.network wants you to enroll\n{signer.address}\n\nNonce: 1"
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.flop.network"
        if request.url.path.endswith("/enroll/wallet/challenge"):
            assert request.url.params["address"] == signer.address
            return httpx.Response(200, json={"message": message, "nonce": "1"})
        seen.update(json.loads(request.content))
        return httpx.Response(
            201,
            json={
                "key": "flop_secret",
                "model": "m",
                "remaining_quota": 1,
                "expires_at": 9_999_999_999_000,
            },
        )

    _fake_http(monkeypatch, gateway, handler)
    public = asyncio.run(gateway.enroll(signer))
    assert public == {"model": "m", "expires_at": 9_999_999_999}
    digest = hashlib.blake2b(message.encode(), digest_size=32).digest()
    signature = eth_keys.Signature(bytes.fromhex(seen["signature"][2:]))
    recovered = signature.recover_public_key_from_msg_hash(digest).to_compressed_bytes()
    assert account.account_id_from_public_key(recovered) == signer.account_id
    assert seen["message"] == message and seen["address"] == signer.address
    assert gateway.stored_key(signer.address)["key"] == "flop_secret"
    assert gateway.stored_key(signer.address, now=9_999_999_999) is None


def test_gateway_chat_waits_out_a_full_pool_then_returns(monkeypatch):
    statuses = iter([429, 200])
    keys = []
    waits = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer k"
        keys.append(request.headers["idempotency-key"])
        if next(statuses) == 429:
            return httpx.Response(
                429, json={"error": {"code": "overloaded", "message": "full"}}
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "pong"}}],
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 1,
                    "cost_flop": 0.066,
                },
                "x_flop_receipt": {"channelId": "0x01", "miner": "5F"},
            },
        )

    async def sleep(seconds):
        waits.append(seconds)

    _fake_http(monkeypatch, gateway, handler)
    reply = asyncio.run(
        gateway.chat(
            "k",
            "m",
            [{"role": "user", "content": "ping"}],
            max_tokens=4,
            max_cost_flop=1,
            sleep=sleep,
        )
    )
    assert (
        reply.text == "pong"
        and reply.cost_flop == 0.066
        and reply.receipt["miner"] == "5F"
    )
    assert waits == [gateway.OVERLOAD_WAIT_SECONDS]
    assert len(set(keys)) == 2


def test_gateway_chat_gives_up_on_other_errors(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402, json={"error": {"code": "max_cost_exceeded", "message": "too dear"}}
        )

    _fake_http(monkeypatch, gateway, handler)
    with pytest.raises(gateway.GatewayError) as caught:
        asyncio.run(gateway.chat("k", "m", [], max_tokens=1, max_cost_flop=1))
    assert caught.value.code == "max_cost_exceeded"


def test_gateway_body_that_is_not_json_is_a_gateway_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    _fake_http(monkeypatch, gateway, handler)
    with pytest.raises(gateway.GatewayError) as caught:
        asyncio.run(gateway.models())
    assert (caught.value.status, caught.value.code) == (502, "invalid_response")


def test_gateway_enrollment_refusal_keeps_its_code_and_sentence(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/challenge"):
            return httpx.Response(200, json={"message": "challenge"})
        return httpx.Response(
            503,
            json={
                "error": "wallet_enrollment_unavailable",
                "message": "Wallet enrollment is temporarily unavailable.",
            },
        )

    signer = LocalFlopSigner(bytes.fromhex("11" * 32))
    _fake_http(monkeypatch, gateway, handler)
    with pytest.raises(gateway.GatewayError) as caught:
        asyncio.run(gateway.enroll(signer))
    assert caught.value.code == "wallet_enrollment_unavailable"
    assert caught.value.message == "Wallet enrollment is temporarily unavailable."
    assert gateway.stored_key(signer.address) is None


# ---- signing freshness ---------------------------------------------------------------


class _Value:
    def __init__(self, value):
        self.value = value


class _Node:
    def __init__(self, block_ms: int, stamp_ms: int) -> None:
        self.block_ms = block_ms
        self.stamp_ms = stamp_ms

    def get_constant(self, module, name):
        assert (module, name) == ("Babe", "ExpectedBlockTime")
        return _Value(self.block_ms)

    def query(self, module, name, params=None, block_hash=None):
        assert (module, name, block_hash) == ("Timestamp", "Now", "0xhead")
        return _Value(self.stamp_ms)


@pytest.mark.parametrize(
    ("drift_ms", "fresh"),
    [(0, True), (16_000, True), (-16_000, True), (16_001, False), (-16_001, False)],
)
def test_signing_needs_a_finalized_head_near_the_local_clock(drift_ms, fresh):
    from wayfinder_paths.core.flop.chain import FlopChain, FlopChainError

    chain = FlopChain()
    now = 1_800_000_000_000
    chain._now_ms = lambda: now
    # 1 s blocks: a 64-block era is 64 s, and a quarter of it is 16 s.
    node = _Node(block_ms=1000, stamp_ms=now - drift_ms)
    if fresh:
        chain._check_head_fresh(node, "0xhead")
    else:
        with pytest.raises(FlopChainError, match="not signing"):
            chain._check_head_fresh(node, "0xhead")


def _stale_then_fresh_chain(stale_reads: int):
    from wayfinder_paths.core.flop.chain import FlopChain, FlopStaleHeadError

    chain = FlopChain()
    clock = {"ms": 1_800_000_000_000}
    chain._now_ms = lambda: clock["ms"]
    sleeps: list[float] = []
    reads = {"n": 0}

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["ms"] += int(seconds * 1000)

    async def run(operation):
        reads["n"] += 1
        if reads["n"] <= stale_reads:
            raise FlopStaleHeadError("the FLOP node's finalized head is 22s behind")
        return "call", 7, {"period": 64, "current": 100}, b"payload"

    chain._sleep = sleep
    chain._run = run
    chain._send = lambda call, nonce, era, signature, account_id: ("sent", nonce)
    return chain, sleeps, reads


class _PayloadSigner:
    address = "5signer"
    account_id = b"\x01" * 32

    def __init__(self) -> None:
        self.signed: list[bytes] = []

    async def sign_payload(self, payload: bytes) -> bytes:
        self.signed.append(payload)
        return b"sig"


async def test_a_lagging_finalized_head_is_waited_out_before_signing():
    chain, sleeps, reads = _stale_then_fresh_chain(stale_reads=3)
    signer = _PayloadSigner()

    assert await chain.submit("M", "f", {}, signer) == ("sent", 7)
    assert sleeps == [2.0, 2.0, 2.0]
    assert reads["n"] == 4
    assert signer.signed == [b"payload"]


async def test_a_head_still_stale_after_the_wait_is_refused_unsigned():
    from wayfinder_paths.core.flop.chain import FlopStaleHeadError

    chain, sleeps, _ = _stale_then_fresh_chain(stale_reads=1_000)
    signer = _PayloadSigner()

    with pytest.raises(FlopStaleHeadError, match="behind"):
        await chain.submit("M", "f", {}, signer)
    assert sum(sleeps) == 30
    assert signer.signed == []


def test_signing_is_refused_when_the_block_time_cannot_be_read():
    from wayfinder_paths.core.flop.chain import FlopChain, FlopChainError

    chain = FlopChain()
    chain._now_ms = lambda: 5
    with pytest.raises(FlopChainError, match="block time"):
        chain._check_head_fresh(_Node(block_ms=0, stamp_ms=5), "0xhead")


@pytest.mark.parametrize(
    ("status", "body", "per_ip"),
    [
        (
            429,
            {"error": "Too many grants from this IP today", "reason": "ip_daily_limit"},
            True,
        ),
        (429, {"error": "Too many requests from this IP"}, True),
        (429, {"error": "slow down", "reason": "client_rate_limited"}, True),
        (
            429,
            {
                "error": "This address was funded recently.",
                "reason": "address_cooldown",
            },
            False,
        ),
        (403, {"error": "faucet paused", "reason": "paused"}, False),
    ],
)
def test_a_per_ip_faucet_refusal_points_to_the_panel(monkeypatch, status, body, per_ip):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    _fake_http(monkeypatch, faucet, handler)
    refused = asyncio.run(faucet.fund(ADDRESS, DEVNET, now=1))
    assert not refused.granted
    assert (refused.action == "use_flop_panel_faucet") is per_ip
    assert ("FLOP panel" in refused.message) is per_ip


# ---- waiting for inclusion -------------------------------------------------------------


class _Extrinsic:
    data = "0xfeed"
    extrinsic_hash = bytes([0xAB]) * 32


class _WatchingNode:
    def __init__(self, statuses):
        self.statuses = statuses
        self.calls = []

    def rpc_request(self, method, params, result_handler=None):
        self.calls.append(method)
        if result_handler is None:
            return {"result": True}
        for number, status in enumerate(self.statuses):
            outcome = result_handler(
                {"params": {"result": status, "subscription": "sub"}}, number, "sub"
            )
            if outcome is not None:
                return outcome
        raise AssertionError("the watch never ended")


def test_inclusion_returns_the_block_it_landed_in():
    from wayfinder_paths.core.flop.chain import _submit_and_wait_for_inclusion

    node = _WatchingNode(["ready", {"broadcast": ["peer"]}, {"inBlock": "0xb10c"}])
    receipt = _submit_and_wait_for_inclusion(node, _Extrinsic())
    assert receipt.block_hash == "0xb10c"
    assert receipt.extrinsic_hash == "0x" + "ab" * 32
    assert node.calls == ["author_submitAndWatchExtrinsic", "author_unwatchExtrinsic"]


@pytest.mark.parametrize(
    ("status", "says"),
    [
        ("invalid", "cannot be included later"),
        ("Invalid", "cannot be included later"),
        ({"usurped": "0x1"}, "cannot be included later"),
        ("dropped", "may still be included"),
    ],
)
def test_a_transaction_the_node_gives_up_on_ends_the_wait(status, says):
    from wayfinder_paths.core.flop.chain import (
        FlopChainError,
        _submit_and_wait_for_inclusion,
    )

    node = _WatchingNode(["ready", status, {"inBlock": "0xnever"}])
    with pytest.raises(FlopChainError, match=says):
        _submit_and_wait_for_inclusion(node, _Extrinsic())
