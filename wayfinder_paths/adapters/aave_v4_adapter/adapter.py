from __future__ import annotations

import asyncio
from typing import Any, Literal

from eth_utils import to_checksum_address

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.adapters.erc4626 import ERC4626VaultMixin
from wayfinder_paths.core.constants.aave_v4_abi import (
    ACCOUNT_FIELDS,
    CONFIG_FIELDS,
    HUB_ABI,
    HUB_CONFIG_FIELDS,
    RESERVE_FIELDS,
    SPOKE_ABI,
)
from wayfinder_paths.core.constants.aave_v4_contracts import (
    ARC_AAVE_V4_SPOKES,
    ARC_AAVE_V4_VAULTS,
)
from wayfinder_paths.core.constants.chains import CHAIN_ID_ARC
from wayfinder_paths.core.utils.tokens import ensure_allowance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id


class AaveV4Adapter(ERC4626VaultMixin, BaseAdapter):
    adapter_type = "AAVE_V4"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        sign_callback: Any = None,
        wallet_address: str | None = None,
    ) -> None:
        super().__init__("aave_v4_adapter", config)
        self.chain_id = int((config or {}).get("chain_id", CHAIN_ID_ARC))
        if self.chain_id != CHAIN_ID_ARC:
            raise ValueError("Aave v4 is currently configured for Arc only")
        self.wallet_address = (
            to_checksum_address(wallet_address) if wallet_address else None
        )
        self.sign_callback = sign_callback

    @staticmethod
    def _spoke(spoke: str) -> str:
        if spoke not in ARC_AAVE_V4_SPOKES:
            raise ValueError("spoke must be main or forex")
        return ARC_AAVE_V4_SPOKES[spoke]

    async def get_reserve(
        self, reserve_id: int, *, spoke: str = "main"
    ) -> tuple[bool, Any]:
        try:
            address = self._spoke(spoke)
            async with web3_from_chain_id(self.chain_id) as web3:
                contract = web3.eth.contract(address=address, abi=SPOKE_ABI)
                reserve, config = await asyncio.gather(
                    contract.functions.getReserve(reserve_id).call(),
                    contract.functions.getReserveConfig(reserve_id).call(),
                )
                result = dict(
                    zip((name for name, _ in RESERVE_FIELDS), reserve, strict=True)
                )
                result.update(
                    zip((name for name, _ in CONFIG_FIELDS), config, strict=True)
                )
                hub = web3.eth.contract(
                    address=to_checksum_address(result["hub"]), abi=HUB_ABI
                )
                asset_id = result["asset_id"]
                liquidity, hub_config, supplied, borrowed = await asyncio.gather(
                    hub.functions.getAssetLiquidity(asset_id).call(),
                    hub.functions.getSpokeConfig(asset_id, address).call(),
                    hub.functions.getSpokeAddedAssets(asset_id, address).call(),
                    hub.functions.getSpokeTotalOwed(asset_id, address).call(),
                )
            result.update(
                reserve_id=reserve_id,
                spoke=spoke,
                spoke_address=address,
                liquidity=liquidity,
                hub_config=dict(
                    zip(
                        (name for name, _ in HUB_CONFIG_FIELDS), hub_config, strict=True
                    )
                ),
                supplied=supplied,
                borrowed=borrowed,
            )
            return True, result
        except Exception as exc:
            return False, str(exc)

    async def get_markets(self, *, spoke: str = "main") -> tuple[bool, Any]:
        try:
            async with web3_from_chain_id(self.chain_id) as web3:
                contract = web3.eth.contract(address=self._spoke(spoke), abi=SPOKE_ABI)
                count = await contract.functions.getReserveCount().call()
            markets = []
            for reserve_id in range(count):
                ok, reserve = await self.get_reserve(reserve_id, spoke=spoke)
                if not ok:
                    return False, reserve
                markets.append(reserve)
            return True, markets
        except Exception as exc:
            return False, str(exc)

    async def get_user_state(
        self, *, spoke: str = "main", account: str | None = None
    ) -> tuple[bool, Any]:
        try:
            owner = to_checksum_address(account or self.wallet_address or "")
            async with web3_from_chain_id(self.chain_id) as web3:
                contract = web3.eth.contract(address=self._spoke(spoke), abi=SPOKE_ABI)
                count, data = await asyncio.gather(
                    contract.functions.getReserveCount().call(),
                    contract.functions.getUserAccountData(owner).call(),
                )
                positions = []
                for reserve_id in range(count):
                    supplied, debt = await asyncio.gather(
                        contract.functions.getUserSuppliedAssets(
                            reserve_id, owner
                        ).call(),
                        contract.functions.getUserTotalDebt(reserve_id, owner).call(),
                    )
                    positions.append(
                        {"reserve_id": reserve_id, "supplied": supplied, "debt": debt}
                    )
            return True, {
                "spoke": spoke,
                "account": owner,
                "account_data": dict(
                    zip((name for name, _ in ACCOUNT_FIELDS), data, strict=True)
                ),
                "positions": positions,
            }
        except Exception as exc:
            return False, str(exc)

    async def _action(
        self,
        action: Literal["supply", "withdraw", "borrow", "repay"],
        reserve_id: int,
        amount: int,
        spoke: str,
    ) -> tuple[bool, Any]:
        try:
            if not self.wallet_address or self.sign_callback is None:
                raise ValueError("wallet_address and sign_callback are required")
            if amount <= 0:
                raise ValueError("amount must be positive raw ERC-20 units")
            ok, reserve = await self.get_reserve(reserve_id, spoke=spoke)
            if not ok:
                return False, reserve
            config = reserve["hub_config"]
            if reserve["paused"] or not config["active"] or config["halted"]:
                raise ValueError("Reserve or Hub spoke is paused, inactive or halted")
            if action in ("supply", "borrow") and reserve["frozen"]:
                raise ValueError("Reserve is frozen")
            if action == "borrow" and not reserve["borrowable"]:
                raise ValueError("Reserve is not borrowable")
            if action in ("withdraw", "borrow") and amount > reserve["liquidity"]:
                raise ValueError("Insufficient Hub liquidity")
            if action in ("supply", "borrow"):
                cap = config["add_cap" if action == "supply" else "draw_cap"]
                used = reserve["supplied" if action == "supply" else "borrowed"]
                if (
                    cap != (1 << 40) - 1
                    and used + amount > cap * 10 ** reserve["decimals"]
                ):
                    raise ValueError("Spoke cap would be exceeded")
            address = self._spoke(spoke)
            if action in ("supply", "repay"):
                ok, result = await ensure_allowance(
                    token_address=reserve["underlying"],
                    owner=self.wallet_address,
                    spender=address,
                    amount=amount,
                    approval_amount=amount,
                    chain_id=self.chain_id,
                    signing_callback=self.sign_callback,
                )
                if not ok:
                    return False, result
            tx = await encode_call(
                target=address,
                abi=SPOKE_ABI,
                fn_name=action,
                args=[reserve_id, amount, self.wallet_address],
                from_address=self.wallet_address,
                chain_id=self.chain_id,
            )
            # Simulates health-factor/collateral checks against pending state before sending.
            async with web3_from_chain_id(self.chain_id) as web3:
                await web3.eth.call(
                    {
                        key: tx[key]
                        for key in ("from", "to", "data", "value")
                        if key in tx
                    },
                    block_identifier="pending",
                )
            return True, await send_transaction(tx, self.sign_callback)
        except Exception as exc:
            return False, str(exc)

    async def supply(
        self, reserve_id: int, amount: int, *, spoke: str = "main"
    ) -> tuple[bool, Any]:
        return await self._action("supply", reserve_id, amount, spoke)

    async def withdraw(
        self, reserve_id: int, amount: int, *, spoke: str = "main"
    ) -> tuple[bool, Any]:
        return await self._action("withdraw", reserve_id, amount, spoke)

    async def borrow(
        self, reserve_id: int, amount: int, *, spoke: str = "main"
    ) -> tuple[bool, Any]:
        return await self._action("borrow", reserve_id, amount, spoke)

    async def repay(
        self, reserve_id: int, amount: int, *, spoke: str = "main"
    ) -> tuple[bool, Any]:
        return await self._action("repay", reserve_id, amount, spoke)

    async def set_collateral(
        self, reserve_id: int, enabled: bool, *, spoke: str = "main"
    ) -> tuple[bool, Any]:
        try:
            if not self.wallet_address or self.sign_callback is None:
                raise ValueError("wallet_address and sign_callback are required")
            tx = await encode_call(
                target=self._spoke(spoke),
                abi=SPOKE_ABI,
                fn_name="setUsingAsCollateral",
                args=[reserve_id, enabled, self.wallet_address],
                from_address=self.wallet_address,
                chain_id=self.chain_id,
            )
            return True, await send_transaction(tx, self.sign_callback)
        except Exception as exc:
            return False, str(exc)

    async def tokenized_deposit(self, symbol: str, assets: int) -> tuple[bool, Any]:
        if symbol not in ARC_AAVE_V4_VAULTS:
            return False, "Unknown tokenized spoke"
        return await self.vault_deposit(
            chain_id=self.chain_id,
            vault_address=ARC_AAVE_V4_VAULTS[symbol],
            assets=assets,
        )

    async def tokenized_redeem(self, symbol: str, shares: int) -> tuple[bool, Any]:
        if symbol not in ARC_AAVE_V4_VAULTS:
            return False, "Unknown tokenized spoke"
        return await self.vault_redeem(
            chain_id=self.chain_id,
            vault_address=ARC_AAVE_V4_VAULTS[symbol],
            shares=shares,
        )
