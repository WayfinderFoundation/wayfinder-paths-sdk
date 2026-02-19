from __future__ import annotations

import asyncio
from typing import Any

from eth_utils import to_checksum_address
from loguru import logger

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.base import MAX_UINT256
from wayfinder_paths.core.constants.euler_v2_abi import (
    ACCOUNT_LENS_ABI,
    EVAULT_ABI,
    EVC_ABI,
    PERSPECTIVE_ABI,
    UTILS_LENS_ABI,
    VAULT_INFO_FULL_KEYS,
    VAULT_LENS_ABI,
)
from wayfinder_paths.core.constants.euler_v2_contracts import EULER_V2_BY_CHAIN
from wayfinder_paths.core.utils.interest import RAY
from wayfinder_paths.core.utils.tokens import ensure_allowance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id


def _tuple_to_dict(value: Any, keys: list[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (list, tuple)):
        return dict(zip(keys, list(value), strict=False))
    # web3.py can return AttributeDict-like objects which are dict subclasses
    try:
        return dict(value)
    except Exception:
        return {}


def _ltv_rows(raw: Any) -> list[Any]:
    try:
        return list(raw or [])
    except Exception:
        return []


class EulerV2Adapter(BaseAdapter):
    """
    Euler v2 (EVK / eVault) adapter.

    Terminology:
    - "vault" is the market address and also the ERC-4626 share token.
    - Underlying is `vault.asset()`.
    - Variable debt token is `vault.dToken()`.
    """

    adapter_type = "EULER_V2"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        strategy_wallet_signing_callback=None,
    ) -> None:
        super().__init__("euler_v2_adapter", config or {})
        cfg = config or {}
        self.strategy_wallet_signing_callback = strategy_wallet_signing_callback

        strategy_addr = (cfg.get("strategy_wallet") or {}).get("address")
        self.strategy_wallet_address: str | None = (
            to_checksum_address(strategy_addr) if strategy_addr else None
        )

        # Cache: (chain_id, vault.lower()) -> address
        self._asset_by_chain_vault: dict[tuple[int, str], str] = {}

    @staticmethod
    def _entry(chain_id: int) -> dict[str, Any]:
        entry = EULER_V2_BY_CHAIN.get(int(chain_id))
        if not entry:
            raise ValueError(f"Unsupported Euler v2 chain_id={chain_id}")
        return entry

    @staticmethod
    def _perspective(entry: dict[str, Any], perspective: str) -> str:
        perspectives = entry.get("perspectives") or {}
        addr = perspectives.get(str(perspective))
        if not addr:
            raise ValueError(f"Unknown perspective: {perspective}")
        return to_checksum_address(str(addr))

    async def _encode_data(
        self,
        *,
        chain_id: int,
        target: str,
        abi: list[dict[str, Any]],
        fn_name: str,
        args: list[Any],
        from_address: str,
    ) -> str:
        tx = await encode_call(
            target=target,
            abi=abi,
            fn_name=fn_name,
            args=args,
            from_address=from_address,
            chain_id=int(chain_id),
        )
        return str(tx.get("data") or "0x")

    async def _vault_asset(self, *, chain_id: int, vault: str) -> str:
        key = (int(chain_id), str(vault).lower())
        cached = self._asset_by_chain_vault.get(key)
        if cached:
            return cached

        async with web3_from_chain_id(int(chain_id)) as web3:
            v = web3.eth.contract(
                address=web3.to_checksum_address(vault),
                abi=EVAULT_ABI,
            )
            asset = await v.functions.asset().call(block_identifier="pending")
            asset = to_checksum_address(str(asset))
            self._asset_by_chain_vault[key] = asset
            return asset

    async def get_verified_vaults(
        self,
        *,
        chain_id: int,
        perspective: str = "governed",
        limit: int | None = None,
    ) -> tuple[bool, list[str] | str]:
        try:
            entry = self._entry(int(chain_id))
            perspective_addr = self._perspective(entry, str(perspective))

            async with web3_from_chain_id(int(chain_id)) as web3:
                p = web3.eth.contract(
                    address=web3.to_checksum_address(perspective_addr),
                    abi=PERSPECTIVE_ABI,
                )
                vaults = await p.functions.verifiedArray().call(
                    block_identifier="pending"
                )

            out = [to_checksum_address(str(v)) for v in (vaults or [])]
            if limit is not None:
                out = out[: max(0, int(limit))]
            return True, out
        except Exception as exc:
            return False, str(exc)

    async def get_vault_info_full(
        self,
        *,
        chain_id: int,
        vault: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            entry = self._entry(int(chain_id))
            vault_lens = entry["lenses"]["vault_lens"]
            vault = to_checksum_address(vault)

            async with web3_from_chain_id(int(chain_id)) as web3:
                lens = web3.eth.contract(
                    address=web3.to_checksum_address(vault_lens),
                    abi=VAULT_LENS_ABI,
                )
                info = await lens.functions.getVaultInfoFull(vault).call(
                    block_identifier="pending"
                )

            return True, _tuple_to_dict(info, VAULT_INFO_FULL_KEYS)
        except Exception as exc:
            return False, str(exc)

    async def get_all_markets(
        self,
        *,
        chain_id: int,
        perspective: str = "governed",
        limit: int | None = None,
        concurrency: int = 10,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        try:
            entry = self._entry(int(chain_id))
            vault_lens_addr = entry["lenses"]["vault_lens"]
            account_lens_addr = entry["lenses"]["account_lens"]
            utils_lens_addr = entry["lenses"]["utils_lens"]
            evc_addr = entry["evc"]
            perspective_addr = self._perspective(entry, str(perspective))

            async with web3_from_chain_id(int(chain_id)) as web3:
                perspective_c = web3.eth.contract(
                    address=web3.to_checksum_address(perspective_addr),
                    abi=PERSPECTIVE_ABI,
                )
                vaults = await perspective_c.functions.verifiedArray().call(
                    block_identifier="pending"
                )
                vaults_list = [to_checksum_address(str(v)) for v in (vaults or [])]
                if limit is not None:
                    vaults_list = vaults_list[: max(0, int(limit))]

                vault_lens = web3.eth.contract(
                    address=web3.to_checksum_address(vault_lens_addr),
                    abi=VAULT_LENS_ABI,
                )
                utils_lens = web3.eth.contract(
                    address=web3.to_checksum_address(utils_lens_addr),
                    abi=UTILS_LENS_ABI,
                )

                sem = asyncio.Semaphore(max(1, int(concurrency)))

                async def _fetch(vault: str) -> tuple[bool, Any]:
                    async with sem:
                        try:
                            info_raw = await vault_lens.functions.getVaultInfoFull(
                                vault
                            ).call(block_identifier="pending")
                            info = _tuple_to_dict(info_raw, VAULT_INFO_FULL_KEYS)

                            (
                                borrow_apy_ray,
                                supply_apy_ray,
                            ) = await utils_lens.functions.getAPYs(vault).call(
                                block_identifier="pending"
                            )

                            ltv_info: list[dict[str, Any]] = []
                            for row in _ltv_rows(info.get("collateralLTVInfo")):
                                try:
                                    if isinstance(row, dict):
                                        collateral = to_checksum_address(
                                            str(row.get("collateral"))
                                        )
                                        borrow_ltv = int(row.get("borrowLTV") or 0)
                                        liq_ltv = int(row.get("liquidationLTV") or 0)
                                        init_liq = int(
                                            row.get("initialLiquidationLTV") or 0
                                        )
                                    else:
                                        collateral = to_checksum_address(str(row[0]))
                                        borrow_ltv = int(row[1] or 0)
                                        liq_ltv = int(row[2] or 0)
                                        init_liq = int(row[3] or 0)
                                except Exception:
                                    continue

                                if borrow_ltv <= 0:
                                    continue
                                ltv_info.append(
                                    {
                                        "collateral": collateral,
                                        "borrow_ltv": borrow_ltv,
                                        "liquidation_ltv": liq_ltv,
                                        "initial_liquidation_ltv": init_liq,
                                    }
                                )

                            return True, {
                                "chain_id": int(chain_id),
                                "evc": to_checksum_address(str(evc_addr)),
                                "vault_lens": to_checksum_address(str(vault_lens_addr)),
                                "account_lens": to_checksum_address(
                                    str(account_lens_addr)
                                ),
                                "utils_lens": to_checksum_address(str(utils_lens_addr)),
                                "perspective": to_checksum_address(
                                    str(perspective_addr)
                                ),
                                "vault": vault,
                                "underlying": to_checksum_address(
                                    str(info.get("asset"))
                                ),
                                "share_token": vault,
                                "debt_token": to_checksum_address(
                                    str(info.get("dToken"))
                                ),
                                "supply_apy": float(int(supply_apy_ray or 0)) / RAY,
                                "borrow_apy": float(int(borrow_apy_ray or 0)) / RAY,
                                "supply_cap": int(info.get("supplyCap") or 0),
                                "borrow_cap": int(info.get("borrowCap") or 0),
                                "cash": int(info.get("totalCash") or 0),
                                "total_borrows": int(info.get("totalBorrowed") or 0),
                                "total_assets": int(info.get("totalAssets") or 0),
                                "vault_symbol": str(info.get("vaultSymbol") or ""),
                                "vault_name": str(info.get("vaultName") or ""),
                                "vault_decimals": int(info.get("vaultDecimals") or 0),
                                "asset_symbol": str(info.get("assetSymbol") or ""),
                                "asset_name": str(info.get("assetName") or ""),
                                "asset_decimals": int(info.get("assetDecimals") or 0),
                                "collateral_ltv_info": ltv_info,
                                "raw": info,
                            }
                        except Exception as exc:
                            logger.warning(
                                "Euler vault %s fetch failed: %s", vault, exc
                            )
                            return False, f"{vault}: {exc}"

                results = await asyncio.gather(
                    *[_fetch(v) for v in vaults_list], return_exceptions=False
                )

            markets = [data for ok, data in results if ok]
            errors = [data for ok, data in results if not ok]
            if not markets:
                return False, f"All vault fetches failed: {'; '.join(errors)}"
            return True, markets
        except Exception as exc:
            return False, str(exc)

    async def get_full_user_state(
        self,
        *,
        chain_id: int,
        account: str,
        include_zero_positions: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            entry = self._entry(int(chain_id))
            evc_addr = to_checksum_address(str(entry["evc"]))
            account_lens_addr = to_checksum_address(
                str(entry["lenses"]["account_lens"])
            )

            acct = to_checksum_address(account)

            async with web3_from_chain_id(int(chain_id)) as web3:
                lens = web3.eth.contract(
                    address=web3.to_checksum_address(account_lens_addr),
                    abi=ACCOUNT_LENS_ABI,
                )
                out = await lens.functions.getAccountEnabledVaultsInfo(
                    evc_addr, acct
                ).call(block_identifier="pending")

            # out: (evcAccountInfo, vaultAccountInfo[], accountRewardInfo[])
            evc_info = out[0] if isinstance(out, (list, tuple)) and len(out) > 0 else {}
            vault_infos = (
                out[1] if isinstance(out, (list, tuple)) and len(out) > 1 else []
            )
            reward_infos = (
                out[2] if isinstance(out, (list, tuple)) and len(out) > 2 else []
            )

            positions: list[dict[str, Any]] = []
            for vi in vault_infos or []:
                try:
                    vault_addr = to_checksum_address(
                        str(vi[2] if not isinstance(vi, dict) else vi.get("vault"))
                    )  # type: ignore[index]
                    asset_addr = to_checksum_address(
                        str(vi[3] if not isinstance(vi, dict) else vi.get("asset"))
                    )  # type: ignore[index]
                    shares = int(
                        vi[5] if not isinstance(vi, dict) else vi.get("shares") or 0
                    )  # type: ignore[index]
                    assets = int(
                        vi[6] if not isinstance(vi, dict) else vi.get("assets") or 0
                    )  # type: ignore[index]
                    borrowed = int(
                        vi[7] if not isinstance(vi, dict) else vi.get("borrowed") or 0  # type: ignore[index]
                    )
                    is_controller = bool(
                        vi[13] if not isinstance(vi, dict) else vi.get("isController")  # type: ignore[index]
                    )
                    is_collateral = bool(
                        vi[14] if not isinstance(vi, dict) else vi.get("isCollateral")  # type: ignore[index]
                    )
                except Exception:
                    continue

                if not include_zero_positions and not (shares or assets or borrowed):
                    continue

                positions.append(
                    {
                        "vault": vault_addr,
                        "underlying": asset_addr,
                        "shares": shares,
                        "assets": assets,
                        "borrowed": borrowed,
                        "is_controller": is_controller,
                        "is_collateral": is_collateral,
                        "raw": vi,
                    }
                )

            return True, {
                "protocol": "euler_v2",
                "chain_id": int(chain_id),
                "evc": evc_addr,
                "account": acct,
                "evcAccountInfo": evc_info,
                "positions": positions,
                "rewards": reward_infos,
                "raw": out,
            }
        except Exception as exc:
            return False, str(exc)

    async def _evc_batch(
        self,
        *,
        chain_id: int,
        items: list[tuple[str, str, int, str]],
        value: int | None = None,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        if not items:
            return False, "no batch items provided"

        entry = self._entry(int(chain_id))
        evc = to_checksum_address(str(entry["evc"]))

        total_value = int(value) if value is not None else sum(int(i[2]) for i in items)
        tx = await encode_call(
            target=evc,
            abi=EVC_ABI,
            fn_name="batch",
            args=[items],
            from_address=strategy,
            chain_id=int(chain_id),
            value=total_value,
        )
        txn_hash = await send_transaction(tx, self.strategy_wallet_signing_callback)
        return True, txn_hash

    async def lend(
        self,
        *,
        chain_id: int,
        vault: str,
        amount: int,
        receiver: str | None = None,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        amount = int(amount)
        if amount <= 0:
            return False, "amount must be positive"

        try:
            vault_addr = to_checksum_address(vault)
            recv = to_checksum_address(receiver) if receiver else strategy
            asset = await self._vault_asset(chain_id=int(chain_id), vault=vault_addr)

            approved = await ensure_allowance(
                token_address=asset,
                owner=strategy,
                spender=vault_addr,
                amount=amount,
                chain_id=int(chain_id),
                signing_callback=self.strategy_wallet_signing_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            data = await self._encode_data(
                chain_id=int(chain_id),
                target=vault_addr,
                abi=EVAULT_ABI,
                fn_name="deposit",
                args=[amount, recv],
                from_address=strategy,
            )
            items = [(vault_addr, strategy, 0, data)]
            return await self._evc_batch(chain_id=int(chain_id), items=items)
        except Exception as exc:
            return False, str(exc)

    async def unlend(
        self,
        *,
        chain_id: int,
        vault: str,
        amount: int,
        receiver: str | None = None,
        withdraw_full: bool = False,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"

        try:
            vault_addr = to_checksum_address(vault)
            recv = to_checksum_address(receiver) if receiver else strategy

            if withdraw_full:
                async with web3_from_chain_id(int(chain_id)) as web3:
                    v = web3.eth.contract(
                        address=web3.to_checksum_address(vault_addr),
                        abi=EVAULT_ABI,
                    )
                    shares = await v.functions.balanceOf(strategy).call(
                        block_identifier="pending"
                    )
                shares = int(shares or 0)
                if shares <= 0:
                    return False, "no shares to redeem"

                data = await self._encode_data(
                    chain_id=int(chain_id),
                    target=vault_addr,
                    abi=EVAULT_ABI,
                    fn_name="redeem",
                    args=[shares, recv, strategy],
                    from_address=strategy,
                )
                items = [(vault_addr, strategy, 0, data)]
                return await self._evc_batch(chain_id=int(chain_id), items=items)

            qty = int(amount)
            if qty <= 0:
                return False, "withdraw amount must be positive"

            data = await self._encode_data(
                chain_id=int(chain_id),
                target=vault_addr,
                abi=EVAULT_ABI,
                fn_name="withdraw",
                args=[qty, recv, strategy],
                from_address=strategy,
            )
            items = [(vault_addr, strategy, 0, data)]
            return await self._evc_batch(chain_id=int(chain_id), items=items)
        except Exception as exc:
            return False, str(exc)

    async def set_collateral(
        self,
        *,
        chain_id: int,
        vault: str,
        use_as_collateral: bool = True,
        account: str | None = None,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"

        try:
            entry = self._entry(int(chain_id))
            evc = to_checksum_address(str(entry["evc"]))
            acct = to_checksum_address(account) if account else strategy
            vault_addr = to_checksum_address(vault)

            fn_name = "enableCollateral" if use_as_collateral else "disableCollateral"
            tx = await encode_call(
                target=evc,
                abi=EVC_ABI,
                fn_name=fn_name,
                args=[acct, vault_addr],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.strategy_wallet_signing_callback)
            return True, txn_hash
        except Exception as exc:
            return False, str(exc)

    async def remove_collateral(
        self,
        *,
        chain_id: int,
        vault: str,
        account: str | None = None,
    ) -> tuple[bool, Any]:
        return await self.set_collateral(
            chain_id=int(chain_id),
            vault=str(vault),
            use_as_collateral=False,
            account=account,
        )

    async def borrow(
        self,
        *,
        chain_id: int,
        vault: str,
        amount: int,
        receiver: str | None = None,
        collateral_vaults: list[str] | None = None,
        enable_controller: bool = True,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        amount = int(amount)
        if amount <= 0:
            return False, "amount must be positive"

        try:
            entry = self._entry(int(chain_id))
            evc = to_checksum_address(str(entry["evc"]))
            vault_addr = to_checksum_address(vault)
            recv = to_checksum_address(receiver) if receiver else strategy

            items: list[tuple[str, str, int, str]] = []

            for cv in collateral_vaults or []:
                c_vault = to_checksum_address(cv)
                enable_collateral_data = await self._encode_data(
                    chain_id=int(chain_id),
                    target=evc,
                    abi=EVC_ABI,
                    fn_name="enableCollateral",
                    args=[strategy, c_vault],
                    from_address=strategy,
                )
                items.append((evc, ZERO_ADDRESS, 0, enable_collateral_data))

            if enable_controller:
                enable_controller_data = await self._encode_data(
                    chain_id=int(chain_id),
                    target=evc,
                    abi=EVC_ABI,
                    fn_name="enableController",
                    args=[strategy, vault_addr],
                    from_address=strategy,
                )
                items.append((evc, ZERO_ADDRESS, 0, enable_controller_data))

            borrow_data = await self._encode_data(
                chain_id=int(chain_id),
                target=vault_addr,
                abi=EVAULT_ABI,
                fn_name="borrow",
                args=[amount, recv],
                from_address=strategy,
            )
            items.append((vault_addr, strategy, 0, borrow_data))

            return await self._evc_batch(chain_id=int(chain_id), items=items)
        except Exception as exc:
            return False, str(exc)

    async def repay(
        self,
        *,
        chain_id: int,
        vault: str,
        amount: int,
        receiver: str | None = None,
        repay_full: bool = False,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"

        try:
            vault_addr = to_checksum_address(vault)
            recv = to_checksum_address(receiver) if receiver else strategy
            qty = int(amount)
            if qty <= 0 and not repay_full:
                return False, "amount must be positive (or set repay_full=True)"

            repay_amount = MAX_UINT256 if repay_full else qty
            allowance_target = MAX_UINT256 if repay_full else qty

            asset = await self._vault_asset(chain_id=int(chain_id), vault=vault_addr)
            approved = await ensure_allowance(
                token_address=asset,
                owner=strategy,
                spender=vault_addr,
                amount=allowance_target,
                chain_id=int(chain_id),
                signing_callback=self.strategy_wallet_signing_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            data = await self._encode_data(
                chain_id=int(chain_id),
                target=vault_addr,
                abi=EVAULT_ABI,
                fn_name="repay",
                args=[int(repay_amount), recv],
                from_address=strategy,
            )
            items = [(vault_addr, strategy, 0, data)]
            return await self._evc_batch(chain_id=int(chain_id), items=items)
        except Exception as exc:
            return False, str(exc)
