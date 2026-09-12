from __future__ import annotations

from typing import Any

import pytest
from eth_account.messages import defunct_hash_message

from wayfinder_paths.adapters.derive_adapter.adapter import (
    DERIVE_MAINNET_API_BASE_URL,
    DERIVE_TESTNET_API_BASE_URL,
    DeriveAdapter,
)
from wayfinder_paths.mcp.scripting import get_adapter


class FakeResponse:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.data


class FakeAsyncClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = [FakeResponse(item) for item in responses]
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    async def post(
        self,
        path: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> FakeResponse:
        self.requests.append({"path": path, "json": json, "headers": headers})
        return self.responses.pop(0)

    async def aclose(self) -> None:
        self.closed = True


def _signed_order() -> dict[str, Any]:
    return {
        "subaccount_id": 12345,
        "instrument_name": "ETH-20260522-2500-C",
        "direction": "buy",
        "amount": "0.1",
        "limit_price": "20",
        "max_fee": "2",
        "nonce": 1779327000000001,
        "signature_expiry_sec": 1779327600,
        "signer": "0x1111111111111111111111111111111111111111",
        "signature": "0x" + "22" * 65,
    }


def _unsigned_order() -> dict[str, Any]:
    return {
        "subaccount_id": 12345,
        "instrument_name": "ETH-20260522-2500-C",
        "direction": "BUY",
        "amount": "0.1",
        "limit_price": "20",
        "max_fee": "2",
        "order_type": "limit",
        "time_in_force": "gtc",
    }


async def _fixed_signature(_hash_hex: str) -> str:
    return "0x" + "33" * 65


def test_adapter_type_and_config_defaults() -> None:
    adapter = DeriveAdapter(config={}, http_client=FakeAsyncClient([]))

    assert adapter.adapter_type == "DERIVE"
    assert adapter.name == "derive_adapter"
    assert adapter.api_base_url == DERIVE_MAINNET_API_BASE_URL
    assert adapter.wallet_address is None


def test_testnet_config_selects_demo_api() -> None:
    adapter = DeriveAdapter(
        config={"derive": {"network": "testnet"}},
        http_client=FakeAsyncClient([]),
    )

    assert adapter.api_base_url == DERIVE_TESTNET_API_BASE_URL


@pytest.mark.asyncio
async def test_get_adapter_wires_wallet_and_hash_signer(monkeypatch) -> None:
    wallet = {
        "label": "derive",
        "address": "0x1111111111111111111111111111111111111111",
        "private_key_hex": "0x" + "11" * 32,
    }

    async def find_wallet_by_label(label: str) -> dict[str, Any] | None:
        return wallet if label == "derive" else None

    monkeypatch.setattr(
        "wayfinder_paths.core.utils.wallets.find_wallet_by_label",
        find_wallet_by_label,
    )
    monkeypatch.setattr("wayfinder_paths.mcp.scripting.CONFIG", {})

    adapter = await get_adapter(
        DeriveAdapter, "derive", http_client=FakeAsyncClient([])
    )

    assert adapter.wallet_address == wallet["address"]
    assert adapter.derive_wallet_address == wallet["address"]
    assert adapter.sign_callback is not None
    assert adapter.sign_hash_callback is not None


@pytest.mark.asyncio
async def test_get_instruments_posts_public_payload() -> None:
    http = FakeAsyncClient(
        [
            {
                "id": "1",
                "result": [
                    {
                        "instrument_name": "ETH-20260522-2500-C",
                        "instrument_type": "option",
                    }
                ],
            }
        ]
    )
    adapter = DeriveAdapter(config={}, http_client=http)

    ok, instruments = await adapter.get_instruments(
        currency="eth",
        instrument_type="option",
        expired=False,
    )

    assert ok is True
    assert instruments == [
        {"instrument_name": "ETH-20260522-2500-C", "instrument_type": "option"}
    ]
    assert http.requests == [
        {
            "path": "/public/get_instruments",
            "json": {
                "currency": "ETH",
                "instrument_type": "option",
                "expired": False,
            },
            "headers": None,
        }
    ]


@pytest.mark.asyncio
async def test_list_option_expiries_groups_option_instruments() -> None:
    http = FakeAsyncClient(
        [
            {
                "id": "1",
                "result": [
                    {"option_details": {"expiry": 1779408000}},
                    {"option_details": {"expiry": 1779408000}},
                    {"option_details": {"expiry": 1780012800}},
                ],
            }
        ]
    )
    adapter = DeriveAdapter(config={}, http_client=http)

    ok, expiries = await adapter.list_option_expiries(currency="ETH")

    assert ok is True
    assert expiries == [
        {
            "expiry": 1779408000,
            "expiry_date": "20260522",
            "instrument_count": 2,
        },
        {
            "expiry": 1780012800,
            "expiry_date": "20260529",
            "instrument_count": 1,
        },
    ]


@pytest.mark.asyncio
async def test_get_option_tickers_requires_option_params() -> None:
    adapter = DeriveAdapter(config={}, http_client=FakeAsyncClient([]))

    ok, error = await adapter.get_tickers(instrument_type="option", currency="ETH")

    assert ok is False
    assert "currency and expiry_date" in error


@pytest.mark.asyncio
async def test_get_option_tickers_returns_tickers_map() -> None:
    http = FakeAsyncClient(
        [
            {
                "id": "1",
                "result": {
                    "tickers": {
                        "ETH-20260522-2500-C": {
                            "b": "10",
                            "a": "11",
                            "M": "10.5",
                        }
                    }
                },
            }
        ]
    )
    adapter = DeriveAdapter(config={}, http_client=http)

    ok, tickers = await adapter.get_option_tickers(
        currency="eth",
        expiry_date=20260522,
    )

    assert ok is True
    assert tickers["ETH-20260522-2500-C"]["M"] == "10.5"
    assert http.requests[0]["json"] == {
        "instrument_type": "option",
        "currency": "ETH",
        "expiry_date": "20260522",
    }


@pytest.mark.asyncio
async def test_private_read_builds_derive_auth_headers(monkeypatch) -> None:
    captured_hashes: list[str] = []

    async def sign_hash(hash_hex: str) -> str:
        captured_hashes.append(hash_hex)
        return "0x" + "33" * 65

    monkeypatch.setattr("time.time", lambda: 1779327000.123)
    http = FakeAsyncClient(
        [{"id": "1", "result": {"subaccount_id": 12345, "positions": []}}]
    )
    adapter = DeriveAdapter(
        config={},
        sign_hash_callback=sign_hash,
        derive_wallet_address="0x1111111111111111111111111111111111111111",
        http_client=http,
    )

    ok, result = await adapter.get_positions(subaccount_id=12345)

    assert ok is True
    assert result == {"subaccount_id": 12345, "positions": []}
    expected_digest = defunct_hash_message(text="1779327000123")
    assert captured_hashes == [f"0x{bytes(expected_digest).hex()}"]
    assert http.requests[0]["headers"] == {
        "X-LyraWallet": "0x1111111111111111111111111111111111111111",
        "X-LyraTimestamp": "1779327000123",
        "X-LyraSignature": "0x" + "33" * 65,
    }


@pytest.mark.asyncio
async def test_private_read_requires_signing_callback() -> None:
    adapter = DeriveAdapter(
        config={},
        derive_wallet_address="0x1111111111111111111111111111111111111111",
        http_client=FakeAsyncClient([]),
    )

    ok, error = await adapter.get_open_orders(subaccount_id=12345)

    assert ok is False
    assert "sign_message_callback or sign_hash_callback" in error


@pytest.mark.asyncio
async def test_create_account_with_secret_posts_public_payload() -> None:
    http = FakeAsyncClient(
        [{"id": "1", "result": {"status": "created", "wallet": "0xabc"}}]
    )
    adapter = DeriveAdapter(config={}, http_client=http)

    ok, result = await adapter.create_account_with_secret(
        secret="invite-secret",
        wallet="0x1111111111111111111111111111111111111111",
        scw_owner="0x2222222222222222222222222222222222222222",
    )

    assert ok is True
    assert result == {"status": "created", "wallet": "0xabc"}
    assert http.requests == [
        {
            "path": "/public/create_account_with_secret",
            "json": {
                "secret": "invite-secret",
                "wallet": "0x1111111111111111111111111111111111111111",
                "scw_owner": "0x2222222222222222222222222222222222222222",
            },
            "headers": None,
        }
    ]


@pytest.mark.asyncio
async def test_get_account_uses_target_wallet_for_auth(monkeypatch) -> None:
    monkeypatch.setattr("time.time", lambda: 1779327000.123)
    http = FakeAsyncClient(
        [
            {
                "id": "1",
                "result": {
                    "wallet": "0x2222222222222222222222222222222222222222",
                    "subaccount_ids": [12345],
                },
            }
        ]
    )
    adapter = DeriveAdapter(
        config={},
        sign_hash_callback=_fixed_signature,
        wallet_address="0x1111111111111111111111111111111111111111",
        derive_wallet_address="0x1111111111111111111111111111111111111111",
        http_client=http,
    )

    ok, result = await adapter.get_account(
        wallet="0x2222222222222222222222222222222222222222"
    )

    assert ok is True
    assert result["subaccount_ids"] == [12345]
    assert http.requests[0]["path"] == "/private/get_account"
    assert http.requests[0]["json"] == {
        "wallet": "0x2222222222222222222222222222222222222222"
    }
    assert http.requests[0]["headers"]["X-LyraWallet"] == (
        "0x2222222222222222222222222222222222222222"
    )


@pytest.mark.asyncio
async def test_ensure_subaccount_returns_existing_preferred(monkeypatch) -> None:
    monkeypatch.setattr("time.time", lambda: 1779327000.123)
    http = FakeAsyncClient(
        [
            {
                "id": "1",
                "result": {
                    "wallet": "0x1111111111111111111111111111111111111111",
                    "subaccount_ids": [111, 222],
                },
            }
        ]
    )
    adapter = DeriveAdapter(
        config={},
        sign_hash_callback=_fixed_signature,
        derive_wallet_address="0x1111111111111111111111111111111111111111",
        http_client=http,
    )

    ok, result = await adapter.ensure_subaccount(preferred_subaccount_id=222)

    assert ok is True
    assert result == {
        "status": "exists",
        "created": False,
        "subaccount_id": 222,
        "subaccount_ids": [111, 222],
        "wallet": "0x1111111111111111111111111111111111111111",
    }
    assert len(http.requests) == 1


@pytest.mark.asyncio
async def test_ensure_subaccount_creates_empty_subaccount(monkeypatch) -> None:
    captured_hashes: list[str] = []

    async def sign_hash(hash_hex: str) -> str:
        captured_hashes.append(hash_hex)
        return "0x" + "44" * 65

    monkeypatch.setattr("time.time", lambda: 1779327000.123)
    monkeypatch.setattr(
        "wayfinder_paths.adapters.derive_adapter.adapter.secrets.randbelow",
        lambda _upper: 7,
    )
    http = FakeAsyncClient(
        [
            {
                "id": "1",
                "result": {
                    "wallet": "0x1111111111111111111111111111111111111111",
                    "subaccount_ids": [],
                },
            },
            {
                "id": "2",
                "result": {
                    "status": "requested",
                    "transaction_id": "00000000-0000-0000-0000-000000000001",
                },
            },
        ]
    )
    adapter = DeriveAdapter(
        config={"derive": {"network": "testnet"}},
        sign_hash_callback=sign_hash,
        wallet_address="0x1111111111111111111111111111111111111111",
        derive_wallet_address="0x1111111111111111111111111111111111111111",
        http_client=http,
    )

    ok, result = await adapter.ensure_subaccount(
        create_if_missing=True,
        amount="0",
        nonce=1779327000123007,
        signature_expiry_sec=1779327600,
    )

    assert ok is True
    assert result["status"] == "create_requested"
    assert result["request"]["transaction_id"].endswith("1")
    assert http.requests[1]["path"] == "/private/create_subaccount"
    assert http.requests[1]["json"] == {
        "amount": "0",
        "asset_name": "USDC",
        "margin_type": "SM",
        "wallet": "0x1111111111111111111111111111111111111111",
        "nonce": 1779327000123007,
        "signature_expiry_sec": 1779327600,
        "signer": "0x1111111111111111111111111111111111111111",
        "signature": "0x" + "44" * 65,
    }
    assert len(captured_hashes) == 3
    assert all(item.startswith("0x") for item in captured_hashes)


@pytest.mark.asyncio
async def test_ensure_subaccount_can_create_account_then_subaccount(
    monkeypatch,
) -> None:
    monkeypatch.setattr("time.time", lambda: 1779327000.123)
    http = FakeAsyncClient(
        [
            {"id": "1", "error": {"code": -32000, "message": "account missing"}},
            {
                "id": "2",
                "result": {
                    "status": "created",
                    "wallet": "0x1111111111111111111111111111111111111111",
                },
            },
            {
                "id": "3",
                "result": {
                    "wallet": "0x1111111111111111111111111111111111111111",
                    "subaccount_ids": [],
                },
            },
            {
                "id": "4",
                "result": {
                    "status": "requested",
                    "transaction_id": "00000000-0000-0000-0000-000000000005",
                },
            },
        ]
    )
    adapter = DeriveAdapter(
        config={"derive": {"network": "testnet"}},
        sign_hash_callback=_fixed_signature,
        wallet_address="0x1111111111111111111111111111111111111111",
        derive_wallet_address="0x1111111111111111111111111111111111111111",
        http_client=http,
    )

    ok, result = await adapter.ensure_subaccount(
        create_account_if_missing=True,
        account_secret="invite-secret",
        scw_owner="0x2222222222222222222222222222222222222222",
        create_if_missing=True,
        amount="0",
        nonce=1779327000123007,
        signature_expiry_sec=1779327600,
    )

    assert ok is True
    assert result["status"] == "create_requested"
    assert result["account"] == {
        "status": "created",
        "wallet": "0x1111111111111111111111111111111111111111",
    }
    assert [request["path"] for request in http.requests] == [
        "/private/get_subaccounts",
        "/public/create_account_with_secret",
        "/private/get_subaccounts",
        "/private/create_subaccount",
    ]
    assert http.requests[1]["json"] == {
        "secret": "invite-secret",
        "wallet": "0x1111111111111111111111111111111111111111",
        "scw_owner": "0x2222222222222222222222222222222222222222",
    }


@pytest.mark.asyncio
async def test_ensure_subaccount_requires_secret_for_account_creation(
    monkeypatch,
) -> None:
    monkeypatch.setattr("time.time", lambda: 1779327000.123)
    http = FakeAsyncClient(
        [{"id": "1", "error": {"code": -32000, "message": "account missing"}}]
    )
    adapter = DeriveAdapter(
        config={},
        sign_hash_callback=_fixed_signature,
        derive_wallet_address="0x1111111111111111111111111111111111111111",
        http_client=http,
    )

    ok, error = await adapter.ensure_subaccount(create_account_if_missing=True)

    assert ok is False
    assert "account_secret is required" in error
    assert len(http.requests) == 1


@pytest.mark.asyncio
async def test_deposit_collateral_signs_action_payload(monkeypatch) -> None:
    captured_hashes: list[str] = []

    async def sign_hash(hash_hex: str) -> str:
        captured_hashes.append(hash_hex)
        return "0x" + "55" * 65

    monkeypatch.setattr("time.time", lambda: 1779327000.123)
    http = FakeAsyncClient(
        [
            {
                "id": "1",
                "result": {
                    "status": "requested",
                    "transaction_id": "00000000-0000-0000-0000-000000000002",
                },
            }
        ]
    )
    adapter = DeriveAdapter(
        config={"derive": {"network": "testnet"}},
        sign_hash_callback=sign_hash,
        wallet_address="0x1111111111111111111111111111111111111111",
        derive_wallet_address="0x2222222222222222222222222222222222222222",
        http_client=http,
    )

    ok, result = await adapter.deposit_collateral(
        subaccount_id=12345,
        amount="12.5",
        nonce=1779327000123007,
        signature_expiry_sec=1779327600,
    )

    assert ok is True
    assert result["status"] == "requested"
    assert http.requests[0]["path"] == "/private/deposit"
    assert http.requests[0]["json"] == {
        "amount": "12.5",
        "asset_name": "USDC",
        "subaccount_id": 12345,
        "nonce": 1779327000123007,
        "signature_expiry_sec": 1779327600,
        "signer": "0x1111111111111111111111111111111111111111",
        "signature": "0x" + "55" * 65,
    }
    assert http.requests[0]["headers"]["X-LyraWallet"] == (
        "0x2222222222222222222222222222222222222222"
    )
    assert len(captured_hashes) == 2


@pytest.mark.asyncio
async def test_withdraw_collateral_signs_action_payload(monkeypatch) -> None:
    monkeypatch.setattr("time.time", lambda: 1779327000.123)
    http = FakeAsyncClient(
        [
            {
                "id": "1",
                "result": {
                    "status": "requested",
                    "transaction_id": "00000000-0000-0000-0000-000000000003",
                },
            }
        ]
    )
    adapter = DeriveAdapter(
        config={"derive": {"network": "testnet"}},
        sign_hash_callback=_fixed_signature,
        wallet_address="0x1111111111111111111111111111111111111111",
        derive_wallet_address="0x1111111111111111111111111111111111111111",
        http_client=http,
    )

    ok, result = await adapter.withdraw_collateral(
        subaccount_id=12345,
        amount="1.25",
        nonce=1779327000123008,
        signature_expiry_sec=1779327600,
        is_atomic_signing=True,
    )

    assert ok is True
    assert result["transaction_id"].endswith("3")
    assert http.requests[0]["path"] == "/private/withdraw"
    assert http.requests[0]["json"] == {
        "amount": "1.25",
        "asset_name": "USDC",
        "subaccount_id": 12345,
        "nonce": 1779327000123008,
        "signature_expiry_sec": 1779327600,
        "signer": "0x1111111111111111111111111111111111111111",
        "signature": "0x" + "33" * 65,
        "is_atomic_signing": True,
    }


@pytest.mark.asyncio
async def test_lifecycle_action_requires_hash_callback() -> None:
    adapter = DeriveAdapter(
        config={},
        derive_wallet_address="0x1111111111111111111111111111111111111111",
        http_client=FakeAsyncClient([]),
    )

    ok, error = await adapter.deposit_collateral(subaccount_id=12345, amount="1")

    assert ok is False
    assert "sign_hash_callback is required" in error


@pytest.mark.asyncio
async def test_deposit_and_withdrawal_history_payloads(monkeypatch) -> None:
    monkeypatch.setattr("time.time", lambda: 1779327000.123)
    http = FakeAsyncClient(
        [
            {"id": "1", "result": {"events": [{"tx_status": "settled"}]}},
            {"id": "2", "result": {"events": [{"tx_status": "pending"}]}},
        ]
    )
    adapter = DeriveAdapter(
        config={},
        sign_hash_callback=_fixed_signature,
        derive_wallet_address="0x1111111111111111111111111111111111111111",
        http_client=http,
    )

    ok, deposits = await adapter.get_deposit_history(
        subaccount_id=12345,
        start_timestamp=100,
        end_timestamp=200,
    )
    assert ok is True
    assert deposits["events"][0]["tx_status"] == "settled"

    ok, withdrawals = await adapter.get_withdrawal_history(subaccount_id=12345)
    assert ok is True
    assert withdrawals["events"][0]["tx_status"] == "pending"
    assert http.requests[0]["path"] == "/private/get_deposit_history"
    assert http.requests[0]["json"] == {
        "subaccount_id": 12345,
        "start_timestamp": 100,
        "end_timestamp": 200,
    }
    assert http.requests[1]["path"] == "/private/get_withdrawal_history"
    assert http.requests[1]["json"] == {"subaccount_id": 12345}


@pytest.mark.asyncio
async def test_transfer_erc20_signs_sender_and_recipient(monkeypatch) -> None:
    captured_hashes: list[str] = []

    async def sign_hash(hash_hex: str) -> str:
        captured_hashes.append(hash_hex)
        return "0x" + str(len(captured_hashes)) * 130

    monkeypatch.setattr("time.time", lambda: 1779327000.123)
    http = FakeAsyncClient(
        [
            {
                "id": "1",
                "result": {
                    "status": "requested",
                    "transaction_id": "00000000-0000-0000-0000-000000000004",
                },
            }
        ]
    )
    adapter = DeriveAdapter(
        config={"derive": {"network": "testnet"}},
        sign_hash_callback=sign_hash,
        wallet_address="0x1111111111111111111111111111111111111111",
        derive_wallet_address="0x1111111111111111111111111111111111111111",
        http_client=http,
    )

    ok, result = await adapter.transfer_erc20(
        subaccount_id=111,
        recipient_subaccount_id=222,
        amount="3.5",
        nonce=1779327000123001,
        recipient_nonce=1779327000123002,
        signature_expiry_sec=1779327600,
    )

    assert ok is True
    assert result["status"] == "requested"
    assert http.requests[0]["path"] == "/private/transfer_erc20"
    assert http.requests[0]["json"]["sender_details"] == {
        "nonce": 1779327000123001,
        "signature_expiry_sec": 1779327600,
        "signer": "0x1111111111111111111111111111111111111111",
        "signature": "0x" + "1" * 130,
    }
    assert http.requests[0]["json"]["recipient_details"] == {
        "nonce": 1779327000123002,
        "signature_expiry_sec": 1779327600,
        "signer": "0x1111111111111111111111111111111111111111",
        "signature": "0x" + "2" * 130,
    }
    assert http.requests[0]["json"]["transfer"] == {
        "address": "0x6caf294DaC985ff653d5aE75b4FF8E0A66025928",
        "amount": "3.5",
        "sub_id": 0,
    }
    assert len(captured_hashes) == 3


@pytest.mark.asyncio
async def test_rpc_error_returns_false() -> None:
    http = FakeAsyncClient(
        [
            {
                "id": "1",
                "error": {"code": -32000, "message": "Rate limit exceeded"},
            }
        ]
    )
    adapter = DeriveAdapter(config={}, http_client=http)

    ok, error = await adapter.get_time()

    assert ok is False
    assert error == {"code": -32000, "message": "Rate limit exceeded"}


@pytest.mark.asyncio
async def test_submit_order_rejects_missing_signed_fields() -> None:
    adapter = DeriveAdapter(config={}, http_client=FakeAsyncClient([]))

    ok, error = await adapter.submit_order({"instrument_name": "ETH-20260522-2500-C"})

    assert ok is False
    assert "missing signed Derive order fields" in error


@pytest.mark.asyncio
async def test_sign_order_uses_wallet_hash_callback(monkeypatch) -> None:
    captured_hashes: list[str] = []

    async def sign_hash(hash_hex: str) -> str:
        captured_hashes.append(hash_hex)
        return "0x" + "66" * 65

    monkeypatch.setattr("time.time", lambda: 1779327000.123)
    adapter = DeriveAdapter(
        config={"derive": {"network": "testnet"}},
        sign_hash_callback=sign_hash,
        wallet_address="0x1111111111111111111111111111111111111111",
        derive_wallet_address="0x2222222222222222222222222222222222222222",
        http_client=FakeAsyncClient([]),
    )

    ok, order = await adapter.sign_order(
        _unsigned_order(),
        asset_address="0xBcB494059969DAaB460E0B5d4f5c2366aab79aa1",
        asset_sub_id=987654,
        nonce=1779327000123009,
        signature_expiry_sec=1779327600,
    )

    assert ok is True
    assert order["direction"] == "buy"
    assert order["signature"] == "0x" + "66" * 65
    assert order["signer"] == "0x1111111111111111111111111111111111111111"
    assert order["nonce"] == 1779327000123009
    assert len(captured_hashes) == 1


@pytest.mark.asyncio
async def test_place_order_dry_run_signs_and_posts_order_debug(monkeypatch) -> None:
    captured_hashes: list[str] = []

    async def sign_hash(hash_hex: str) -> str:
        captured_hashes.append(hash_hex)
        return "0x" + "77" * 65

    monkeypatch.setattr("time.time", lambda: 1779327000.123)
    http = FakeAsyncClient([{"id": "1", "result": {"is_valid": True}}])
    adapter = DeriveAdapter(
        config={"derive": {"network": "testnet"}},
        sign_hash_callback=sign_hash,
        wallet_address="0x1111111111111111111111111111111111111111",
        derive_wallet_address="0x2222222222222222222222222222222222222222",
        http_client=http,
    )

    ok, result = await adapter.place_order(
        _unsigned_order(),
        asset_address="0xBcB494059969DAaB460E0B5d4f5c2366aab79aa1",
        asset_sub_id=987654,
        nonce=1779327000123010,
        signature_expiry_sec=1779327600,
        dry_run=True,
    )

    assert ok is True
    assert result == {"is_valid": True}
    assert http.requests[0]["path"] == "/private/order_debug"
    assert http.requests[0]["json"]["signature"] == "0x" + "77" * 65
    assert http.requests[0]["json"]["direction"] == "buy"
    assert http.requests[0]["headers"]["X-LyraWallet"] == (
        "0x2222222222222222222222222222222222222222"
    )
    assert len(captured_hashes) == 2


@pytest.mark.asyncio
async def test_submit_order_dry_run_uses_order_debug(monkeypatch) -> None:
    async def sign_hash(_hash_hex: str) -> str:
        return "0x" + "33" * 65

    monkeypatch.setattr("time.time", lambda: 1779327000.123)
    http = FakeAsyncClient([{"id": "1", "result": {"is_valid": True}}])
    adapter = DeriveAdapter(
        config={},
        sign_hash_callback=sign_hash,
        derive_wallet_address="0x1111111111111111111111111111111111111111",
        http_client=http,
    )

    ok, result = await adapter.submit_order(_signed_order(), dry_run=True)

    assert ok is True
    assert result == {"is_valid": True}
    assert http.requests[0]["path"] == "/private/order_debug"
    assert http.requests[0]["json"] == _signed_order()


@pytest.mark.asyncio
async def test_submit_order_live_uses_order_endpoint(monkeypatch) -> None:
    async def sign_hash(_hash_hex: str) -> str:
        return "0x" + "33" * 65

    monkeypatch.setattr("time.time", lambda: 1779327000.123)
    http = FakeAsyncClient([{"id": "1", "result": {"order_id": "order-123"}}])
    adapter = DeriveAdapter(
        config={},
        sign_hash_callback=sign_hash,
        derive_wallet_address="0x1111111111111111111111111111111111111111",
        http_client=http,
    )

    ok, result = await adapter.submit_order(_signed_order())

    assert ok is True
    assert result == {"order_id": "order-123"}
    assert http.requests[0]["path"] == "/private/order"


@pytest.mark.asyncio
async def test_cancel_order_posts_cancel_payload(monkeypatch) -> None:
    async def sign_hash(_hash_hex: str) -> str:
        return "0x" + "33" * 65

    monkeypatch.setattr("time.time", lambda: 1779327000.123)
    http = FakeAsyncClient([{"id": "1", "result": {"order_status": "cancelled"}}])
    adapter = DeriveAdapter(
        config={},
        sign_hash_callback=sign_hash,
        derive_wallet_address="0x1111111111111111111111111111111111111111",
        http_client=http,
    )

    ok, result = await adapter.cancel_order(
        instrument_name="ETH-20260522-2500-C",
        order_id="order-123",
        subaccount_id=12345,
    )

    assert ok is True
    assert result == {"order_status": "cancelled"}
    assert http.requests[0]["path"] == "/private/cancel"
    assert http.requests[0]["json"] == {
        "instrument_name": "ETH-20260522-2500-C",
        "order_id": "order-123",
        "subaccount_id": 12345,
    }


def test_orderbook_channel_builder() -> None:
    assert (
        DeriveAdapter.orderbook_channel("ETH-20260522-2500-C", group="10", depth="20")
        == "orderbook.ETH-20260522-2500-C.10.20"
    )

    with pytest.raises(ValueError, match="group"):
        DeriveAdapter.orderbook_channel("ETH-20260522-2500-C", group="5")


def test_new_order_nonce_uses_three_digit_random_suffix(monkeypatch) -> None:
    monkeypatch.setattr(
        "wayfinder_paths.adapters.derive_adapter.adapter.secrets.randbelow",
        lambda _upper: 7,
    )

    assert DeriveAdapter.new_order_nonce(now_ms=1779327000123) == 1779327000123007


@pytest.mark.asyncio
async def test_close_closes_http_client() -> None:
    http = FakeAsyncClient([])
    adapter = DeriveAdapter(config={}, http_client=http)

    await adapter.close()

    assert http.closed is True
