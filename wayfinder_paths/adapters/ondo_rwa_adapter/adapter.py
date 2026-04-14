from __future__ import annotations

import asyncio
from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter, require_wallet
from wayfinder_paths.core.constants.base import MAX_UINT256
from wayfinder_paths.core.constants.chains import (
    CHAIN_ID_ETHEREUM,
    CHAIN_ID_MANTLE,
)
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
from wayfinder_paths.core.constants.ondo_rwa_abi import (
    ONDO_ID_REGISTRY_ABI,
    ONDO_PRICE_ORACLE_ABI,
    OUSG_INSTANT_MANAGER_ABI,
    ROUSG_WRAPPER_ABI,
    RUSDY_WRAPPER_ABI,
    USDY_INSTANT_MANAGER_ABI,
)
from wayfinder_paths.core.constants.ondo_rwa_contracts import (
    ONDO_PRODUCT_DEFAULT_CHAIN,
    ONDO_RWA_MARKETS,
    ONDO_RWA_PROTOCOL,
    ONDO_SHARES_MULTIPLIER,
)
from wayfinder_paths.core.utils.multicall import (
    Call,
    read_only_calls_multicall_or_gather,
)
from wayfinder_paths.core.utils.tokens import ensure_allowance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

MarketKey = tuple[str, int]

_ZERO_BYTES32 = "0x" + ("00" * 32)


def _normalize_product(product: str) -> str:
    normalized = str(product).strip().lower()
    if not normalized:
        raise ValueError("product is required")
    return normalized


def _notes_list(market: dict[str, Any]) -> list[str]:
    notes = market.get("notes") or []
    return [str(note) for note in notes]


class OndoRwaAdapter(BaseAdapter):
    adapter_type: str = "ONDO_RWA"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        sign_callback: Any | None = None,
        wallet_address: str | None = None,
    ) -> None:
        super().__init__("ondo_rwa_adapter", config or {})
        self.sign_callback = sign_callback
        self.wallet_address: str | None = (
            to_checksum_address(wallet_address) if wallet_address else None
        )

    def _market_key(
        self,
        *,
        product: str,
        chain_id: int | None = None,
    ) -> MarketKey:
        normalized_product = _normalize_product(product)
        if normalized_product not in ONDO_PRODUCT_DEFAULT_CHAIN:
            raise ValueError(f"Unsupported Ondo product: {normalized_product}")
        resolved_chain_id = (
            int(chain_id)
            if chain_id is not None
            else int(ONDO_PRODUCT_DEFAULT_CHAIN[normalized_product])
        )
        key = (normalized_product, resolved_chain_id)
        if key not in ONDO_RWA_MARKETS:
            raise ValueError(
                f"Unsupported Ondo market for product={normalized_product} chain_id={resolved_chain_id}"
            )
        return key

    def _market(
        self,
        *,
        product: str,
        chain_id: int | None = None,
    ) -> dict[str, Any]:
        return ONDO_RWA_MARKETS[self._market_key(product=product, chain_id=chain_id)]

    def _find_market_by_token(
        self,
        *,
        token_address: str,
        chain_id: int | None = None,
    ) -> dict[str, Any]:
        checksum_token = to_checksum_address(token_address)
        exact_candidates: list[dict[str, Any]] = []
        paired_candidates: list[dict[str, Any]] = []

        for market in ONDO_RWA_MARKETS.values():
            if chain_id is not None and int(market["chain_id"]) != int(chain_id):
                continue
            token_match = checksum_token.lower() == str(market.get("token", "")).lower()
            paired_match = checksum_token.lower() in {
                str(market.get("underlying_token", "")).lower(),
                str(market.get("rebasing_token", "")).lower(),
            }
            if token_match:
                exact_candidates.append(market)
            elif paired_match:
                paired_candidates.append(market)

        candidates = exact_candidates or paired_candidates

        if not candidates:
            raise ValueError(f"Unknown Ondo token address: {checksum_token}")
        if len(candidates) > 1:
            raise ValueError(
                f"Ambiguous Ondo token address for chain inference: {checksum_token}"
            )
        return candidates[0]

    def _manager_abi(self, market: dict[str, Any]) -> list[dict[str, Any]]:
        if str(market["family"]) == "ousg":
            return OUSG_INSTANT_MANAGER_ABI
        return USDY_INSTANT_MANAGER_ABI

    def _wrapper_abi(self, market: dict[str, Any]) -> list[dict[str, Any]]:
        if str(market["family"]) == "ousg":
            return ROUSG_WRAPPER_ABI
        return RUSDY_WRAPPER_ABI

    def _subscribe_fn_name(self, product: str) -> str:
        if product == "ousg":
            return "subscribe"
        if product == "rousg":
            return "subscribeRebasingOUSG"
        if product == "usdy":
            return "subscribe"
        if product == "rusdy":
            return "subscribeRebasingUSDY"
        raise ValueError(f"Unsupported subscribe product: {product}")

    def _redeem_fn_name(self, product: str) -> str:
        if product == "ousg":
            return "redeem"
        if product == "rousg":
            return "redeemRebasingOUSG"
        if product == "usdy":
            return "redeem"
        if product == "rusdy":
            return "redeemRebasingUSDY"
        raise ValueError(f"Unsupported redeem product: {product}")

    def _wrap_pair(
        self, market: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        product = str(market["product"])
        chain_id = int(market["chain_id"])

        if chain_id == CHAIN_ID_ETHEREUM and product in {"ousg", "rousg"}:
            return (
                ONDO_RWA_MARKETS[("ousg", CHAIN_ID_ETHEREUM)],
                ONDO_RWA_MARKETS[("rousg", CHAIN_ID_ETHEREUM)],
            )
        if chain_id == CHAIN_ID_ETHEREUM and product in {"usdy", "rusdy"}:
            return (
                ONDO_RWA_MARKETS[("usdy", CHAIN_ID_ETHEREUM)],
                ONDO_RWA_MARKETS[("rusdy", CHAIN_ID_ETHEREUM)],
            )
        if chain_id == CHAIN_ID_MANTLE and product in {"usdy", "musd"}:
            return (
                ONDO_RWA_MARKETS[("usdy", CHAIN_ID_MANTLE)],
                ONDO_RWA_MARKETS[("musd", CHAIN_ID_MANTLE)],
            )
        raise ValueError(
            f"Wrap/unwrap is not supported for product={product} chain_id={chain_id}"
        )

    def _family_name(self, product_or_family: str) -> str:
        normalized = _normalize_product(product_or_family)
        if normalized in {"ousg", "usdy"}:
            return normalized
        market = self._market(product=normalized)
        return str(market["family"])

    def _ensure_signer(self) -> tuple[bool, str | None]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        return True, None

    async def _preflight_transaction(
        self, transaction: dict[str, Any]
    ) -> tuple[bool, str | None]:
        chain_id = int(transaction["chainId"])
        async with web3_from_chain_id(chain_id) as web3:
            try:
                await web3.eth.call(transaction, block_identifier="pending")
                return True, None
            except Exception as exc:  # noqa: BLE001 - bubble up cleaned reason
                return False, self._format_revert_reason(exc)

    def _format_revert_reason(self, exc: Exception) -> str:
        raw = str(exc).strip()
        if not raw:
            return exc.__class__.__name__
        if "execution reverted:" in raw:
            return raw.split("execution reverted:", 1)[1].strip(" '()")
        if "execution reverted" in raw and "no data" not in raw and "'0x'" not in raw:
            return raw
        if "ContractCustomError" in raw:
            return raw
        return raw

    async def _fetch_supported_seed_tokens(
        self,
        market: dict[str, Any],
    ) -> list[dict[str, Any]]:
        manager = market.get("manager")
        stablecoins = market.get("stablecoins") or {}
        if not manager or not stablecoins:
            return []

        chain_id = int(market["chain_id"])
        async with web3_from_chain_id(chain_id) as web3:
            manager_contract = web3.eth.contract(
                address=to_checksum_address(str(manager)),
                abi=self._manager_abi(market),
            )
            token_rows = [
                {
                    "key": str(key),
                    "address": to_checksum_address(str(token_data["address"])),
                    "symbol": str(token_data.get("symbol") or key).upper(),
                    "decimals": int(token_data.get("decimals") or 0),
                }
                for key, token_data in stablecoins.items()
            ]

            try:
                supported_values = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=chain_id,
                    calls=[
                        Call(
                            manager_contract,
                            "acceptedSubscriptionTokens",
                            args=(row["address"],),
                            postprocess=bool,
                        )
                        for row in token_rows
                    ],
                    block_identifier="pending",
                )
            except Exception:

                async def _read_supported(token_address: str) -> bool | None:
                    try:
                        supported = (
                            await manager_contract.functions.acceptedSubscriptionTokens(
                                token_address
                            ).call(block_identifier="pending")
                        )
                        return bool(supported)
                    except Exception:
                        return None

                supported_values = await asyncio.gather(
                    *[_read_supported(str(row["address"])) for row in token_rows]
                )

            return [
                {
                    **row,
                    "supported": supported,
                }
                for row, supported in zip(token_rows, supported_values, strict=True)
            ]

    async def _read_market_metadata(
        self,
        market: dict[str, Any],
    ) -> dict[str, Any]:
        chain_id = int(market["chain_id"])
        token_address = to_checksum_address(str(market["token"]))
        subscription_tokens_task = asyncio.create_task(
            self._fetch_supported_seed_tokens(market)
        )
        base: dict[str, Any] = {
            "chain_id": chain_id,
            "chain_name": str(market["chain_name"]),
            "product": str(market["product"]),
            "family": str(market["family"]),
            "token_address": token_address,
            "underlying_or_pair_token": to_checksum_address(
                str(
                    market.get("underlying_token")
                    or market.get("rebasing_token")
                    or token_address
                )
            ),
            "manager_address": market.get("manager"),
            "oracle_address": market.get("oracle"),
            "symbol": str(market["product"]).upper(),
            "name": str(market["product"]).upper(),
            "decimals": 18,
            "supports_subscribe": bool(market.get("supports_subscribe")),
            "supports_redeem": bool(market.get("supports_redeem")),
            "supports_wrap": bool(market.get("supports_wrap")),
            "supports_unwrap": bool(market.get("supports_unwrap")),
            "permissioned": bool(market.get("permissioned", True)),
            "read_only": bool(market.get("read_only", False)),
            "minimum_subscribe_value_1e18": market.get("minimum_subscribe_value_1e18"),
            "minimum_redeem_value_1e18": market.get("minimum_redeem_value_1e18"),
            "notes": _notes_list(market),
        }

        try:
            async with web3_from_chain_id(chain_id) as web3:
                token_contract = web3.eth.contract(address=token_address, abi=ERC20_ABI)
                calls = [
                    Call(token_contract, "name", postprocess=str),
                    Call(token_contract, "symbol", postprocess=str),
                    Call(token_contract, "decimals", postprocess=int),
                    Call(token_contract, "totalSupply", postprocess=int),
                ]
                (
                    name,
                    symbol,
                    decimals,
                    total_supply,
                ) = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=chain_id,
                    calls=calls,
                    block_identifier="pending",
                )
                base["name"] = name
                base["symbol"] = symbol
                base["decimals"] = int(decimals)
                base["total_supply_raw"] = int(total_supply)
        except Exception as exc:
            base["metadata_error"] = str(exc)

        base["subscription_tokens"] = await subscription_tokens_task
        return base

    async def _family_price_1e18(self, family: str) -> int | None:
        normalized_family = self._family_name(family)
        if normalized_family == "ousg":
            market = ONDO_RWA_MARKETS[("rousg", CHAIN_ID_ETHEREUM)]
            async with web3_from_chain_id(CHAIN_ID_ETHEREUM) as web3:
                wrapper = web3.eth.contract(
                    address=to_checksum_address(str(market["token"])),
                    abi=ROUSG_WRAPPER_ABI,
                )
                try:
                    return int(
                        await wrapper.functions.getOUSGPrice().call(
                            block_identifier="pending"
                        )
                    )
                except Exception:
                    oracle = web3.eth.contract(
                        address=to_checksum_address(str(market["oracle"])),
                        abi=ONDO_PRICE_ORACLE_ABI,
                    )
                    return int(
                        await oracle.functions.getAssetPrice(
                            to_checksum_address(
                                str(
                                    ONDO_RWA_MARKETS[("ousg", CHAIN_ID_ETHEREUM)][
                                        "token"
                                    ]
                                )
                            )
                        ).call(block_identifier="pending")
                    )

        if normalized_family == "usdy":
            market = ONDO_RWA_MARKETS[("rusdy", CHAIN_ID_ETHEREUM)]
            async with web3_from_chain_id(CHAIN_ID_ETHEREUM) as web3:
                wrapper = web3.eth.contract(
                    address=to_checksum_address(str(market["token"])),
                    abi=RUSDY_WRAPPER_ABI,
                )
                shares_for_one_token = int(
                    await wrapper.functions.getSharesByRUSDY(10**18).call(
                        block_identifier="pending"
                    )
                )
                if shares_for_one_token <= 0:
                    return None
                return ((10**36) * ONDO_SHARES_MULTIPLIER) // shares_for_one_token

        return None

    async def _position_for_market(
        self,
        *,
        account: str,
        market: dict[str, Any],
        include_usd: bool,
        include_zero_positions: bool,
        price_1e18: int | None,
    ) -> dict[str, Any] | None:
        chain_id = int(market["chain_id"])
        token_address = to_checksum_address(str(market["token"]))
        acct = to_checksum_address(account)

        async with web3_from_chain_id(chain_id) as web3:
            token_contract = web3.eth.contract(address=token_address, abi=ERC20_ABI)
            (
                name,
                symbol,
                decimals,
                balance_raw,
                total_supply_raw,
            ) = await read_only_calls_multicall_or_gather(
                web3=web3,
                chain_id=chain_id,
                calls=[
                    Call(token_contract, "name", postprocess=str),
                    Call(token_contract, "symbol", postprocess=str),
                    Call(token_contract, "decimals", postprocess=int),
                    Call(token_contract, "balanceOf", args=(acct,), postprocess=int),
                    Call(token_contract, "totalSupply", postprocess=int),
                ],
                block_identifier="pending",
            )

            if not include_zero_positions and int(balance_raw) == 0:
                return None

            position: dict[str, Any] = {
                "product": str(market["product"]),
                "family": str(market["family"]),
                "chain_id": chain_id,
                "chain_name": str(market["chain_name"]),
                "token_address": token_address,
                "symbol": str(symbol),
                "name": str(name),
                "decimals": int(decimals),
                "balance_raw": int(balance_raw),
                "total_supply_raw": int(total_supply_raw),
                "permissioned": bool(market.get("permissioned", True)),
                "read_only": bool(market.get("read_only", False)),
                "notes": _notes_list(market),
            }

            if str(market["product"]) in {"rousg", "rusdy", "musd"}:
                wrapper_contract = web3.eth.contract(
                    address=token_address,
                    abi=self._wrapper_abi(market),
                )
                conversion_fn = (
                    "getSharesByROUSG"
                    if str(market["family"]) == "ousg"
                    else "getSharesByRUSDY"
                )
                (
                    shares_raw,
                    underlying_equivalent_raw,
                ) = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=chain_id,
                    calls=[
                        Call(
                            wrapper_contract,
                            "sharesOf",
                            args=(acct,),
                            postprocess=int,
                        ),
                        Call(
                            wrapper_contract,
                            conversion_fn,
                            args=(int(balance_raw),),
                            postprocess=int,
                        ),
                    ],
                    block_identifier="pending",
                )

                position["shares_raw"] = int(shares_raw)
                position["underlying_token"] = market.get("underlying_token")
                position["underlying_equivalent_raw"] = int(
                    int(underlying_equivalent_raw) // ONDO_SHARES_MULTIPLIER
                )
            else:
                rebasing_token = market.get("rebasing_token")
                if rebasing_token:
                    position["rebasing_token"] = rebasing_token

            if include_usd and price_1e18 is not None:
                reference_amount_raw = int(
                    position.get("underlying_equivalent_raw", position["balance_raw"])
                )
                position["price_1e18"] = int(price_1e18)
                position["usd_value"] = (
                    float(reference_amount_raw) * float(price_1e18) / 10**36
                )

            return position

    async def get_all_markets(self) -> tuple[bool, list[dict[str, Any]] | str]:
        try:
            markets = await asyncio.gather(
                *[
                    self._read_market_metadata(market)
                    for market in ONDO_RWA_MARKETS.values()
                ]
            )
            markets.sort(key=lambda row: (int(row["chain_id"]), str(row["product"])))
            return True, markets
        except Exception as exc:
            return False, str(exc)

    async def is_subscription_token_supported(
        self,
        *,
        product_family: str,
        token: str,
        chain_id: int | None = None,
    ) -> tuple[bool, bool | str]:
        try:
            family = self._family_name(product_family)

            market = self._market(product=family, chain_id=chain_id)
            manager_address = market.get("manager")
            if not manager_address:
                return False, f"No manager configured for family={family}"

            checksum_token = to_checksum_address(token)
            async with web3_from_chain_id(int(market["chain_id"])) as web3:
                manager = web3.eth.contract(
                    address=to_checksum_address(str(manager_address)),
                    abi=self._manager_abi(market),
                )
                supported = await manager.functions.acceptedSubscriptionTokens(
                    checksum_token
                ).call(block_identifier="pending")
                return True, bool(supported)
        except Exception as exc:
            return False, str(exc)

    async def is_registered_or_eligible(
        self,
        *,
        account: str,
        product_family: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            family = self._family_name(product_family)
            if family != "ousg":
                return True, {
                    "supported": False,
                    "product_family": family,
                    "eligible": None,
                    "reason": "On-chain registry lookup is only configured for the OUSG family in v1",
                }

            market = ONDO_RWA_MARKETS[("ousg", CHAIN_ID_ETHEREUM)]
            registry_address = market.get("id_registry")
            if not registry_address:
                return True, {
                    "supported": False,
                    "product_family": family,
                    "eligible": None,
                    "reason": "No registry configured",
                }

            acct = to_checksum_address(account)
            async with web3_from_chain_id(CHAIN_ID_ETHEREUM) as web3:
                registry = web3.eth.contract(
                    address=to_checksum_address(str(registry_address)),
                    abi=ONDO_ID_REGISTRY_ABI,
                )
                registered_id = await registry.functions.getRegisteredID(
                    to_checksum_address(str(market["token"])),
                    acct,
                ).call(block_identifier="pending")

            hex_id = (
                registered_id.hex()
                if isinstance(registered_id, (bytes, bytearray))
                else str(registered_id)
            )
            if not hex_id.startswith("0x"):
                hex_id = "0x" + hex_id

            return True, {
                "supported": True,
                "product_family": family,
                "account": acct,
                "eligible": hex_id.lower() != _ZERO_BYTES32,
                "registered_id": hex_id,
            }
        except Exception as exc:
            return False, str(exc)

    async def get_pos(
        self,
        *,
        account: str | None = None,
        product: str | None = None,
        chain_id: int | None = None,
        include_usd: bool = False,
        include_zero_positions: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        acct = to_checksum_address(account) if account else self.wallet_address
        if not acct:
            return False, "account (or wallet_address) is required"

        try:
            if product is not None:
                markets_to_read = [self._market(product=product, chain_id=chain_id)]
            else:
                markets_to_read = [
                    market
                    for market in ONDO_RWA_MARKETS.values()
                    if chain_id is None or int(market["chain_id"]) == int(chain_id)
                ]

            families = {
                str(market["family"])
                for market in markets_to_read
                if include_usd and str(market["family"]) in {"ousg", "usdy"}
            }
            price_tasks = {
                family: asyncio.create_task(self._family_price_1e18(family))
                for family in families
            }
            price_by_family = {
                family: await task for family, task in price_tasks.items()
            }

            positions = await asyncio.gather(
                *[
                    self._position_for_market(
                        account=acct,
                        market=market,
                        include_usd=include_usd,
                        include_zero_positions=include_zero_positions,
                        price_1e18=price_by_family.get(str(market["family"])),
                    )
                    for market in markets_to_read
                ]
            )

            filtered_positions = [
                position for position in positions if position is not None
            ]
            out: dict[str, Any] = {
                "protocol": ONDO_RWA_PROTOCOL,
                "account": acct,
                "positions": filtered_positions,
            }
            if include_usd:
                out["total_usd_value"] = sum(
                    float(position.get("usd_value") or 0.0)
                    for position in filtered_positions
                )
            return True, out
        except Exception as exc:
            return False, str(exc)

    async def get_full_user_state(
        self,
        *,
        account: str,
        include_usd: bool = False,
        include_zero_positions: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        acct = to_checksum_address(account)
        (ok_pos, pos), (ok_registry, registry) = await asyncio.gather(
            self.get_pos(
                account=acct,
                include_usd=include_usd,
                include_zero_positions=include_zero_positions,
            ),
            self.is_registered_or_eligible(
                account=acct,
                product_family="ousg",
            ),
        )
        if not ok_pos:
            return False, str(pos)
        if not ok_registry:
            registry = {"supported": True, "error": str(registry)}

        positions = pos.get("positions", []) if isinstance(pos, dict) else []
        by_product: dict[str, list[dict[str, Any]]] = {}
        by_chain: dict[str, list[dict[str, Any]]] = {}
        for position in positions:
            product_key = str(position["product"])
            chain_key = str(position["chain_id"])
            by_product.setdefault(product_key, []).append(position)
            by_chain.setdefault(chain_key, []).append(position)

        out: dict[str, Any] = {
            "protocol": ONDO_RWA_PROTOCOL,
            "account": acct,
            "positions": positions,
            "positions_by_product": by_product,
            "positions_by_chain": by_chain,
            "registration": {"ousg": registry},
        }
        if include_usd and isinstance(pos, dict):
            out["total_usd_value"] = float(pos.get("total_usd_value") or 0.0)
        return True, out

    @require_wallet
    async def subscribe(
        self,
        *,
        product: str,
        deposit_token: str,
        amount: int,
        min_received: int,
        chain_id: int | None = None,
        approval_amount: int = MAX_UINT256,
        preflight: bool = True,
    ) -> tuple[bool, Any]:
        signer_ok, signer_error = self._ensure_signer()
        if not signer_ok:
            return False, str(signer_error)

        market = self._market(product=product, chain_id=chain_id)
        if not market.get("supports_subscribe"):
            return False, f"Subscribe is not supported for product={market['product']}"

        if amount <= 0:
            return False, "amount must be positive"
        if min_received < 0:
            return False, "min_received must be non-negative"

        checksum_deposit_token = to_checksum_address(deposit_token)
        supported_ok, supported = await self.is_subscription_token_supported(
            product_family=str(market["family"]),
            token=checksum_deposit_token,
            chain_id=int(market["chain_id"]),
        )
        if supported_ok and supported is False:
            return (
                False,
                f"deposit token {checksum_deposit_token} is not currently allowlisted for {market['family']}",
            )

        approved = await ensure_allowance(
            token_address=checksum_deposit_token,
            owner=self.wallet_address,
            spender=to_checksum_address(str(market["manager"])),
            amount=int(amount),
            chain_id=int(market["chain_id"]),
            signing_callback=self.sign_callback,
            approval_amount=int(approval_amount),
        )
        if not approved[0]:
            return approved

        transaction = await encode_call(
            target=to_checksum_address(str(market["manager"])),
            abi=self._manager_abi(market),
            fn_name=self._subscribe_fn_name(str(market["product"])),
            args=[checksum_deposit_token, int(amount), int(min_received)],
            from_address=self.wallet_address,
            chain_id=int(market["chain_id"]),
        )
        if preflight:
            ok, reason = await self._preflight_transaction(transaction)
            if not ok:
                return False, str(reason)

        try:
            txn_hash = await send_transaction(transaction, self.sign_callback)
            return True, txn_hash
        except Exception as exc:
            return False, self._format_revert_reason(exc)

    @require_wallet
    async def redeem(
        self,
        *,
        product: str,
        amount: int,
        receiving_token: str,
        min_received: int,
        chain_id: int | None = None,
        approval_amount: int = MAX_UINT256,
        preflight: bool = True,
    ) -> tuple[bool, Any]:
        signer_ok, signer_error = self._ensure_signer()
        if not signer_ok:
            return False, str(signer_error)

        market = self._market(product=product, chain_id=chain_id)
        if not market.get("supports_redeem"):
            return False, f"Redeem is not supported for product={market['product']}"

        if amount <= 0:
            return False, "amount must be positive"
        if min_received < 0:
            return False, "min_received must be non-negative"

        manager_address = to_checksum_address(str(market["manager"]))
        approved = await ensure_allowance(
            token_address=to_checksum_address(str(market["token"])),
            owner=self.wallet_address,
            spender=manager_address,
            amount=int(amount),
            chain_id=int(market["chain_id"]),
            signing_callback=self.sign_callback,
            approval_amount=int(approval_amount),
        )
        if not approved[0]:
            return approved

        transaction = await encode_call(
            target=manager_address,
            abi=self._manager_abi(market),
            fn_name=self._redeem_fn_name(str(market["product"])),
            args=[int(amount), to_checksum_address(receiving_token), int(min_received)],
            from_address=self.wallet_address,
            chain_id=int(market["chain_id"]),
        )
        if preflight:
            ok, reason = await self._preflight_transaction(transaction)
            if not ok:
                return False, str(reason)

        try:
            txn_hash = await send_transaction(transaction, self.sign_callback)
            return True, txn_hash
        except Exception as exc:
            return False, self._format_revert_reason(exc)

    @require_wallet
    async def wrap(
        self,
        *,
        amount: int,
        product: str | None = None,
        token_address: str | None = None,
        chain_id: int | None = None,
        approval_amount: int = MAX_UINT256,
        preflight: bool = True,
    ) -> tuple[bool, Any]:
        signer_ok, signer_error = self._ensure_signer()
        if not signer_ok:
            return False, str(signer_error)
        if amount <= 0:
            return False, "amount must be positive"

        try:
            if product is None and token_address is None:
                return False, "Either product or token_address is required"
            market = (
                self._market(product=product, chain_id=chain_id)
                if product is not None
                else self._find_market_by_token(
                    token_address=str(token_address),
                    chain_id=chain_id,
                )
            )
            underlying_market, wrapper_market = self._wrap_pair(market)
            if not wrapper_market.get("supports_wrap"):
                return (
                    False,
                    f"Wrap is not supported for product={wrapper_market['product']}",
                )

            approved = await ensure_allowance(
                token_address=to_checksum_address(str(underlying_market["token"])),
                owner=self.wallet_address,
                spender=to_checksum_address(str(wrapper_market["token"])),
                amount=int(amount),
                chain_id=int(wrapper_market["chain_id"]),
                signing_callback=self.sign_callback,
                approval_amount=int(approval_amount),
            )
            if not approved[0]:
                return approved

            transaction = await encode_call(
                target=to_checksum_address(str(wrapper_market["token"])),
                abi=self._wrapper_abi(wrapper_market),
                fn_name="wrap",
                args=[int(amount)],
                from_address=self.wallet_address,
                chain_id=int(wrapper_market["chain_id"]),
            )
            if preflight:
                ok, reason = await self._preflight_transaction(transaction)
                if not ok:
                    return False, str(reason)

            txn_hash = await send_transaction(transaction, self.sign_callback)
            return True, txn_hash
        except Exception as exc:
            return False, self._format_revert_reason(exc)

    @require_wallet
    async def unwrap(
        self,
        *,
        amount: int,
        product: str | None = None,
        token_address: str | None = None,
        chain_id: int | None = None,
        preflight: bool = True,
    ) -> tuple[bool, Any]:
        signer_ok, signer_error = self._ensure_signer()
        if not signer_ok:
            return False, str(signer_error)
        if amount <= 0:
            return False, "amount must be positive"

        try:
            if product is None and token_address is None:
                return False, "Either product or token_address is required"
            market = (
                self._market(product=product, chain_id=chain_id)
                if product is not None
                else self._find_market_by_token(
                    token_address=str(token_address),
                    chain_id=chain_id,
                )
            )
            _, wrapper_market = self._wrap_pair(market)
            if not wrapper_market.get("supports_unwrap"):
                return (
                    False,
                    f"Unwrap is not supported for product={wrapper_market['product']}",
                )

            transaction = await encode_call(
                target=to_checksum_address(str(wrapper_market["token"])),
                abi=self._wrapper_abi(wrapper_market),
                fn_name="unwrap",
                args=[int(amount)],
                from_address=self.wallet_address,
                chain_id=int(wrapper_market["chain_id"]),
            )
            if preflight:
                ok, reason = await self._preflight_transaction(transaction)
                if not ok:
                    return False, str(reason)

            txn_hash = await send_transaction(transaction, self.sign_callback)
            return True, txn_hash
        except Exception as exc:
            return False, self._format_revert_reason(exc)
