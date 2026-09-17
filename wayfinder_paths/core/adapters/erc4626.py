from __future__ import annotations

from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.core.constants.base import MAX_UINT256
from wayfinder_paths.core.constants.erc4626_abi import ERC4626_ABI
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.core.utils.tokens import ensure_allowance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction


class ERC4626VaultMixin:
    """Common raw-unit vault actions; protocol adapters own discovery and policy."""

    wallet_address: str | None
    sign_callback: Any

    async def _vault_asset(self, *, chain_id: int, vault_address: str) -> str:
        async with web3_utils.web3_from_chain_id(int(chain_id)) as web3:
            contract = web3.eth.contract(
                address=to_checksum_address(str(vault_address)), abi=ERC4626_ABI
            )
            asset = await contract.functions.asset().call(block_identifier="pending")
            return to_checksum_address(str(asset))

    async def vault_deposit(
        self,
        *,
        chain_id: int,
        vault_address: str,
        assets: int,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        assets = int(assets)
        if assets <= 0:
            return False, "assets must be positive"

        try:
            vault = to_checksum_address(str(vault_address))
            asset = await self._vault_asset(chain_id=int(chain_id), vault_address=vault)

            approved = await ensure_allowance(
                token_address=asset,
                owner=strategy,
                spender=vault,
                amount=int(assets),
                chain_id=int(chain_id),
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=vault,
                abi=ERC4626_ABI,
                fn_name="deposit",
                args=[int(assets), strategy],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def vault_withdraw(
        self,
        *,
        chain_id: int,
        vault_address: str,
        assets: int,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        assets = int(assets)
        if assets <= 0:
            return False, "assets must be positive"

        try:
            vault = to_checksum_address(str(vault_address))
            tx = await encode_call(
                target=vault,
                abi=ERC4626_ABI,
                fn_name="withdraw",
                args=[int(assets), strategy, strategy],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def vault_mint(
        self,
        *,
        chain_id: int,
        vault_address: str,
        shares: int,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        shares = int(shares)
        if shares <= 0:
            return False, "shares must be positive"

        try:
            vault = to_checksum_address(str(vault_address))
            asset = await self._vault_asset(chain_id=int(chain_id), vault_address=vault)

            approved = await ensure_allowance(
                token_address=asset,
                owner=strategy,
                spender=vault,
                amount=MAX_UINT256,
                chain_id=int(chain_id),
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=vault,
                abi=ERC4626_ABI,
                fn_name="mint",
                args=[int(shares), strategy],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def vault_redeem(
        self,
        *,
        chain_id: int,
        vault_address: str,
        shares: int,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        shares = int(shares)
        if shares <= 0:
            return False, "shares must be positive"

        try:
            vault = to_checksum_address(str(vault_address))
            tx = await encode_call(
                target=vault,
                abi=ERC4626_ABI,
                fn_name="redeem",
                args=[int(shares), strategy, strategy],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
