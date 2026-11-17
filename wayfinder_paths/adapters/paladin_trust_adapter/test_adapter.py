import base64
import json
from unittest.mock import patch

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from wayfinder_paths.adapters.paladin_trust_adapter.adapter import (
    PaladinTrustAdapter,
    TRANSFER_WITH_AUTHORIZATION_TYPES,
)

# Deterministic throwaway key — used only to exercise signing in tests.
TEST_PRIVATE_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
TEST_ADDRESS = Account.from_key(TEST_PRIVATE_KEY).address
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TREASURY = "0xeA8C33d018760D034384e92D1B2a7cf0338834b4"

PAYMENT_REQUIRED = {
    "x402Version": 2,
    "error": "Payment required",
    "resource": {
        "url": "https://swap.paladinfi.com/v1/trust-check",
        "description": "PaladinFi Trust Check",
        "mimeType": "application/json",
    },
    "accepts": [
        {
            "scheme": "exact",
            "network": "eip155:8453",
            "asset": USDC_BASE,
            "amount": "1000",
            "payTo": TREASURY,
            "maxTimeoutSeconds": 300,
            "extra": {"name": "USD Coin", "version": "2"},
        }
    ],
}
PAYMENT_REQUIRED_HEADER_B64 = base64.b64encode(
    json.dumps(PAYMENT_REQUIRED).encode()
).decode()

TRUST_OK = {
    "address": USDC_BASE,
    "chainId": 8453,
    "trust": {
        "risk_score": 0,
        "recommendation": "allow",
        "factors": [{"source": "ofac", "signal": "not_listed", "details": ""}],
        "version": "1.1",
    },
}
OFAC_OK = {
    "address": TREASURY,
    "chainId": 8453,
    "trust": {
        "recommendation": "allow",
        "factors": [{"source": "ofac", "signal": "not_listed", "details": ""}],
        "version": "1.1",
        "_real": True,
    },
}


class _FakeResponse:
    def __init__(self, status_code, json_body=None, headers=None):
        self.status_code = status_code
        self._json = json_body or {}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 402:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient; returns queued responses per post()."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self._responses.pop(0)


def _patch_client(responses):
    return patch(
        "wayfinder_paths.adapters.paladin_trust_adapter.adapter.httpx.AsyncClient",
        return_value=_FakeAsyncClient(responses),
    )


class TestPaladinTrustAdapter:
    @pytest.fixture
    def adapter(self):
        return PaladinTrustAdapter({"private_key": TEST_PRIVATE_KEY})

    def test_init(self):
        adapter = PaladinTrustAdapter()
        assert adapter.adapter_type == "TRUST"
        assert adapter.name == "paladin_trust_adapter"
        assert adapter._signer is None

    def test_init_builds_signer_from_private_key(self, adapter):
        assert adapter._signer is not None
        assert adapter._signer.address == TEST_ADDRESS

    @pytest.mark.asyncio
    async def test_check_token_requires_signer(self):
        adapter = PaladinTrustAdapter()
        success, result = await adapter.check_token(USDC_BASE, chain_id=8453)
        assert success is False
        assert "signer" in result

    @pytest.mark.asyncio
    async def test_screen_wallet_ofac_success(self):
        adapter = PaladinTrustAdapter()
        with _patch_client([_FakeResponse(200, OFAC_OK)]):
            success, result = await adapter.screen_wallet_ofac(TREASURY, chain_id=8453)
        assert success
        assert result["trust"]["recommendation"] == "allow"

    @pytest.mark.asyncio
    async def test_screen_wallet_ofac_error(self):
        adapter = PaladinTrustAdapter()
        with _patch_client([_FakeResponse(500)]):
            success, result = await adapter.screen_wallet_ofac(TREASURY)
        assert success is False
        assert "500" in result

    @pytest.mark.asyncio
    async def test_check_token_pays_402_then_succeeds(self, adapter):
        responses = [
            _FakeResponse(402, headers={"payment-required": PAYMENT_REQUIRED_HEADER_B64}),
            _FakeResponse(200, TRUST_OK),
        ]
        fake = _FakeAsyncClient(responses)
        with patch(
            "wayfinder_paths.adapters.paladin_trust_adapter.adapter.httpx.AsyncClient",
            return_value=fake,
        ):
            success, result = await adapter.check_token(USDC_BASE, chain_id=8453)
        assert success
        assert result["trust"]["recommendation"] == "allow"
        # second call must carry the payment-signature header
        assert fake.calls[1]["headers"] is not None
        assert "payment-signature" in fake.calls[1]["headers"]

    def test_payment_header_signature_recovers_to_signer(self, adapter):
        header_b64 = adapter._build_payment_header(
            {"payment-required": PAYMENT_REQUIRED_HEADER_B64}
        )
        decoded = json.loads(base64.b64decode(header_b64))
        auth = decoded["payload"]["authorization"]
        signature = decoded["payload"]["signature"]

        domain = {
            "name": "USD Coin",
            "version": "2",
            "chainId": 8453,
            "verifyingContract": USDC_BASE,
        }
        message = {
            "from": auth["from"],
            "to": auth["to"],
            "value": int(auth["value"]),
            "validAfter": int(auth["validAfter"]),
            "validBefore": int(auth["validBefore"]),
            "nonce": bytes.fromhex(auth["nonce"].removeprefix("0x")),
        }
        signable = encode_typed_data(
            domain_data=domain,
            message_types=TRANSFER_WITH_AUTHORIZATION_TYPES,
            message_data=message,
        )
        recovered = Account.recover_message(signable, signature=bytes.fromhex(signature.removeprefix("0x")))
        assert recovered == TEST_ADDRESS

    def test_payment_header_structure(self, adapter):
        header_b64 = adapter._build_payment_header(
            {"payment-required": PAYMENT_REQUIRED_HEADER_B64}
        )
        decoded = json.loads(base64.b64decode(header_b64))
        assert decoded["x402Version"] == 2
        assert decoded["accepted"]["payTo"] == TREASURY
        assert decoded["accepted"]["amount"] == "1000"
        auth = decoded["payload"]["authorization"]
        assert auth["from"] == TEST_ADDRESS
        assert auth["to"] == TREASURY
        assert auth["value"] == "1000"
        assert auth["nonce"].startswith("0x") and len(auth["nonce"]) == 66
        assert int(auth["validBefore"]) > int(auth["validAfter"])

    def test_build_payment_header_missing_header_raises(self, adapter):
        with pytest.raises(ValueError, match="payment-required"):
            adapter._build_payment_header({})

    def test_can_pay(self):
        assert PaladinTrustAdapter().can_pay is False
        assert PaladinTrustAdapter({"private_key": TEST_PRIVATE_KEY}).can_pay is True

    def test_select_payment_option_no_match_raises(self, adapter):
        with pytest.raises(ValueError, match="no exact"):
            adapter._select_payment_option([{"scheme": "exact", "network": "eip155:1"}])

    def test_validity_window_bounds(self, adapter):
        header = adapter._build_payment_header(
            {"payment-required": PAYMENT_REQUIRED_HEADER_B64}
        )
        auth = json.loads(base64.b64decode(header))["payload"]["authorization"]
        # backdate 600s + maxTimeoutSeconds (300 in the fixture) => 900s window
        assert int(auth["validBefore"]) - int(auth["validAfter"]) == 900

    def test_refuses_oversized_payment(self, adapter):
        required = dict(PAYMENT_REQUIRED)
        required["accepts"] = [dict(PAYMENT_REQUIRED["accepts"][0], amount="999999999")]
        header_b64 = base64.b64encode(json.dumps(required).encode()).decode()
        with pytest.raises(ValueError, match="max_fee_atomic"):
            adapter._build_payment_header({"payment-required": header_b64})
