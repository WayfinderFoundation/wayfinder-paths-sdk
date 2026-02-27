from __future__ import annotations

from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.clients.MerklClient import MERKL_CLIENT
from wayfinder_paths.core.clients.MorphoClient import MORPHO_CLIENT
from wayfinder_paths.core.clients.MorphoRewardsClient import MORPHO_REWARDS_CLIENT
from wayfinder_paths.core.constants.base import MANTISSA, MAX_UINT256
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.erc4626_abi import ERC4626_ABI
from wayfinder_paths.core.constants.morpho_abi import MORPHO_BLUE_ABI
from wayfinder_paths.core.constants.morpho_bundler_abi import BUNDLER3_ABI
from wayfinder_paths.core.constants.morpho_constants import (
    MERKL_DISTRIBUTOR_ADDRESS,
    ORACLE_PRICE_SCALE,
)
from wayfinder_paths.core.constants.morpho_contracts import MORPHO_BY_CHAIN
from wayfinder_paths.core.constants.public_allocator_abi import PUBLIC_ALLOCATOR_ABI
from wayfinder_paths.core.constants.rewards_abi import MERKL_DISTRIBUTOR_ABI
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.core.utils.tokens import ensure_allowance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction

MarketParamsTuple = tuple[str, str, str, str, int]


class MorphoAdapter(BaseAdapter):
    adapter_type = "MORPHO"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        sign_callback=None,
        wallet_address: str | None = None,
    ) -> None:
        super().__init__("morpho_adapter", config or {})
        self.sign_callback = sign_callback

        self.wallet_address: str | None = (
            to_checksum_address(wallet_address) if wallet_address else None
        )

        cfg = config or {}
        bundler_addr = (
            cfg.get("bundler_address")
            or (cfg.get("bundler") or {}).get("address")
            or (cfg.get("bundler3") or {}).get("address")
        )
        self.bundler_address: str | None = (
            to_checksum_address(str(bundler_addr)) if bundler_addr else None
        )

        self._morpho_address_cache: dict[int, str] = {}
        self._public_allocator_address_cache: dict[int, str] = {}
        self._market_cache: dict[tuple[int, str], dict[str, Any]] = {}

    async def _morpho_address(self, *, chain_id: int) -> str:
        cid = int(chain_id)
        if cached := self._morpho_address_cache.get(cid):
            return cached

        entry = MORPHO_BY_CHAIN.get(int(chain_id))
        if entry and entry.get("morpho"):
            addr = to_checksum_address(str(entry["morpho"]))
            self._morpho_address_cache[cid] = addr
            return addr

        # Fallback to the Morpho API if constants are missing/out-of-date.
        addr = to_checksum_address(
            str(await MORPHO_CLIENT.get_morpho_address(chain_id=cid))
        )
        self._morpho_address_cache[cid] = addr
        return addr

    async def _public_allocator_address(self, *, chain_id: int) -> str:
        cid = int(chain_id)
        if cached := self._public_allocator_address_cache.get(cid):
            return cached

        entry = MORPHO_BY_CHAIN.get(int(chain_id))
        if entry and entry.get("public_allocator"):
            addr = to_checksum_address(str(entry["public_allocator"]))
            self._public_allocator_address_cache[cid] = addr
            return addr

        by_chain = await MORPHO_CLIENT.get_morpho_by_chain()
        api_entry = by_chain.get(int(chain_id)) if isinstance(by_chain, dict) else None
        if api_entry and api_entry.get("public_allocator"):
            addr = to_checksum_address(str(api_entry["public_allocator"]))
            self._public_allocator_address_cache[cid] = addr
            return addr

        raise ValueError(f"Public allocator not found for chain_id={chain_id}")

    async def _get_market(self, *, chain_id: int, unique_key: str) -> dict[str, Any]:
        cache_key = (int(chain_id), str(unique_key).lower())
        if cached := self._market_cache.get(cache_key):
            return cached
        market = await MORPHO_CLIENT.get_market_by_unique_key(
            unique_key=str(unique_key), chain_id=int(chain_id)
        )
        if not isinstance(market, dict):
            raise ValueError(f"Invalid market response for uniqueKey={unique_key}")
        self._market_cache[cache_key] = market
        return market

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

    async def set_authorization(
        self,
        *,
        chain_id: int,
        authorized: str,
        is_authorized: bool = True,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"

        try:
            morpho = await self._morpho_address(chain_id=int(chain_id))
            tx = await encode_call(
                target=morpho,
                abi=MORPHO_BLUE_ABI,
                fn_name="setAuthorization",
                args=[to_checksum_address(str(authorized)), bool(is_authorized)],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def set_authorization_with_sig(
        self,
        *,
        chain_id: int,
        authorization: dict[str, Any] | tuple[Any, ...],
        signature: dict[str, Any] | tuple[Any, ...],
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"

        try:
            morpho = await self._morpho_address(chain_id=int(chain_id))

            if isinstance(authorization, dict):
                authorization_tuple = (
                    to_checksum_address(str(authorization["authorizer"])),
                    to_checksum_address(str(authorization["authorized"])),
                    bool(authorization["isAuthorized"]),
                    int(authorization["nonce"]),
                    int(authorization["deadline"]),
                )
            else:
                authorization_tuple = authorization

            if isinstance(signature, dict):
                signature_tuple = (
                    int(signature["v"]),
                    signature["r"],
                    signature["s"],
                )
            else:
                signature_tuple = signature

            tx = await encode_call(
                target=morpho,
                abi=MORPHO_BLUE_ABI,
                fn_name="setAuthorizationWithSig",
                args=[authorization_tuple, signature_tuple],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    @staticmethod
    def _market_params_from_market(market: dict[str, Any]) -> MarketParamsTuple:
        loan = market.get("loanAsset") or {}
        collateral = market.get("collateralAsset") or {}
        oracle = market.get("oracle") or {}

        loan_addr = loan.get("address")
        collateral_addr = collateral.get("address")
        oracle_addr = oracle.get("address")
        irm_addr = market.get("irmAddress")
        lltv_raw = market.get("lltv")

        if not (
            loan_addr and collateral_addr and oracle_addr and irm_addr and lltv_raw
        ):
            raise ValueError("market is missing required MarketParams fields")

        try:
            lltv = int(lltv_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid market.lltv: {lltv_raw}") from exc

        return (
            to_checksum_address(str(loan_addr)),
            to_checksum_address(str(collateral_addr)),
            to_checksum_address(str(oracle_addr)),
            to_checksum_address(str(irm_addr)),
            int(lltv),
        )

    @staticmethod
    def _format_market(
        chain_id: int, morpho: str, market: dict[str, Any]
    ) -> dict[str, Any]:
        loan = market.get("loanAsset") or {}
        collateral = market.get("collateralAsset") or {}
        oracle = market.get("oracle") or {}
        state = market.get("state") or {}
        rewards = state.get("rewards") or []

        reward_supply_apr = 0.0
        reward_borrow_apr = 0.0
        incentives: list[dict[str, Any]] = []
        for r in rewards if isinstance(rewards, list) else []:
            if not isinstance(r, dict):
                continue
            supply_apr = r.get("supplyApr")
            borrow_apr = r.get("borrowApr")
            try:
                reward_supply_apr += float(supply_apr or 0.0)
            except (TypeError, ValueError):
                pass
            try:
                reward_borrow_apr += float(borrow_apr or 0.0)
            except (TypeError, ValueError):
                pass
            asset = r.get("asset") or {}
            incentives.append(
                {
                    "token": {
                        "address": asset.get("address"),
                        "symbol": asset.get("symbol"),
                        "name": asset.get("name"),
                        "decimals": asset.get("decimals"),
                        "price_usd": asset.get("priceUsd"),
                    },
                    "supplyApr": supply_apr,
                    "borrowApr": borrow_apr,
                }
            )

        out: dict[str, Any] = {
            "uniqueKey": market.get("uniqueKey"),
            "chainId": int(chain_id),
            "morpho": morpho,
            "listed": bool(market.get("listed")),
            "lltv": int(market.get("lltv") or 0),
            "irm": market.get("irmAddress"),
            "oracle": oracle.get("address"),
            "reallocatableLiquidityAssets": market.get("reallocatableLiquidityAssets"),
            "warnings": market.get("warnings") or [],
            "loan": {
                "address": loan.get("address"),
                "symbol": loan.get("symbol"),
                "name": loan.get("name"),
                "decimals": loan.get("decimals"),
                "price_usd": loan.get("priceUsd"),
            },
            "collateral": {
                "address": collateral.get("address"),
                "symbol": collateral.get("symbol"),
                "name": collateral.get("name"),
                "decimals": collateral.get("decimals"),
                "price_usd": collateral.get("priceUsd"),
            },
            "state": {
                "supply_apy": state.get("supplyApy"),
                "net_supply_apy": state.get("netSupplyApy"),
                "borrow_apy": state.get("borrowApy"),
                "net_borrow_apy": state.get("netBorrowApy"),
                "reward_supply_apr": reward_supply_apr,
                "reward_borrow_apr": reward_borrow_apr,
                "incentives": incentives,
                # Approximate "all-in" yields for display (rewards are linear APR).
                "supply_apy_with_rewards": (
                    float(state.get("supplyApy") or 0.0) + reward_supply_apr
                ),
                "borrow_apy_with_rewards": (
                    float(state.get("borrowApy") or 0.0) - reward_borrow_apr
                ),
                "utilization": state.get("utilization"),
                "apy_at_target": state.get("apyAtTarget"),
                "price": state.get("price"),
                "liquidity_assets": int(state.get("liquidityAssets") or 0),
                "liquidity_assets_usd": state.get("liquidityAssetsUsd"),
                "supply_assets": int(state.get("supplyAssets") or 0),
                "supply_assets_usd": state.get("supplyAssetsUsd"),
                "borrow_assets": int(state.get("borrowAssets") or 0),
                "borrow_assets_usd": state.get("borrowAssetsUsd"),
            },
        }
        return out

    async def get_all_markets(
        self,
        *,
        chain_id: int,
        listed: bool | None = True,
        include_idle: bool = False,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        try:
            morpho = await self._morpho_address(chain_id=int(chain_id))
            markets = await MORPHO_CLIENT.get_all_markets(
                chain_id=int(chain_id),
                listed=listed,
                include_idle=include_idle,
            )
            out = [self._format_market(int(chain_id), morpho, m) for m in markets]
            return True, out
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_market_entry(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            morpho = await self._morpho_address(chain_id=int(chain_id))
            market = await self._get_market(
                chain_id=int(chain_id), unique_key=str(market_unique_key)
            )
            return True, self._format_market(int(chain_id), morpho, market)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_market_state(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            morpho = await self._morpho_address(chain_id=int(chain_id))
            market = await self._get_market(
                chain_id=int(chain_id), unique_key=str(market_unique_key)
            )
            out = self._format_market(int(chain_id), morpho, market)
            out["publicAllocatorSharedLiquidity"] = (
                market.get("publicAllocatorSharedLiquidity") or []
            )
            out["supplyingVaults"] = market.get("supplyingVaults") or []
            out["supplyingVaultV2s"] = market.get("supplyingVaultV2s") or []
            return True, out
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    @staticmethod
    def _filter_series(
        series: list[dict[str, Any]] | None,
        *,
        start_timestamp: int | None,
        end_timestamp: int | None,
    ) -> list[dict[str, Any]]:
        if not series:
            return []
        out: list[dict[str, Any]] = []
        for p in series:
            if not isinstance(p, dict):
                continue
            try:
                x = int(p.get("x"))
            except (TypeError, ValueError):
                continue
            if start_timestamp is not None and x < int(start_timestamp):
                continue
            if end_timestamp is not None and x > int(end_timestamp):
                continue
            out.append({"x": x, "y": p.get("y")})
        return out

    async def get_market_historical_apy(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
        interval: str = "DAY",
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        interval_norm = str(interval or "DAY").upper()
        prefix = {
            "HOUR": "",
            "DAY": "daily",
            "WEEK": "weekly",
            "MONTH": "monthly",
            "QUARTER": "quarterly",
            "YEAR": "yearly",
        }.get(interval_norm)
        if prefix is None:
            return False, f"Unsupported interval: {interval}"

        try:
            history = await MORPHO_CLIENT.get_market_history(
                unique_key=str(market_unique_key),
                chain_id=int(chain_id),
            )

            def k(name: str) -> str:
                if not prefix:
                    return name
                return f"{prefix}{name[0].upper()}{name[1:]}"

            supply = self._filter_series(
                history.get(k("supplyApy")),
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
            )
            net_supply = self._filter_series(
                history.get(k("netSupplyApy")),
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
            )
            borrow = self._filter_series(
                history.get(k("borrowApy")),
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
            )
            net_borrow = self._filter_series(
                history.get(k("netBorrowApy")),
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
            )

            return (
                True,
                {
                    "uniqueKey": str(market_unique_key),
                    "chainId": int(chain_id),
                    "interval": interval_norm,
                    "series": {
                        "supplyApy": supply,
                        "netSupplyApy": net_supply,
                        "borrowApy": borrow,
                        "netBorrowApy": net_borrow,
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    @staticmethod
    def _format_vault_rewards(rewards: Any) -> tuple[float, list[dict[str, Any]]]:
        reward_supply_apr = 0.0
        incentives: list[dict[str, Any]] = []
        for r in rewards if isinstance(rewards, list) else []:
            if not isinstance(r, dict):
                continue
            supply_apr = r.get("supplyApr")
            try:
                reward_supply_apr += float(supply_apr or 0.0)
            except (TypeError, ValueError):
                pass
            asset = r.get("asset") or {}
            incentives.append(
                {
                    "token": {
                        "address": asset.get("address"),
                        "symbol": asset.get("symbol"),
                        "name": asset.get("name"),
                        "decimals": asset.get("decimals"),
                        "price_usd": asset.get("priceUsd"),
                    },
                    "supplyApr": supply_apr,
                    "yearlySupplyTokens": r.get("yearlySupplyTokens"),
                    "amountPerSuppliedToken": r.get("amountPerSuppliedToken"),
                }
            )
        return reward_supply_apr, incentives

    @classmethod
    def _format_vault_v1(cls, chain_id: int, vault: dict[str, Any]) -> dict[str, Any]:
        asset = vault.get("asset") or {}
        state = vault.get("state") or {}
        reward_supply_apr, incentives = cls._format_vault_rewards(state.get("rewards"))
        apy = state.get("apy")
        try:
            apy_float = float(apy or 0.0)
        except (TypeError, ValueError):
            apy_float = 0.0
        return {
            "type": "vault",
            "version": "v1",
            "chainId": int(chain_id),
            "address": vault.get("address"),
            "symbol": vault.get("symbol"),
            "name": vault.get("name"),
            "listed": bool(vault.get("listed")),
            "featured": bool(vault.get("featured")),
            "warnings": vault.get("warnings") or [],
            "asset": {
                "address": asset.get("address"),
                "symbol": asset.get("symbol"),
                "name": asset.get("name"),
                "decimals": asset.get("decimals"),
                "price_usd": asset.get("priceUsd"),
            },
            "state": {
                "apy": apy,
                "net_apy": state.get("netApy"),
                "net_apy_without_rewards": state.get("netApyWithoutRewards"),
                "reward_supply_apr": reward_supply_apr,
                "apy_with_rewards": apy_float + reward_supply_apr,
                "total_assets": int(state.get("totalAssets") or 0),
                "total_assets_usd": state.get("totalAssetsUsd"),
                "incentives": incentives,
                "allocation": state.get("allocation") or [],
            },
        }

    @classmethod
    def _format_vault_v2(cls, chain_id: int, vault: dict[str, Any]) -> dict[str, Any]:
        asset = vault.get("asset") or {}
        reward_supply_apr, incentives = cls._format_vault_rewards(vault.get("rewards"))
        apy = vault.get("apy")
        try:
            apy_float = float(apy or 0.0)
        except (TypeError, ValueError):
            apy_float = 0.0
        return {
            "type": "vault",
            "version": "v2",
            "chainId": int(chain_id),
            "address": vault.get("address"),
            "symbol": vault.get("symbol"),
            "name": vault.get("name"),
            "listed": bool(vault.get("listed")),
            "warnings": vault.get("warnings") or [],
            "asset": {
                "address": asset.get("address"),
                "symbol": asset.get("symbol"),
                "name": asset.get("name"),
                "decimals": asset.get("decimals"),
                "price_usd": asset.get("priceUsd"),
            },
            "state": {
                "apy": apy,
                "net_apy": vault.get("netApy"),
                "avg_apy": vault.get("avgApy"),
                "avg_net_apy": vault.get("avgNetApy"),
                "reward_supply_apr": reward_supply_apr,
                "apy_with_rewards": apy_float + reward_supply_apr,
                "total_assets": int(vault.get("totalAssets") or 0),
                "total_assets_usd": vault.get("totalAssetsUsd"),
                "liquidity": int(vault.get("liquidity") or 0),
                "liquidity_usd": vault.get("liquidityUsd"),
                "incentives": incentives,
                "liquidity_adapter": vault.get("liquidityAdapter"),
                "adapters": ((vault.get("adapters") or {}).get("items") or [])
                if isinstance(vault.get("adapters"), dict)
                else (vault.get("adapters") or []),
            },
        }

    async def get_all_vaults(
        self,
        *,
        chain_id: int,
        listed: bool | None = True,
        include_v2: bool = True,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        try:
            v1 = await MORPHO_CLIENT.get_all_vaults(
                chain_id=int(chain_id), listed=listed
            )
            out: list[dict[str, Any]] = [
                self._format_vault_v1(int(chain_id), v) for v in v1
            ]

            if include_v2:
                v2 = await MORPHO_CLIENT.get_all_vault_v2s(
                    chain_id=int(chain_id), listed=listed
                )
                out.extend([self._format_vault_v2(int(chain_id), v) for v in v2])

            return True, out
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_vault_entry(
        self,
        *,
        chain_id: int,
        vault_address: str,
        version: str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            addr = to_checksum_address(str(vault_address))
            v = (version or "").lower()
            if v in ("v2", "2", "vaultv2"):
                vault = await MORPHO_CLIENT.get_vault_v2_by_address(
                    address=addr, chain_id=int(chain_id)
                )
                return True, self._format_vault_v2(int(chain_id), vault)
            if v in ("v1", "1", "vault"):
                vault = await MORPHO_CLIENT.get_vault_by_address(
                    address=addr, chain_id=int(chain_id)
                )
                return True, self._format_vault_v1(int(chain_id), vault)

            # Auto-detect: try v2 first, then v1.
            try:
                vault = await MORPHO_CLIENT.get_vault_v2_by_address(
                    address=addr, chain_id=int(chain_id)
                )
                return True, self._format_vault_v2(int(chain_id), vault)
            except Exception:
                vault = await MORPHO_CLIENT.get_vault_by_address(
                    address=addr, chain_id=int(chain_id)
                )
                return True, self._format_vault_v1(int(chain_id), vault)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

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

    async def get_full_user_state(
        self,
        *,
        account: str,
        include_zero_positions: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        """Query all Morpho chains and return merged positions."""
        acct = to_checksum_address(account)

        try:
            positions = await MORPHO_CLIENT.get_all_market_positions(
                user_address=acct, chain_id=None
            )

            filtered = self._filter_positions(positions, include_zero_positions)

            return (
                True,
                {
                    "protocol": "morpho",
                    "account": acct,
                    "positions": filtered,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_full_user_state_per_chain(
        self,
        *,
        account: str,
        chain_id: int = CHAIN_ID_BASE,
        include_zero_positions: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        acct = to_checksum_address(account)

        try:
            positions = await MORPHO_CLIENT.get_all_market_positions(
                user_address=acct, chain_id=int(chain_id)
            )

            filtered = self._filter_positions(positions, include_zero_positions)
            for p in filtered:
                if isinstance(p, dict) and p.get("chainId") is None:
                    p["chainId"] = int(chain_id)

            return (
                True,
                {
                    "protocol": "morpho",
                    "chainId": int(chain_id),
                    "account": acct,
                    "positions": filtered,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    @staticmethod
    def _filter_positions(
        positions: list[dict[str, Any]], include_zero_positions: bool
    ) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        for p in positions:
            market = p.get("market") or {}
            state = p.get("state") or {}
            try:
                supply_shares = int(state.get("supplyShares") or 0)
                borrow_shares = int(state.get("borrowShares") or 0)
                collateral = int(state.get("collateral") or 0)
            except (TypeError, ValueError):
                supply_shares = borrow_shares = collateral = 0

            if not include_zero_positions and not (
                supply_shares > 0 or borrow_shares > 0 or collateral > 0
            ):
                continue

            market_state = market.get("state") or {}
            rewards = market_state.get("rewards") or []
            reward_supply_apr = 0.0
            reward_borrow_apr = 0.0
            for r in rewards if isinstance(rewards, list) else []:
                if not isinstance(r, dict):
                    continue
                try:
                    reward_supply_apr += float(r.get("supplyApr") or 0.0)
                except (TypeError, ValueError):
                    pass
                try:
                    reward_borrow_apr += float(r.get("borrowApr") or 0.0)
                except (TypeError, ValueError):
                    pass

            chain_id: int | None = None
            if isinstance(market, dict):
                try:
                    chain_raw = (
                        (market.get("morphoBlue") or {}).get("chain") or {}
                    ).get("id")
                    chain_id = int(chain_raw) if chain_raw is not None else None
                except (TypeError, ValueError):
                    chain_id = None

            entry: dict[str, Any] = {
                "chainId": chain_id,
                "marketUniqueKey": market.get("uniqueKey"),
                "healthFactor": p.get("healthFactor"),
                "market": market,
                "state": state,
                "supply_apy": market_state.get("supplyApy"),
                "net_supply_apy": market_state.get("netSupplyApy"),
                "borrow_apy": market_state.get("borrowApy"),
                "net_borrow_apy": market_state.get("netBorrowApy"),
                "reward_supply_apr": reward_supply_apr,
                "reward_borrow_apr": reward_borrow_apr,
            }
            filtered.append(entry)
        return filtered

    async def get_claimable_rewards(
        self,
        *,
        chain_id: int,
        account: str | None = None,
        include_merkl: bool = True,
        include_urd: bool = True,
        trusted: bool = True,
        claimable_only: bool = True,
    ) -> tuple[bool, dict[str, Any] | str]:
        acct = to_checksum_address(account) if account else self.wallet_address
        if not acct:
            return False, "strategy wallet address not configured"

        try:
            out: dict[str, Any] = {
                "protocol": "morpho",
                "chainId": int(chain_id),
                "account": acct,
            }

            if include_merkl:
                merkl = await MERKL_CLIENT.get_user_rewards(
                    address=acct,
                    chain_ids=[int(chain_id)],
                    breakdown_page=0,
                    claimable_only=bool(claimable_only),
                    reward_type="TOKEN",
                )
                rewards: list[dict[str, Any]] = []
                for item in merkl:
                    chain = item.get("chain") or {}
                    try:
                        if int(chain.get("id")) != int(chain_id):
                            continue
                    except (TypeError, ValueError):
                        continue
                    rewards = [
                        r for r in (item.get("rewards") or []) if isinstance(r, dict)
                    ]
                    break
                out["merkl"] = {
                    "distributor": MERKL_DISTRIBUTOR_ADDRESS,
                    "rewards": rewards,
                }

            if include_urd:
                dists = await MORPHO_REWARDS_CLIENT.get_user_distributions(
                    user=acct,
                    chain_id=int(chain_id),
                    trusted=bool(trusted),
                )
                out["urd"] = {"distributions": dists}

            return True, out
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def claim_merkl_rewards(
        self,
        *,
        chain_id: int,
        account: str | None = None,
        min_claim_amount: int = 0,
        claimable_only: bool = True,
    ) -> tuple[bool, str | None]:
        acct = to_checksum_address(account) if account else self.wallet_address
        if not acct:
            return False, "strategy wallet address not configured"

        try:
            ok, data = await self.get_claimable_rewards(
                chain_id=int(chain_id),
                account=acct,
                include_merkl=True,
                include_urd=False,
                claimable_only=bool(claimable_only),
            )
            if not ok or not isinstance(data, dict):
                return False, str(data)

            rewards = (
                ((data.get("merkl") or {}).get("rewards") or [])
                if isinstance(data, dict)
                else []
            )
            rewards = [r for r in rewards if isinstance(r, dict)]
            if not rewards:
                return True, None

            users: list[str] = []
            tokens: list[str] = []
            amounts: list[int] = []
            proofs: list[list[str]] = []
            for r in rewards:
                token = (r.get("token") or {}).get("address")
                amt_raw = r.get("amount")
                prf = r.get("proofs") or []
                if not token or not prf:
                    continue
                try:
                    amt = int(amt_raw)
                except (TypeError, ValueError):
                    continue
                if amt <= int(min_claim_amount):
                    continue
                users.append(acct)
                tokens.append(to_checksum_address(str(token)))
                amounts.append(int(amt))
                proofs.append([str(p) for p in prf if isinstance(p, str)])

            if not users:
                return True, None

            tx = await encode_call(
                target=MERKL_DISTRIBUTOR_ADDRESS,
                abi=MERKL_DISTRIBUTOR_ABI,
                fn_name="claim",
                args=[users, tokens, amounts, proofs],
                from_address=acct,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def claim_urd_rewards(
        self,
        *,
        chain_id: int,
        account: str | None = None,
        trusted: bool = True,
        max_claims: int = 50,
        min_claimable: int = 0,
    ) -> tuple[bool, list[str] | str]:
        acct = to_checksum_address(account) if account else self.wallet_address
        if not acct:
            return False, "strategy wallet address not configured"

        try:
            dists = await MORPHO_REWARDS_CLIENT.get_user_distributions(
                user=acct,
                chain_id=int(chain_id),
                trusted=bool(trusted),
            )

            tx_hashes: list[str] = []
            for d in dists:
                if len(tx_hashes) >= int(max_claims):
                    break
                if not isinstance(d, dict):
                    continue
                claimable_raw = d.get("claimable")
                tx_data = d.get("txData")
                distributor = (d.get("distributor") or {}).get("address")
                if not (tx_data and distributor and claimable_raw is not None):
                    continue
                try:
                    claimable = int(claimable_raw)
                except (TypeError, ValueError):
                    continue
                if claimable <= int(min_claimable):
                    continue

                tx = {
                    "chainId": int(chain_id),
                    "from": acct,
                    "to": to_checksum_address(str(distributor)),
                    "data": str(tx_data),
                    "value": 0,
                }
                txn_hash = await send_transaction(tx, self.sign_callback)
                tx_hashes.append(str(txn_hash))

            return True, tx_hashes
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def claim_rewards(
        self,
        *,
        chain_id: int,
        account: str | None = None,
        claim_merkl: bool = True,
        claim_urd: bool = True,
        trusted: bool = True,
    ) -> tuple[bool, dict[str, Any] | str]:
        acct = to_checksum_address(account) if account else self.wallet_address
        if not acct:
            return False, "strategy wallet address not configured"

        try:
            out: dict[str, Any] = {"chainId": int(chain_id), "account": acct}

            if claim_merkl:
                ok, tx = await self.claim_merkl_rewards(
                    chain_id=int(chain_id),
                    account=acct,
                )
                if not ok:
                    return False, str(tx)
                out["merkl_tx"] = tx

            if claim_urd:
                ok, txs = await self.claim_urd_rewards(
                    chain_id=int(chain_id),
                    account=acct,
                    trusted=bool(trusted),
                )
                if not ok:
                    return False, str(txs)
                out["urd_txs"] = txs

            return True, out
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_pos(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
        account: str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        acct = to_checksum_address(account) if account else self.wallet_address
        if not acct:
            return False, "strategy wallet address not configured"

        try:
            pos = await MORPHO_CLIENT.get_market_position(
                user_address=acct,
                market_unique_key=str(market_unique_key),
                chain_id=int(chain_id),
            )
            return True, pos
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_user_position(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
        account: str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        return await self.get_pos(
            chain_id=int(chain_id),
            market_unique_key=str(market_unique_key),
            account=account,
        )

    async def _risk_metrics(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
        account: str | None = None,
    ) -> dict[str, Any]:
        acct = to_checksum_address(account) if account else self.wallet_address
        if not acct:
            raise ValueError("strategy wallet address not configured")

        pos = await MORPHO_CLIENT.get_market_position(
            user_address=acct,
            market_unique_key=str(market_unique_key),
            chain_id=int(chain_id),
        )
        market = await self._get_market(
            chain_id=int(chain_id), unique_key=str(market_unique_key)
        )
        state = (market.get("state") or {}) if isinstance(market, dict) else {}

        try:
            lltv = int(market.get("lltv") or 0)
        except (TypeError, ValueError):
            lltv = 0
        try:
            price = int(state.get("price") or 0)
        except (TypeError, ValueError):
            price = 0
        try:
            liquidity_assets = int(state.get("liquidityAssets") or 0)
        except (TypeError, ValueError):
            liquidity_assets = 0

        pos_state = pos.get("state") or {}
        try:
            collateral_assets = int(pos_state.get("collateral") or 0)
        except (TypeError, ValueError):
            collateral_assets = 0
        try:
            borrow_assets = int(pos_state.get("borrowAssets") or 0)
        except (TypeError, ValueError):
            borrow_assets = 0

        collateral_value_loan_assets = 0
        if collateral_assets > 0 and price > 0:
            collateral_value_loan_assets = (
                collateral_assets * price
            ) // ORACLE_PRICE_SCALE

        borrow_limit = 0
        if collateral_value_loan_assets > 0 and lltv > 0:
            borrow_limit = (collateral_value_loan_assets * lltv) // MANTISSA

        max_borrow = borrow_limit - borrow_assets
        if max_borrow < 0:
            max_borrow = 0
        if liquidity_assets > 0 and max_borrow > liquidity_assets:
            max_borrow = liquidity_assets

        if borrow_assets <= 0:
            max_withdraw_collateral = collateral_assets
        elif price <= 0 or lltv <= 0:
            max_withdraw_collateral = 0
        else:
            # collateral_min = ceil(borrow_assets * ORACLE_PRICE_SCALE * MANTISSA / (price * lltv))
            denom = price * lltv
            num = borrow_assets * ORACLE_PRICE_SCALE * MANTISSA
            collateral_min = (num + denom - 1) // denom
            max_withdraw_collateral = collateral_assets - collateral_min
            if max_withdraw_collateral < 0:
                max_withdraw_collateral = 0

        ltv = None
        if collateral_value_loan_assets > 0:
            ltv = borrow_assets / float(collateral_value_loan_assets)

        hf = pos.get("healthFactor")
        return {
            "account": acct,
            "marketUniqueKey": str(market_unique_key),
            "lltv": lltv,
            "price": state.get("price"),
            "collateral_assets": collateral_assets,
            "borrow_assets": borrow_assets,
            "collateral_value_loan_assets": collateral_value_loan_assets,
            "borrow_limit_assets": borrow_limit,
            "max_borrow_assets": max_borrow,
            "max_withdraw_collateral_assets": max_withdraw_collateral,
            "healthFactor": hf,
            "priceVariationToLiquidationPrice": pos.get(
                "priceVariationToLiquidationPrice"
            ),
            "ltv": ltv,
            "liquidity_assets": liquidity_assets,
        }

    async def get_health(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
        account: str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            metrics = await self._risk_metrics(
                chain_id=int(chain_id),
                market_unique_key=str(market_unique_key),
                account=account,
            )
            return True, metrics
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def max_borrow(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
        account: str | None = None,
    ) -> tuple[bool, int | str]:
        try:
            metrics = await self._risk_metrics(
                chain_id=int(chain_id),
                market_unique_key=str(market_unique_key),
                account=account,
            )
            return True, int(metrics.get("max_borrow_assets") or 0)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def max_withdraw_collateral(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
        account: str | None = None,
    ) -> tuple[bool, int | str]:
        try:
            metrics = await self._risk_metrics(
                chain_id=int(chain_id),
                market_unique_key=str(market_unique_key),
                account=account,
            )
            return True, int(metrics.get("max_withdraw_collateral_assets") or 0)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def supply_collateral(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
        qty: int,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        qty = int(qty)
        if qty <= 0:
            return False, "qty must be positive"

        try:
            morpho = await self._morpho_address(chain_id=int(chain_id))
            market = await self._get_market(
                chain_id=int(chain_id), unique_key=str(market_unique_key)
            )
            market_params = self._market_params_from_market(market)
            collateral_token = market_params[1]

            approved = await ensure_allowance(
                token_address=collateral_token,
                owner=strategy,
                spender=morpho,
                amount=qty,
                chain_id=int(chain_id),
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=morpho,
                abi=MORPHO_BLUE_ABI,
                fn_name="supplyCollateral",
                args=[market_params, qty, strategy, b""],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def withdraw_collateral(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
        qty: int,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        qty = int(qty)
        if qty <= 0:
            return False, "qty must be positive"

        try:
            morpho = await self._morpho_address(chain_id=int(chain_id))
            market = await self._get_market(
                chain_id=int(chain_id), unique_key=str(market_unique_key)
            )
            market_params = self._market_params_from_market(market)

            tx = await encode_call(
                target=morpho,
                abi=MORPHO_BLUE_ABI,
                fn_name="withdrawCollateral",
                args=[market_params, qty, strategy, strategy],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def lend(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
        qty: int,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        qty = int(qty)
        if qty <= 0:
            return False, "qty must be positive"

        try:
            morpho = await self._morpho_address(chain_id=int(chain_id))
            market = await self._get_market(
                chain_id=int(chain_id), unique_key=str(market_unique_key)
            )
            market_params = self._market_params_from_market(market)
            loan_token = market_params[0]

            approved = await ensure_allowance(
                token_address=loan_token,
                owner=strategy,
                spender=morpho,
                amount=qty,
                chain_id=int(chain_id),
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=morpho,
                abi=MORPHO_BLUE_ABI,
                fn_name="supply",
                args=[market_params, qty, 0, strategy, b""],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def unlend(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
        qty: int,
        withdraw_full: bool = False,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        qty = int(qty)
        if qty <= 0 and not withdraw_full:
            return False, "qty must be positive"

        try:
            morpho = await self._morpho_address(chain_id=int(chain_id))
            market = await self._get_market(
                chain_id=int(chain_id), unique_key=str(market_unique_key)
            )
            market_params = self._market_params_from_market(market)

            withdraw_assets = qty
            withdraw_shares = 0
            if withdraw_full:
                supply_shares, _borrow_shares, _coll = await self._position(
                    chain_id=int(chain_id),
                    market_unique_key=str(market_unique_key),
                    account=strategy,
                )
                if supply_shares <= 0:
                    return True, None
                withdraw_assets = 0
                withdraw_shares = int(supply_shares)

            tx = await encode_call(
                target=morpho,
                abi=MORPHO_BLUE_ABI,
                fn_name="withdraw",
                args=[
                    market_params,
                    int(withdraw_assets),
                    int(withdraw_shares),
                    strategy,
                    strategy,
                ],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def borrow(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
        qty: int,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        qty = int(qty)
        if qty <= 0:
            return False, "qty must be positive"

        try:
            morpho = await self._morpho_address(chain_id=int(chain_id))
            market = await self._get_market(
                chain_id=int(chain_id), unique_key=str(market_unique_key)
            )
            market_params = self._market_params_from_market(market)

            tx = await encode_call(
                target=morpho,
                abi=MORPHO_BLUE_ABI,
                fn_name="borrow",
                args=[market_params, qty, 0, strategy, strategy],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_public_allocator_fee(
        self,
        *,
        chain_id: int,
        vault: str,
        public_allocator: str | None = None,
    ) -> tuple[bool, int | str]:
        try:
            allocator = (
                to_checksum_address(public_allocator)
                if public_allocator
                else await self._public_allocator_address(chain_id=int(chain_id))
            )
            async with web3_utils.web3_from_chain_id(int(chain_id)) as web3:
                contract = web3.eth.contract(
                    address=allocator, abi=PUBLIC_ALLOCATOR_ABI
                )
                fee = await contract.functions.fee(
                    to_checksum_address(str(vault))
                ).call(block_identifier="pending")
                return True, int(fee or 0)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def public_allocator_reallocate_to(
        self,
        *,
        chain_id: int,
        vault: str,
        supply_market_unique_key: str,
        withdrawals: list[dict[str, Any]],
        public_allocator: str | None = None,
        value: int | None = None,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"

        try:
            allocator = (
                to_checksum_address(public_allocator)
                if public_allocator
                else await self._public_allocator_address(chain_id=int(chain_id))
            )

            supply_market = await self._get_market(
                chain_id=int(chain_id), unique_key=str(supply_market_unique_key)
            )
            supply_market_params = self._market_params_from_market(supply_market)

            withdrawal_tuples: list[tuple[MarketParamsTuple, int]] = []
            for w in withdrawals:
                if not isinstance(w, dict):
                    continue
                wk = w.get("market_unique_key") or w.get("uniqueKey")
                amt = w.get("amount") or w.get("qty")
                if not wk or amt is None:
                    continue
                try:
                    amount_int = int(amt)
                except (TypeError, ValueError):
                    continue
                if amount_int <= 0:
                    continue
                wm = await self._get_market(chain_id=int(chain_id), unique_key=str(wk))
                wm_params = self._market_params_from_market(wm)
                withdrawal_tuples.append((wm_params, int(amount_int)))

            if not withdrawal_tuples:
                return False, "No valid withdrawals provided"

            fee_value = 0
            if value is None:
                ok, fee_or_err = await self.get_public_allocator_fee(
                    chain_id=int(chain_id), vault=str(vault), public_allocator=allocator
                )
                if ok:
                    fee_value = int(fee_or_err or 0)
                else:
                    fee_value = 0
            else:
                fee_value = int(value)

            tx = await encode_call(
                target=allocator,
                abi=PUBLIC_ALLOCATOR_ABI,
                fn_name="reallocateTo",
                args=[
                    to_checksum_address(str(vault)),
                    withdrawal_tuples,
                    supply_market_params,
                ],
                from_address=strategy,
                chain_id=int(chain_id),
                value=int(fee_value),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def bundler_multicall(
        self,
        *,
        chain_id: int,
        calls: list[str] | list[dict[str, Any]],
        bundler_address: str | None = None,
        value: int = 0,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"

        bundler = (
            to_checksum_address(bundler_address)
            if bundler_address
            else self.bundler_address
        )
        if not bundler:
            return False, "bundler address not configured"

        try:
            datas: list[str] = []
            if calls and isinstance(calls[0], dict):
                for c in calls:  # type: ignore[assignment]
                    if not isinstance(c, dict):
                        continue
                    fn_name = c.get("fn_name") or c.get("fn") or c.get("name")
                    args = c.get("args") or []
                    if not fn_name:
                        continue
                    data = await self._encode_data(
                        chain_id=int(chain_id),
                        target=bundler,
                        abi=BUNDLER3_ABI,
                        fn_name=str(fn_name),
                        args=list(args) if isinstance(args, list) else [],
                        from_address=strategy,
                    )
                    datas.append(data)
            else:
                datas = [str(d) for d in calls if isinstance(d, str)]

            if not datas:
                return False, "No calls provided"

            tx = await encode_call(
                target=bundler,
                abi=BUNDLER3_ABI,
                fn_name="multicall",
                args=[datas],
                from_address=strategy,
                chain_id=int(chain_id),
                value=int(value),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def borrow_with_jit_liquidity(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
        qty: int,
        atomic: bool = True,
        bundler_address: str | None = None,
    ) -> tuple[bool, Any]:
        """
        Borrow with optional Public Allocator JIT reallocation when market liquidity is insufficient.
        If `atomic=True` and a bundler address is configured/provided, attempts to bundle reallocate+borrow.
        """
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        qty = int(qty)
        if qty <= 0:
            return False, "qty must be positive"

        try:
            market = await self._get_market(
                chain_id=int(chain_id), unique_key=str(market_unique_key)
            )
            state = market.get("state") or {}
            try:
                liquidity_assets = int(state.get("liquidityAssets") or 0)
            except (TypeError, ValueError):
                liquidity_assets = 0

            if liquidity_assets >= qty:
                return await self.borrow(
                    chain_id=int(chain_id),
                    market_unique_key=str(market_unique_key),
                    qty=int(qty),
                )

            shared = market.get("publicAllocatorSharedLiquidity") or []
            shared_items = [s for s in shared if isinstance(s, dict)]
            if not shared_items:
                return (
                    False,
                    "No public allocator shared liquidity available for this market",
                )

            needed = qty - liquidity_assets

            # group by (publicAllocator, vault)
            groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for item in shared_items:
                vault = (item.get("vault") or {}).get("address")
                allocator = (item.get("publicAllocator") or {}).get("address")
                if not (vault and allocator):
                    continue
                key = (str(allocator).lower(), str(vault).lower())
                groups.setdefault(key, []).append(item)

            if not groups:
                return False, "No valid allocator/vault groups found"

            best_key = None
            best_total = 0
            for k, items in groups.items():
                total = 0
                for it in items:
                    try:
                        total += int(it.get("assets") or 0)
                    except (TypeError, ValueError):
                        continue
                if total > best_total:
                    best_total = total
                    best_key = k

            if best_key is None or best_total <= 0:
                return False, "No reallocatable liquidity found"

            allocator_addr_lc, vault_addr_lc = best_key
            allocator_addr = to_checksum_address(allocator_addr_lc)
            vault_addr = to_checksum_address(vault_addr_lc)

            if best_total < needed:
                return (
                    False,
                    f"Insufficient reallocatable liquidity: needed={needed} available={best_total}",
                )

            remaining = needed
            withdrawals: list[tuple[MarketParamsTuple, int]] = []
            for it in sorted(
                groups[best_key],
                key=lambda x: int(x.get("assets") or 0),
                reverse=True,
            ):
                if remaining <= 0:
                    break
                withdraw_market = (it.get("withdrawMarket") or {}).get("uniqueKey")
                if not withdraw_market:
                    continue
                try:
                    available = int(it.get("assets") or 0)
                except (TypeError, ValueError):
                    continue
                if available <= 0:
                    continue
                amount = available if available <= remaining else remaining
                wm = await self._get_market(
                    chain_id=int(chain_id), unique_key=str(withdraw_market)
                )
                wm_params = self._market_params_from_market(wm)
                withdrawals.append((wm_params, int(amount)))
                remaining -= amount

            if remaining > 0:
                return False, "Failed to build sufficient withdrawals"

            supply_market_params = self._market_params_from_market(market)

            ok, fee_or_err = await self.get_public_allocator_fee(
                chain_id=int(chain_id),
                vault=vault_addr,
                public_allocator=allocator_addr,
            )
            fee_value = int(fee_or_err or 0) if ok else 0

            if atomic and (bundler_address or self.bundler_address):
                bundler = (
                    to_checksum_address(bundler_address)
                    if bundler_address
                    else to_checksum_address(self.bundler_address)
                )
                call1 = await self._encode_data(
                    chain_id=int(chain_id),
                    target=bundler,
                    abi=BUNDLER3_ABI,
                    fn_name="reallocateTo",
                    args=[
                        allocator_addr,
                        vault_addr,
                        int(fee_value),
                        withdrawals,
                        supply_market_params,
                    ],
                    from_address=strategy,
                )
                call2 = await self._encode_data(
                    chain_id=int(chain_id),
                    target=bundler,
                    abi=BUNDLER3_ABI,
                    fn_name="morphoBorrow",
                    args=[
                        supply_market_params,
                        int(qty),
                        0,
                        MAX_UINT256,
                        strategy,
                    ],
                    from_address=strategy,
                )
                return await self.bundler_multicall(
                    chain_id=int(chain_id),
                    bundler_address=bundler,
                    calls=[call1, call2],
                    value=int(fee_value),
                )

            # Non-atomic fallback: reallocate then borrow.
            tx = await encode_call(
                target=allocator_addr,
                abi=PUBLIC_ALLOCATOR_ABI,
                fn_name="reallocateTo",
                args=[vault_addr, withdrawals, supply_market_params],
                from_address=strategy,
                chain_id=int(chain_id),
                value=int(fee_value),
            )
            realloc_hash = await send_transaction(tx, self.sign_callback)

            ok2, borrow_tx = await self.borrow(
                chain_id=int(chain_id),
                market_unique_key=str(market_unique_key),
                qty=int(qty),
            )
            if not ok2:
                return False, str(borrow_tx)

            return True, {"reallocate_tx": realloc_hash, "borrow_tx": borrow_tx}
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def withdraw_full(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
    ) -> tuple[bool, Any]:
        return await self.unlend(
            chain_id=int(chain_id),
            market_unique_key=str(market_unique_key),
            qty=0,
            withdraw_full=True,
        )

    async def _position(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
        account: str,
    ) -> tuple[int, int, int]:
        morpho = await self._morpho_address(chain_id=int(chain_id))
        async with web3_utils.web3_from_chain_id(int(chain_id)) as web3:
            contract = web3.eth.contract(address=morpho, abi=MORPHO_BLUE_ABI)
            (
                supply_shares,
                borrow_shares,
                collateral,
            ) = await contract.functions.position(
                market_unique_key, to_checksum_address(account)
            ).call(block_identifier="pending")
        return (int(supply_shares), int(borrow_shares), int(collateral))

    async def repay_full(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
    ) -> tuple[bool, Any]:
        return await self.repay(
            chain_id=int(chain_id),
            market_unique_key=str(market_unique_key),
            qty=0,
            repay_full=True,
        )

    async def repay(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
        qty: int,
        repay_full: bool = False,
    ) -> tuple[bool, Any]:
        strategy = self.wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        qty = int(qty)
        if qty <= 0 and not repay_full:
            return False, "qty must be positive"

        try:
            morpho = await self._morpho_address(chain_id=int(chain_id))
            market = await self._get_market(
                chain_id=int(chain_id), unique_key=str(market_unique_key)
            )
            market_params = self._market_params_from_market(market)
            loan_token = market_params[0]

            repay_assets = qty
            repay_shares = 0
            allowance_target = qty

            if repay_full:
                _supply_shares, borrow_shares, _coll = await self._position(
                    chain_id=int(chain_id),
                    market_unique_key=str(market_unique_key),
                    account=strategy,
                )
                if borrow_shares <= 0:
                    return True, None
                repay_assets = 0
                repay_shares = int(borrow_shares)
                allowance_target = MAX_UINT256

            approved = await ensure_allowance(
                token_address=loan_token,
                owner=strategy,
                spender=morpho,
                amount=allowance_target,
                chain_id=int(chain_id),
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=morpho,
                abi=MORPHO_BLUE_ABI,
                fn_name="repay",
                args=[
                    market_params,
                    int(repay_assets),
                    int(repay_shares),
                    strategy,
                    b"",
                ],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
