from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayfinder_paths.adapters.ethena_vault_adapter.adapter import (
    VESTING_PERIOD_S,
    EthenaVaultAdapter,
)
from wayfinder_paths.core.constants.base import SECONDS_PER_YEAR
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM
from wayfinder_paths.core.constants.ethena_contracts import (
    ETHENA_SUSDE_VAULT_MAINNET,
    ETHENA_USDE_MAINNET,
    ethena_tokens_by_chain_id,
)

MOCK_WALLET = "0x1234567890123456789012345678901234567890"
ADAPTER_MODULE = "wayfinder_paths.adapters.ethena_vault_adapter.adapter"


class TestEthenaVaultAdapter:
    @pytest.fixture
    def adapter(self):
        return EthenaVaultAdapter(
            config={},
            sign_callback=AsyncMock(return_value=b"\x00" * 32),
            wallet_address=MOCK_WALLET,
        )

    @pytest.fixture
    def readonly_adapter(self):
        return EthenaVaultAdapter(config={})

    def test_adapter_type(self, adapter):
        assert adapter.adapter_type == "ETHENA"

    def test_wallet_address_checksummed(self, adapter):
        assert adapter.wallet_address is not None
        assert adapter.wallet_address.startswith("0x")

    def test_no_wallet(self, readonly_adapter):
        assert readonly_adapter.wallet_address is None
        assert readonly_adapter.sign_callback is None

    # -- get_apy ---------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_apy(self, readonly_adapter):
        now_ts = 1_700_000_000
        last_dist_ts = now_ts - 3600  # 1 hour ago
        unvested = 10**18
        total_assets = 100 * 10**18

        mock_vault = MagicMock()
        mock_vault.functions.getUnvestedAmount.return_value = MagicMock(
            call=AsyncMock(return_value=unvested)
        )
        mock_vault.functions.lastDistributionTimestamp.return_value = MagicMock(
            call=AsyncMock(return_value=last_dist_ts)
        )
        mock_vault.functions.totalAssets.return_value = MagicMock(
            call=AsyncMock(return_value=total_assets)
        )

        mock_web3 = MagicMock()
        mock_web3.eth.contract.return_value = mock_vault
        mock_web3.eth.get_block = AsyncMock(return_value={"timestamp": now_ts})

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(f"{ADAPTER_MODULE}.web3_from_chain_id", mock_web3_ctx):
            ok, apy = await readonly_adapter.get_apy()

        assert ok is True
        assert isinstance(apy, float)
        assert apy > 0

    @pytest.mark.asyncio
    async def test_get_apy_zero_unvested(self, readonly_adapter):
        mock_vault = MagicMock()
        mock_vault.functions.getUnvestedAmount.return_value = MagicMock(
            call=AsyncMock(return_value=0)
        )
        mock_vault.functions.lastDistributionTimestamp.return_value = MagicMock(
            call=AsyncMock(return_value=0)
        )
        mock_vault.functions.totalAssets.return_value = MagicMock(
            call=AsyncMock(return_value=100 * 10**18)
        )

        mock_web3 = MagicMock()
        mock_web3.eth.contract.return_value = mock_vault
        mock_web3.eth.get_block = AsyncMock(return_value={"timestamp": 1_700_000_000})

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(f"{ADAPTER_MODULE}.web3_from_chain_id", mock_web3_ctx):
            ok, apy = await readonly_adapter.get_apy()

        assert ok is True
        assert apy == 0.0

    @pytest.mark.asyncio
    async def test_get_apy_vesting_elapsed(self, readonly_adapter):
        """APY is 0 when the full vesting period has elapsed."""
        now_ts = 1_700_000_000
        last_dist_ts = now_ts - VESTING_PERIOD_S - 1  # vesting fully elapsed

        mock_vault = MagicMock()
        mock_vault.functions.getUnvestedAmount.return_value = MagicMock(
            call=AsyncMock(return_value=10**18)
        )
        mock_vault.functions.lastDistributionTimestamp.return_value = MagicMock(
            call=AsyncMock(return_value=last_dist_ts)
        )
        mock_vault.functions.totalAssets.return_value = MagicMock(
            call=AsyncMock(return_value=100 * 10**18)
        )

        mock_web3 = MagicMock()
        mock_web3.eth.contract.return_value = mock_vault
        mock_web3.eth.get_block = AsyncMock(return_value={"timestamp": now_ts})

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(f"{ADAPTER_MODULE}.web3_from_chain_id", mock_web3_ctx):
            ok, apy = await readonly_adapter.get_apy()

        assert ok is True
        assert apy == 0.0

    @pytest.mark.asyncio
    async def test_get_apy_math(self, readonly_adapter):
        """Verify the APY calculation against a known input."""
        now_ts = 1_700_000_000
        elapsed = 3600  # 1 hour into 8-hour vesting
        remaining = VESTING_PERIOD_S - elapsed
        unvested = 10**18
        total_assets = 1000 * 10**18

        mock_vault = MagicMock()
        mock_vault.functions.getUnvestedAmount.return_value = MagicMock(
            call=AsyncMock(return_value=unvested)
        )
        mock_vault.functions.lastDistributionTimestamp.return_value = MagicMock(
            call=AsyncMock(return_value=now_ts - elapsed)
        )
        mock_vault.functions.totalAssets.return_value = MagicMock(
            call=AsyncMock(return_value=total_assets)
        )

        mock_web3 = MagicMock()
        mock_web3.eth.contract.return_value = mock_vault
        mock_web3.eth.get_block = AsyncMock(return_value={"timestamp": now_ts})

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(f"{ADAPTER_MODULE}.web3_from_chain_id", mock_web3_ctx):
            ok, apy = await readonly_adapter.get_apy()

        assert ok is True
        expected_apr = (unvested / remaining) / total_assets * SECONDS_PER_YEAR
        # APY > APR for positive rates
        assert apy > expected_apr

    # -- get_cooldown ----------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_cooldown(self, readonly_adapter):
        cooldown_end = 1_700_100_000
        underlying = 50 * 10**18

        mock_vault = MagicMock()
        mock_vault.functions.cooldowns.return_value = MagicMock(
            call=AsyncMock(return_value=(cooldown_end, underlying))
        )

        mock_web3 = MagicMock()
        mock_web3.eth.contract.return_value = mock_vault

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(f"{ADAPTER_MODULE}.web3_from_chain_id", mock_web3_ctx):
            ok, cd = await readonly_adapter.get_cooldown(account=MOCK_WALLET)

        assert ok is True
        assert isinstance(cd, dict)
        assert cd["cooldownEnd"] == cooldown_end
        assert cd["underlyingAmount"] == underlying

    @pytest.mark.asyncio
    async def test_get_cooldown_none(self, readonly_adapter):
        mock_vault = MagicMock()
        mock_vault.functions.cooldowns.return_value = MagicMock(
            call=AsyncMock(return_value=(0, 0))
        )

        mock_web3 = MagicMock()
        mock_web3.eth.contract.return_value = mock_vault

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with patch(f"{ADAPTER_MODULE}.web3_from_chain_id", mock_web3_ctx):
            ok, cd = await readonly_adapter.get_cooldown(account=MOCK_WALLET)

        assert ok is True
        assert cd["cooldownEnd"] == 0
        assert cd["underlyingAmount"] == 0

    # -- get_full_user_state ---------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_full_user_state_mainnet(self, readonly_adapter):
        shares = 50 * 10**18
        usde_balance = 10 * 10**18
        usde_equivalent = 55 * 10**18

        mock_vault = MagicMock()
        mock_vault.functions.cooldowns.return_value = MagicMock(
            call=AsyncMock(return_value=(0, 0))
        )
        mock_vault.functions.convertToAssets.return_value = MagicMock(
            call=AsyncMock(return_value=usde_equivalent)
        )

        mock_web3 = MagicMock()
        mock_web3.eth.contract.return_value = mock_vault

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with (
            patch(f"{ADAPTER_MODULE}.web3_from_chain_id", mock_web3_ctx),
            patch(
                f"{ADAPTER_MODULE}.get_token_balance",
                new_callable=AsyncMock,
                side_effect=[usde_balance, shares],
            ),
        ):
            ok, state = await readonly_adapter.get_full_user_state(
                account=MOCK_WALLET,
                chain_id=CHAIN_ID_ETHEREUM,
            )

        assert ok is True
        assert state["protocol"] == "ethena"
        assert state["hubChainId"] == CHAIN_ID_ETHEREUM
        assert state["chainId"] == CHAIN_ID_ETHEREUM
        assert len(state["positions"]) == 1

        pos = state["positions"][0]
        assert pos["susdeBalance"] == shares
        assert pos["usdeBalance"] == usde_balance
        assert pos["usdeEquivalent"] == usde_equivalent

    @pytest.mark.asyncio
    async def test_get_full_user_state_zero_position_filtered(self, readonly_adapter):
        mock_vault = MagicMock()
        mock_vault.functions.cooldowns.return_value = MagicMock(
            call=AsyncMock(return_value=(0, 0))
        )

        mock_web3 = MagicMock()
        mock_web3.eth.contract.return_value = mock_vault

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with (
            patch(f"{ADAPTER_MODULE}.web3_from_chain_id", mock_web3_ctx),
            patch(
                f"{ADAPTER_MODULE}.get_token_balance",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            ok, state = await readonly_adapter.get_full_user_state(
                account=MOCK_WALLET,
                chain_id=CHAIN_ID_ETHEREUM,
                include_zero_positions=False,
            )

        assert ok is True
        assert state["positions"] == []

    @pytest.mark.asyncio
    async def test_get_full_user_state_zero_position_included(self, readonly_adapter):
        mock_vault = MagicMock()
        mock_vault.functions.cooldowns.return_value = MagicMock(
            call=AsyncMock(return_value=(0, 0))
        )

        mock_web3 = MagicMock()
        mock_web3.eth.contract.return_value = mock_vault

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with (
            patch(f"{ADAPTER_MODULE}.web3_from_chain_id", mock_web3_ctx),
            patch(
                f"{ADAPTER_MODULE}.get_token_balance",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            ok, state = await readonly_adapter.get_full_user_state(
                account=MOCK_WALLET,
                chain_id=CHAIN_ID_ETHEREUM,
                include_zero_positions=True,
            )

        assert ok is True
        assert len(state["positions"]) == 1
        assert state["positions"][0]["susdeBalance"] == 0

    @pytest.mark.asyncio
    async def test_get_full_user_state_non_mainnet(self, readonly_adapter):
        """Non-mainnet: balances from target chain, vault reads from mainnet."""
        chain_id = 42161  # Arbitrum
        shares = 20 * 10**18
        usde_balance = 5 * 10**18
        usde_equivalent = 22 * 10**18
        tokens = ethena_tokens_by_chain_id(chain_id)

        mock_vault = MagicMock()
        mock_vault.functions.cooldowns.return_value = MagicMock(
            call=AsyncMock(return_value=(0, 0))
        )
        mock_vault.functions.convertToAssets.return_value = MagicMock(
            call=AsyncMock(return_value=usde_equivalent)
        )

        mock_web3 = MagicMock()
        mock_web3.eth.contract.return_value = mock_vault

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with (
            patch(f"{ADAPTER_MODULE}.web3_from_chain_id", mock_web3_ctx),
            patch(
                f"{ADAPTER_MODULE}.get_token_balance",
                new_callable=AsyncMock,
                side_effect=[usde_balance, shares],
            ),
        ):
            ok, state = await readonly_adapter.get_full_user_state(
                account=MOCK_WALLET,
                chain_id=chain_id,
            )

        assert ok is True
        assert state["chainId"] == chain_id
        assert state["hubChainId"] == CHAIN_ID_ETHEREUM
        assert len(state["positions"]) == 1
        assert state["positions"][0]["usde"] == tokens["usde"]
        assert state["positions"][0]["susde"] == tokens["susde"]

    # -- deposit_usde ----------------------------------------------------------

    @pytest.mark.asyncio
    async def test_deposit_usde(self, adapter):
        mock_tx_hash = "0xabc123"
        with (
            patch(
                f"{ADAPTER_MODULE}.ensure_allowance",
                new_callable=AsyncMock,
            ) as mock_allowance,
            patch(
                f"{ADAPTER_MODULE}.encode_call",
                new_callable=AsyncMock,
            ) as mock_encode,
            patch(
                f"{ADAPTER_MODULE}.send_transaction",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_allowance.return_value = (True, {})
            mock_encode.return_value = {"data": "0x1234", "to": ETHENA_SUSDE_VAULT_MAINNET}
            mock_send.return_value = mock_tx_hash

            ok, result = await adapter.deposit_usde(amount_assets=100 * 10**18)

        assert ok is True
        assert result == mock_tx_hash

    @pytest.mark.asyncio
    async def test_deposit_usde_invalid_amount(self, adapter):
        ok, result = await adapter.deposit_usde(amount_assets=0)
        assert ok is False
        assert "shares must be positive" == result.lower()

    @pytest.mark.asyncio
    async def test_deposit_usde_negative_amount(self, adapter):
        ok, result = await adapter.deposit_usde(amount_assets=-1)
        assert ok is False
        assert "shares must be positive" == result.lower()

    @pytest.mark.asyncio
    async def test_deposit_usde_no_wallet(self, readonly_adapter):
        ok, result = await readonly_adapter.deposit_usde(amount_assets=10**18)
        assert ok is False
        assert "wallet" in result.lower()

    @pytest.mark.asyncio
    async def test_deposit_usde_allowance_fails(self, adapter):
        with patch(
            f"{ADAPTER_MODULE}.ensure_allowance",
            new_callable=AsyncMock,
            return_value=(False, "approval rejected"),
        ):
            ok, result = await adapter.deposit_usde(amount_assets=10**18)

        assert ok is False

    # -- request_withdraw_by_shares --------------------------------------------

    @pytest.mark.asyncio
    async def test_request_withdraw_by_shares(self, adapter):
        mock_tx_hash = "0xdef456"
        with (
            patch(f"{ADAPTER_MODULE}.encode_call", new_callable=AsyncMock) as mock_encode,
            patch(f"{ADAPTER_MODULE}.send_transaction", new_callable=AsyncMock) as mock_send,
        ):
            mock_encode.return_value = {"data": "0x1234", "to": ETHENA_SUSDE_VAULT_MAINNET}
            mock_send.return_value = mock_tx_hash

            ok, result = await adapter.request_withdraw_by_shares(shares=50 * 10**18)

        assert ok is True
        assert result == mock_tx_hash

    @pytest.mark.asyncio
    async def test_request_withdraw_by_shares_invalid(self, adapter):
        ok, result = await adapter.request_withdraw_by_shares(shares=0)
        assert ok is False
        assert "positive" in result.lower()

    @pytest.mark.asyncio
    async def test_request_withdraw_by_shares_no_wallet(self, readonly_adapter):
        ok, result = await readonly_adapter.request_withdraw_by_shares(shares=10**18)
        assert ok is False
        assert "wallet" in result.lower()

    # -- request_withdraw_by_assets --------------------------------------------

    @pytest.mark.asyncio
    async def test_request_withdraw_by_assets(self, adapter):
        mock_tx_hash = "0x789abc"
        with (
            patch(f"{ADAPTER_MODULE}.encode_call", new_callable=AsyncMock) as mock_encode,
            patch(f"{ADAPTER_MODULE}.send_transaction", new_callable=AsyncMock) as mock_send,
        ):
            mock_encode.return_value = {"data": "0x1234", "to": ETHENA_SUSDE_VAULT_MAINNET}
            mock_send.return_value = mock_tx_hash

            ok, result = await adapter.request_withdraw_by_assets(assets=100 * 10**18)

        assert ok is True
        assert result == mock_tx_hash

    @pytest.mark.asyncio
    async def test_request_withdraw_by_assets_invalid(self, adapter):
        ok, result = await adapter.request_withdraw_by_assets(assets=-5)
        assert ok is False
        assert "positive" in result.lower()

    # -- claim_withdraw --------------------------------------------------------

    @pytest.mark.asyncio
    async def test_claim_withdraw(self, adapter):
        mock_tx_hash = "0xclaim1"

        mock_web3 = MagicMock()
        mock_web3.eth.get_block = AsyncMock(
            return_value={"timestamp": 1_700_200_000}
        )

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with (
            patch.object(
                adapter,
                "get_cooldown",
                new_callable=AsyncMock,
                return_value=(
                    True,
                    {"cooldownEnd": 1_700_100_000, "underlyingAmount": 50 * 10**18},
                ),
            ),
            patch(f"{ADAPTER_MODULE}.web3_from_chain_id", mock_web3_ctx),
            patch(f"{ADAPTER_MODULE}.encode_call", new_callable=AsyncMock) as mock_encode,
            patch(f"{ADAPTER_MODULE}.send_transaction", new_callable=AsyncMock) as mock_send,
        ):
            mock_encode.return_value = {"data": "0x1234", "to": ETHENA_SUSDE_VAULT_MAINNET}
            mock_send.return_value = mock_tx_hash

            ok, result = await adapter.claim_withdraw()

        assert ok is True
        assert result == mock_tx_hash

    @pytest.mark.asyncio
    async def test_claim_withdraw_not_matured(self, adapter):
        mock_web3 = MagicMock()
        mock_web3.eth.get_block = AsyncMock(
            return_value={"timestamp": 1_700_000_000}
        )

        @asynccontextmanager
        async def mock_web3_ctx(_chain_id):
            yield mock_web3

        with (
            patch.object(
                adapter,
                "get_cooldown",
                new_callable=AsyncMock,
                return_value=(
                    True,
                    {"cooldownEnd": 1_700_100_000, "underlyingAmount": 50 * 10**18},
                ),
            ),
            patch(f"{ADAPTER_MODULE}.web3_from_chain_id", mock_web3_ctx),
        ):
            ok, result = await adapter.claim_withdraw(require_matured=True)

        assert ok is False
        assert "cooldown" in result.lower()

    @pytest.mark.asyncio
    async def test_claim_withdraw_no_pending(self, adapter):
        with patch.object(
            adapter,
            "get_cooldown",
            new_callable=AsyncMock,
            return_value=(True, {"cooldownEnd": 0, "underlyingAmount": 0}),
        ):
            ok, result = await adapter.claim_withdraw()

        assert ok is True
        assert "no pending" in result.lower()

    @pytest.mark.asyncio
    async def test_claim_withdraw_no_wallet(self, readonly_adapter):
        ok, result = await readonly_adapter.claim_withdraw()
        assert ok is False
        assert "wallet" in result.lower()

    # -- ethena_tokens_by_chain_id ---------------------------------------------

    def test_tokens_mainnet(self):
        tokens = ethena_tokens_by_chain_id(1)
        assert tokens["usde"] == ETHENA_USDE_MAINNET
        assert tokens["susde"] == ETHENA_SUSDE_VAULT_MAINNET

    def test_tokens_arbitrum(self):
        tokens = ethena_tokens_by_chain_id(42161)
        assert "usde" in tokens
        assert "susde" in tokens
        assert "ena" in tokens
        # Arbitrum uses OFT addresses, not mainnet
        assert tokens["usde"] != ETHENA_USDE_MAINNET
