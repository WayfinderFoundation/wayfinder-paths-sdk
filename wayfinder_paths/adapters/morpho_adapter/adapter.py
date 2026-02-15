from __future__ import annotations

from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.clients.MorphoClient import MORPHO_CLIENT
from wayfinder_paths.core.constants.base import MAX_UINT256
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.morpho_abi import MORPHO_BLUE_ABI
from wayfinder_paths.core.constants.morpho_contracts import MORPHO_BY_CHAIN
from wayfinder_paths.core.utils.tokens import ensure_allowance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

MarketParamsTuple = tuple[str, str, str, str, int]


class MorphoAdapter(BaseAdapter):
    adapter_type = "MORPHO"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        strategy_wallet_signing_callback=None,
    ) -> None:
        super().__init__("morpho_adapter", config or {})
        self.strategy_wallet_signing_callback = strategy_wallet_signing_callback

        cfg = config or {}
        strategy_addr = (cfg.get("strategy_wallet") or {}).get("address")
        self.strategy_wallet_address: str | None = (
            to_checksum_address(strategy_addr) if strategy_addr else None
        )

        self._market_cache: dict[tuple[int, str], dict[str, Any]] = {}

    async def _morpho_address(self, *, chain_id: int) -> str:
        entry = MORPHO_BY_CHAIN.get(int(chain_id))
        if entry and entry.get("morpho"):
            return to_checksum_address(str(entry["morpho"]))

        # Fallback to the Morpho API if constants are missing/out-of-date.
        addr = await MORPHO_CLIENT.get_morpho_address(chain_id=int(chain_id))
        return to_checksum_address(str(addr))

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

        if not (loan_addr and collateral_addr and oracle_addr and irm_addr and lltv_raw):
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
    def _format_market(chain_id: int, morpho: str, market: dict[str, Any]) -> dict[str, Any]:
        loan = market.get("loanAsset") or {}
        collateral = market.get("collateralAsset") or {}
        oracle = market.get("oracle") or {}
        state = market.get("state") or {}

        out: dict[str, Any] = {
            "uniqueKey": market.get("uniqueKey"),
            "chainId": int(chain_id),
            "morpho": morpho,
            "listed": bool(market.get("listed")),
            "lltv": int(market.get("lltv") or 0),
            "irm": market.get("irmAddress"),
            "oracle": oracle.get("address"),
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
                "utilization": state.get("utilization"),
                "apy_at_target": state.get("apyAtTarget"),
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

    async def get_full_user_state(
        self,
        *,
        account: str | None = None,
        chain_id: int = CHAIN_ID_BASE,
        include_zero_positions: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        acct = to_checksum_address(account) if account else self.strategy_wallet_address
        if not acct:
            return False, "strategy wallet address not configured"

        try:
            positions = await MORPHO_CLIENT.get_all_market_positions(
                user_address=acct, chain_id=int(chain_id)
            )

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

                filtered.append(
                    {
                        "marketUniqueKey": market.get("uniqueKey"),
                        "healthFactor": p.get("healthFactor"),
                        "market": market,
                        "state": state,
                    }
                )

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

    async def get_pos(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
        account: str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        acct = to_checksum_address(account) if account else self.strategy_wallet_address
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

    async def supply_collateral(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
        qty: int,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
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
                signing_callback=self.strategy_wallet_signing_callback,
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
            txn_hash = await send_transaction(tx, self.strategy_wallet_signing_callback)
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
        strategy = self.strategy_wallet_address
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
            txn_hash = await send_transaction(tx, self.strategy_wallet_signing_callback)
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
        strategy = self.strategy_wallet_address
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
                signing_callback=self.strategy_wallet_signing_callback,
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
            txn_hash = await send_transaction(tx, self.strategy_wallet_signing_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def unlend(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
        qty: int,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
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
                fn_name="withdraw",
                args=[market_params, qty, 0, strategy, strategy],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.strategy_wallet_signing_callback)
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
        strategy = self.strategy_wallet_address
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
            txn_hash = await send_transaction(tx, self.strategy_wallet_signing_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def _position(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
        account: str,
    ) -> tuple[int, int, int]:
        morpho = await self._morpho_address(chain_id=int(chain_id))
        async with web3_from_chain_id(int(chain_id)) as web3:
            contract = web3.eth.contract(address=morpho, abi=MORPHO_BLUE_ABI)
            supply_shares, borrow_shares, collateral = await contract.functions.position(
                market_unique_key, to_checksum_address(account)
            ).call(block_identifier="pending")
        return (int(supply_shares), int(borrow_shares), int(collateral))

    async def repay(
        self,
        *,
        chain_id: int,
        market_unique_key: str,
        qty: int,
        repay_full: bool = False,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
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
                signing_callback=self.strategy_wallet_signing_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=morpho,
                abi=MORPHO_BLUE_ABI,
                fn_name="repay",
                args=[market_params, int(repay_assets), int(repay_shares), strategy, b""],
                from_address=strategy,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.strategy_wallet_signing_callback)
            return True, txn_hash
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
