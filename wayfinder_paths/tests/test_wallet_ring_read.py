"""Wallet listing rides the backend's ring read (the collection GET 405s
since the 2026-07-16 backend refactor) and flattens rings to the legacy
per-wallet rows every caller expects."""

from __future__ import annotations

from typing import Any

import pytest

from wayfinder_paths.core.clients.WalletClient import WalletClient


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


@pytest.mark.asyncio
async def test_list_wallets_flattens_rings(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    async def fake_request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        captured["method"] = method
        captured["url"] = url
        return _FakeResponse(
            [
                {
                    "label": "funding-carry-basket",
                    "evm": {
                        "wallet_address": "0xabc",
                        "wallet_type": "session",
                        "session_expires_at": 1786700000,
                    },
                    "svm": {
                        "wallet_address": "So1abc",
                        "wallet_type": "session",
                    },
                },
                {
                    "label": "empty-ring",
                    "evm": None,
                    "svm": {},
                },
            ]
        )

    monkeypatch.setattr(WalletClient, "_authed_request", fake_request)
    wallets = await WalletClient().list_wallets(instance_id="oc-test")

    assert captured["method"] == "GET"
    assert "/wallets/rings?instance_id=oc-test" in captured["url"]
    # One flat row per leg with an address; ring label is the source of truth.
    assert [(w["label"], w["wallet_address"], w["chain_type"]) for w in wallets] == [
        ("funding-carry-basket", "0xabc", "ethereum"),
        ("funding-carry-basket", "So1abc", "solana"),
    ]
    assert wallets[0]["session_expires_at"] == 1786700000
