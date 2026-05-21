import pytest
from eth_utils import to_checksum_address

from wayfinder_paths.adapters.euler_v2_adapter.adapter import EulerV2Adapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE

BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_VAULT = "0x859160DB5841E5cfB8D3f144C6b3381A85A4b410"
BASE_EARN_VAULT = "0x8bF41Ad2b816F7c220b22F4BCD63fC2A35Ab4247"
BASE_DTOKEN = "0x5CB62136b1eea9945bc1F72c614f0b73c79f1F16"


class TestEulerV2Adapter:
    def test_adapter_type(self):
        assert EulerV2Adapter.adapter_type == "EULER_V2"

    def test_strategy_address_optional(self):
        adapter = EulerV2Adapter(config={})
        assert adapter.strategy_wallet_address is None

    @pytest.mark.asyncio
    async def test_unsupported_chain_returns_error(self):
        adapter = EulerV2Adapter(config={})
        ok, err = await adapter.get_verified_vaults(chain_id=0)
        assert ok is False
        assert isinstance(err, str) and err

    @pytest.mark.asyncio
    async def test_get_protocol_contracts_includes_current_surfaces(self):
        adapter = EulerV2Adapter(config={})

        ok, contracts = await adapter.get_protocol_contracts(chain_id=CHAIN_ID_BASE)

        assert ok is True, contracts
        assert contracts["evc"] == to_checksum_address(
            "0x5301c7dD20bD945D2013b48ed0DEE3A284ca8989"
        )
        assert contracts["lenses"]["vault_lens"] == to_checksum_address(
            "0x601F023CD063324DdbCADa69460e969fb97e98b9"
        )
        assert contracts["lenses"]["euler_earn_vault_lens"] == to_checksum_address(
            "0x0BBf9eE761bFF1c4d64dB608781D5e3beFeed875"
        )
        assert contracts["perspectives"]["euler_earn_governed"] == to_checksum_address(
            "0x08B817C17d84DF89AA371084D910081a5Cc04724"
        )
        assert contracts["swaps"]["euler_swap_v2_periphery"] == to_checksum_address(
            "0xA564dAe65eA7B1ce049AbACFC4Cb1A32C93e127c"
        )
        assert contracts["periphery"]["swap_verifier"] == to_checksum_address(
            "0xF8B2d2BA412E24235eAaDa8d3050202898455455"
        )

        ok, morph = await adapter.get_protocol_contracts(chain_id=2818)
        assert ok is True, morph
        assert morph["network"] == "morph"

    @pytest.mark.asyncio
    async def test_get_indexed_vaults_calls_euler_v3_api(self, monkeypatch):
        adapter = EulerV2Adapter(config={})
        captured = {}

        async def fake_get(endpoint, *, params=None):
            captured["endpoint"] = endpoint
            captured["params"] = params
            return {
                "data": [
                    {
                        "chainId": CHAIN_ID_BASE,
                        "address": BASE_VAULT.lower(),
                        "vaultType": "evk",
                        "asset": {
                            "address": BASE_USDC.lower(),
                            "symbol": "USDC",
                            "decimals": "6",
                        },
                        "supplyApy": 5.25,
                        "borrowApy": 9.5,
                        "totalAssets": "1000000",
                        "totalBorrows": "123",
                    }
                ],
                "meta": {"total": 1, "limit": 1},
            }

        monkeypatch.setattr(adapter, "_euler_v3_get", fake_get)

        ok, result = await adapter.get_indexed_vaults(
            chain_id=CHAIN_ID_BASE,
            limit=500,
            offset=-5,
            fields=["address", "supplyApy"],
            asset=BASE_USDC,
            sort="-totalSupplyUsd",
        )

        assert ok is True, result
        assert captured["endpoint"] == "/evk/vaults"
        assert captured["params"] == {
            "chainId": CHAIN_ID_BASE,
            "limit": 100,
            "offset": 0,
            "fields": "address,supplyApy",
            "sort": "-totalSupplyUsd",
            "asset": to_checksum_address(BASE_USDC),
            "minTvl": None,
            "maxTvl": None,
            "visibility": None,
        }
        assert result["source"] == "euler_v3_api"
        assert result["endpoint"] == "/evk/vaults"
        assert result["raw"]["data"][0]["address"] == BASE_VAULT.lower()
        assert result["data"][0]["address"] == to_checksum_address(BASE_VAULT)
        assert result["data"][0]["asset"]["address"] == to_checksum_address(BASE_USDC)
        assert result["data"][0]["asset"]["decimals"] == 6
        assert result["data"][0]["underlying"] == to_checksum_address(BASE_USDC)
        assert result["data"][0]["supply_apy_decimal"] == 0.0525
        assert result["data"][0]["borrow_apy_decimal"] == 0.095
        assert result["data"][0]["total_assets_raw"] == 1_000_000
        assert result["data"][0]["total_borrows_raw"] == 123
        assert result["data"][0]["vault_type"] == "evk"
        assert result["data"][0]["chain_id"] == CHAIN_ID_BASE
        assert result["meta"]["total"] == 1

    @pytest.mark.asyncio
    async def test_get_indexed_vault_detail_collaterals_and_totals_use_v3_api(
        self, monkeypatch
    ):
        adapter = EulerV2Adapter(config={})
        calls = []

        async def fake_get(endpoint, *, params=None):
            calls.append((endpoint, params))
            if endpoint.endswith("/collaterals"):
                return {
                    "data": [
                        {
                            "collateral": BASE_EARN_VAULT.lower(),
                            "asset": BASE_USDC.lower(),
                            "borrowLTV": "7500",
                            "liquidationLTV": "8000",
                            "initialLiquidationLTV": "0",
                        }
                    ],
                    "meta": {"total": 1, "limit": 100},
                }
            return {
                "data": {
                    "chainId": CHAIN_ID_BASE,
                    "address": BASE_VAULT.lower(),
                    "vaultType": "evk",
                    "dToken": BASE_DTOKEN.lower(),
                    "asset": {"address": BASE_USDC.lower(), "decimals": 6},
                    "totalAssets": "1000000",
                    "current": {
                        "totalAssets": "1000000",
                        "borrowApy": 12.5,
                    },
                    "history": [{"totalBorrows": "42", "supplyApy": 5}],
                },
                "meta": {},
            }

        monkeypatch.setattr(adapter, "_euler_v3_get", fake_get)

        ok, detail = await adapter.get_indexed_vault(
            chain_id=CHAIN_ID_BASE, vault=BASE_VAULT
        )
        assert ok is True, detail
        assert detail["endpoint"] == (
            f"/evk/vaults/{CHAIN_ID_BASE}/{to_checksum_address(BASE_VAULT)}"
        )
        assert detail["data"]["address"] == to_checksum_address(BASE_VAULT)
        assert detail["data"]["dToken"] == to_checksum_address(BASE_DTOKEN)
        assert detail["data"]["total_assets_raw"] == 1_000_000

        ok, collaterals = await adapter.get_indexed_vault_collaterals(
            chain_id=CHAIN_ID_BASE, vault=BASE_VAULT, limit=500, offset=-1
        )
        assert ok is True, collaterals
        assert collaterals["data"][0]["collateral"] == to_checksum_address(
            BASE_EARN_VAULT
        )
        assert collaterals["data"][0]["asset"] == to_checksum_address(BASE_USDC)
        assert collaterals["data"][0]["borrowLTV"] == 7500

        ok, totals = await adapter.get_indexed_vault_totals(
            chain_id=CHAIN_ID_BASE, vault=BASE_VAULT
        )
        assert ok is True, totals
        assert totals["data"]["current"]["total_assets_raw"] == 1_000_000
        assert totals["data"]["current"]["borrow_apy_decimal"] == 0.125
        assert totals["data"]["history"][0]["total_borrows_raw"] == 42
        assert totals["data"]["history"][0]["supply_apy_decimal"] == 0.05

        assert calls == [
            (f"/evk/vaults/{CHAIN_ID_BASE}/{to_checksum_address(BASE_VAULT)}", None),
            (
                f"/evk/vaults/{CHAIN_ID_BASE}/{to_checksum_address(BASE_VAULT)}/collaterals",
                {"limit": 100, "offset": 0},
            ),
            (
                f"/evk/vaults/{CHAIN_ID_BASE}/{to_checksum_address(BASE_VAULT)}/totals",
                None,
            ),
        ]

    @pytest.mark.asyncio
    async def test_get_euler_earn_vaults_and_resolve_vault_use_v3_api(
        self, monkeypatch
    ):
        adapter = EulerV2Adapter(config={})
        calls = []

        async def fake_get(endpoint, *, params=None):
            calls.append((endpoint, params))
            if endpoint == "/earn/vaults":
                return {
                    "data": [
                        {
                            "chainId": CHAIN_ID_BASE,
                            "address": BASE_EARN_VAULT.lower(),
                            "asset": {"address": BASE_USDC.lower(), "decimals": "6"},
                            "totalAssets": "1000000",
                            "apy30d": 7.75,
                        }
                    ],
                    "meta": {"total": 1},
                }
            if endpoint.startswith("/earn/vaults/"):
                return {
                    "data": {
                        "chainId": CHAIN_ID_BASE,
                        "address": BASE_EARN_VAULT.lower(),
                        "asset": {"address": BASE_USDC.lower(), "decimals": 6},
                        "apyCurrent": 3.5,
                    },
                    "meta": {},
                }
            return {
                "data": {
                    "chainId": CHAIN_ID_BASE,
                    "address": BASE_VAULT.lower(),
                    "found": True,
                    "vaultType": "evk",
                },
                "meta": {},
            }

        monkeypatch.setattr(adapter, "_euler_v3_get", fake_get)

        ok, earn = await adapter.get_euler_earn_vaults(chain_id=CHAIN_ID_BASE, limit=5)
        assert ok is True, earn
        assert earn["endpoint"] == "/earn/vaults"
        assert earn["data"][0]["address"] == to_checksum_address(BASE_EARN_VAULT)
        assert earn["data"][0]["underlying"] == to_checksum_address(BASE_USDC)
        assert earn["data"][0]["vault_type"] == "earn"
        assert earn["data"][0]["total_assets_raw"] == 1_000_000
        assert earn["data"][0]["apy_30d_decimal"] == 0.0775

        ok, earn_detail = await adapter.get_euler_earn_vault(
            chain_id=CHAIN_ID_BASE, vault=BASE_EARN_VAULT
        )
        assert ok is True, earn_detail
        assert earn_detail["endpoint"] == (
            f"/earn/vaults/{CHAIN_ID_BASE}/{to_checksum_address(BASE_EARN_VAULT)}"
        )
        assert earn_detail["data"]["apy_current_decimal"] == 0.035

        ok, resolved = await adapter.resolve_vault(
            chain_id=CHAIN_ID_BASE, address=BASE_VAULT
        )
        assert ok is True, resolved
        assert resolved["data"]["found"] is True
        assert resolved["data"]["vaultType"] == "evk"
        assert resolved["data"]["address"] == to_checksum_address(BASE_VAULT)

        assert calls == [
            (
                "/earn/vaults",
                {"chainId": CHAIN_ID_BASE, "limit": 5, "offset": 0},
            ),
            (
                f"/earn/vaults/{CHAIN_ID_BASE}/{to_checksum_address(BASE_EARN_VAULT)}",
                None,
            ),
            (
                "/resolve/vaults",
                {"chainId": CHAIN_ID_BASE, "address": to_checksum_address(BASE_VAULT)},
            ),
        ]

    @pytest.mark.asyncio
    async def test_get_offchain_prices_checksums_and_dedupes_addresses(
        self, monkeypatch
    ):
        adapter = EulerV2Adapter(config={})
        captured = {}

        async def fake_get(endpoint, *, params=None):
            captured["endpoint"] = endpoint
            captured["params"] = params
            return {
                "data": [
                    {
                        "chainId": CHAIN_ID_BASE,
                        "address": to_checksum_address(BASE_USDC),
                        "priceUsd": 0.999,
                    }
                ],
                "meta": {"hasMore": False},
            }

        monkeypatch.setattr(adapter, "_euler_v3_get", fake_get)

        ok, prices = await adapter.get_offchain_prices(
            chain_id=CHAIN_ID_BASE,
            addresses=[BASE_USDC.lower(), BASE_USDC],
        )

        assert ok is True, prices
        assert captured["endpoint"] == "/prices"
        assert captured["params"] == {
            "chainId": CHAIN_ID_BASE,
            "addresses": to_checksum_address(BASE_USDC),
        }
        assert prices["data"][0]["address"] == to_checksum_address(BASE_USDC)
        assert prices["data"][0]["chain_id"] == CHAIN_ID_BASE
        assert prices["data"][0]["price_usd"] == 0.999
        assert prices["data"][0]["priceUsd"] == 0.999

    @pytest.mark.asyncio
    async def test_get_offchain_prices_rejects_invalid_or_empty_addresses(
        self, monkeypatch
    ):
        adapter = EulerV2Adapter(config={})

        async def should_not_call(endpoint, *, params=None):
            raise AssertionError("Euler V3 API should not be called")

        monkeypatch.setattr(adapter, "_euler_v3_get", should_not_call)

        ok, err = await adapter.get_offchain_prices(
            chain_id=CHAIN_ID_BASE, addresses=[]
        )
        assert ok is False
        assert err == "at least one token address is required"

        ok, err = await adapter.get_offchain_prices(
            chain_id=CHAIN_ID_BASE, addresses=["not-an-address"]
        )
        assert ok is False
        assert "Unknown format" in err or "when sending a str" in err

    @pytest.mark.asyncio
    async def test_v3_api_errors_are_returned_to_caller(self, monkeypatch):
        adapter = EulerV2Adapter(config={})

        async def fake_get(endpoint, *, params=None):
            raise ValueError(
                "Euler endpoint error VALIDATION_ERROR: Invalid chain ID "
                "request_id=req_123"
            )

        monkeypatch.setattr(adapter, "_euler_v3_get", fake_get)

        ok, err = await adapter.get_indexed_vaults(chain_id=CHAIN_ID_BASE)

        assert ok is False
        assert "VALIDATION_ERROR" in err
        assert "request_id=req_123" in err

    @pytest.mark.asyncio
    async def test_get_labelled_vaults_reads_products_and_earn_lists(self, monkeypatch):
        adapter = EulerV2Adapter(config={})
        calls = []

        async def fake_labels(*, chain_id, file_name):
            calls.append((chain_id, file_name))
            if file_name == "products.json":
                return {
                    "curator-product": {
                        "name": "Curator Product",
                        "vaults": [BASE_VAULT.lower(), BASE_VAULT],
                    }
                }
            return [BASE_EARN_VAULT.lower(), BASE_EARN_VAULT]

        monkeypatch.setattr(adapter, "_euler_labels_get", fake_labels)

        ok, labels = await adapter.get_labelled_vaults(chain_id=CHAIN_ID_BASE)

        assert ok is True, labels
        assert labels["source"] == "euler_labels"
        assert labels["evk_vaults"] == [to_checksum_address(BASE_VAULT)]
        assert labels["earn_vaults"] == [to_checksum_address(BASE_EARN_VAULT)]
        assert labels["product_count"] == 1
        assert calls == [
            (CHAIN_ID_BASE, "products.json"),
            (CHAIN_ID_BASE, "earn-vaults.json"),
        ]
