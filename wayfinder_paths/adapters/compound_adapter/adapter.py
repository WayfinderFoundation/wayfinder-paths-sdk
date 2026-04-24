from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from eth_utils import to_checksum_address

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter, require_wallet
from wayfinder_paths.core.constants.base import MANTISSA, MAX_UINT256, SECONDS_PER_YEAR
from wayfinder_paths.core.constants.compound_abi import COMET_ABI, COMET_REWARDS_ABI
from wayfinder_paths.core.constants.compound_contracts import COMPOUND_COMET_BY_CHAIN
from wayfinder_paths.core.constants.contracts import ZERO_ADDRESS
from wayfinder_paths.core.utils.interest import apr_to_apy
from wayfinder_paths.core.utils.multicall import (
    Call,
    read_only_calls_multicall_or_gather,
)
from wayfinder_paths.core.utils.tokens import ensure_allowance, get_erc20_metadata
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

PRICE_SCALE = 10**8
FACTOR_SCALE = MANTISSA


@dataclass(frozen=True)
class CompoundMarketSeed:
    chain_id: int
    chain_name: str
    market_name: str
    comet: str
    rewards: str
    bulker: str
    configurator: str


@dataclass(frozen=True)
class TokenMetadata:
    address: str
    symbol: str
    name: str
    decimals: int


def _coerce_tuple_value(value: Any, idx: int, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    try:
        attr = getattr(value, key)
        return attr() if callable(attr) else attr
    except Exception:
        pass
    try:
        return value[idx]
    except Exception:
        return None


def _parse_asset_info(value: Any) -> dict[str, Any]:
    return {
        "offset": int(_coerce_tuple_value(value, 0, "offset") or 0),
        "asset": to_checksum_address(str(_coerce_tuple_value(value, 1, "asset"))),
        "price_feed": to_checksum_address(
            str(_coerce_tuple_value(value, 2, "priceFeed"))
        ),
        "scale": int(_coerce_tuple_value(value, 3, "scale") or 0),
        "borrow_collateral_factor_raw": int(
            _coerce_tuple_value(value, 4, "borrowCollateralFactor") or 0
        ),
        "liquidate_collateral_factor_raw": int(
            _coerce_tuple_value(value, 5, "liquidateCollateralFactor") or 0
        ),
        "liquidation_factor_raw": int(
            _coerce_tuple_value(value, 6, "liquidationFactor") or 0
        ),
        "supply_cap": int(_coerce_tuple_value(value, 7, "supplyCap") or 0),
    }


def _parse_totals_basic(value: Any) -> dict[str, int]:
    return {
        "base_supply_index": int(_coerce_tuple_value(value, 0, "baseSupplyIndex") or 0),
        "base_borrow_index": int(_coerce_tuple_value(value, 1, "baseBorrowIndex") or 0),
        "tracking_supply_index": int(
            _coerce_tuple_value(value, 2, "trackingSupplyIndex") or 0
        ),
        "tracking_borrow_index": int(
            _coerce_tuple_value(value, 3, "trackingBorrowIndex") or 0
        ),
        "total_supply_base": int(_coerce_tuple_value(value, 4, "totalSupplyBase") or 0),
        "total_borrow_base": int(_coerce_tuple_value(value, 5, "totalBorrowBase") or 0),
        "last_accrual_time": int(_coerce_tuple_value(value, 6, "lastAccrualTime") or 0),
        "pause_flags": int(_coerce_tuple_value(value, 7, "pauseFlags") or 0),
    }


def _parse_user_basic(value: Any) -> dict[str, int]:
    return {
        "principal": int(_coerce_tuple_value(value, 0, "principal") or 0),
        "base_tracking_index": int(
            _coerce_tuple_value(value, 1, "baseTrackingIndex") or 0
        ),
        "base_tracking_accrued": int(
            _coerce_tuple_value(value, 2, "baseTrackingAccrued") or 0
        ),
        "assets_in": int(_coerce_tuple_value(value, 3, "assetsIn") or 0),
    }


def _parse_reward_owed(value: Any) -> tuple[str | None, int]:
    token = _coerce_tuple_value(value, 0, "token")
    if not token or str(token) == ZERO_ADDRESS:
        return None, int(_coerce_tuple_value(value, 1, "owed") or 0)
    return to_checksum_address(str(token)), int(
        _coerce_tuple_value(value, 1, "owed") or 0
    )


def _factor_to_float(raw: int) -> float:
    return float(raw) / float(FACTOR_SCALE) if raw else 0.0


def _price_to_float(raw: int | None) -> float | None:
    if raw is None:
        return None
    return float(raw) / float(PRICE_SCALE)


def _amount_to_decimal(raw: int, decimals: int) -> float:
    if decimals < 0:
        return float(raw)
    return float(raw) / float(10**decimals)


def _rate_to_apr(raw_rate: int) -> float:
    if raw_rate <= 0:
        return 0.0
    return (float(raw_rate) / float(MANTISSA)) * float(SECONDS_PER_YEAR)


def _scale_to_decimals(scale: int) -> int | None:
    if scale <= 0:
        return None
    value = int(scale)
    decimals = 0
    while value > 1 and value % 10 == 0:
        value //= 10
        decimals += 1
    if value == 1:
        return decimals
    return None


def _pause_flags_to_dict(flags: int) -> dict[str, bool]:
    flags_int = int(flags)
    return {
        "supply_paused": bool(flags_int & (1 << 0)),
        "transfer_paused": bool(flags_int & (1 << 1)),
        "withdraw_paused": bool(flags_int & (1 << 2)),
        "absorb_paused": bool(flags_int & (1 << 3)),
        "buy_paused": bool(flags_int & (1 << 4)),
    }


class CompoundAdapter(BaseAdapter):
    adapter_type: str = "COMPOUND"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        sign_callback=None,
        wallet_address: str | None = None,
    ) -> None:
        super().__init__("compound_adapter", config or {})
        self.sign_callback = sign_callback
        self.wallet_address: str | None = (
            to_checksum_address(wallet_address) if wallet_address else None
        )
        self._token_metadata_cache: dict[tuple[int, str], TokenMetadata] = {}

    @staticmethod
    def _entry(chain_id: int) -> dict[str, Any]:
        entry = COMPOUND_COMET_BY_CHAIN.get(int(chain_id))
        if not entry:
            raise ValueError(f"Unsupported Compound chain_id={chain_id}")
        return entry

    def _list_market_seeds(
        self, chain_id: int | None = None
    ) -> list[CompoundMarketSeed]:
        chain_ids = (
            [int(chain_id)]
            if chain_id is not None
            else list(COMPOUND_COMET_BY_CHAIN.keys())
        )
        seeds: list[CompoundMarketSeed] = []
        for cid in chain_ids:
            entry = self._entry(cid)
            shared_rewards = to_checksum_address(str(entry["rewards"]))
            shared_bulker = to_checksum_address(str(entry["bulker"]))
            configurator = to_checksum_address(str(entry["configurator"]))
            chain_name = str(entry["chain_name"])
            markets = entry.get("markets") or {}
            for market_name, market_cfg in markets.items():
                market = dict(market_cfg)
                seeds.append(
                    CompoundMarketSeed(
                        chain_id=int(cid),
                        chain_name=chain_name,
                        market_name=str(market_name),
                        comet=to_checksum_address(str(market["comet"])),
                        rewards=shared_rewards,
                        bulker=to_checksum_address(
                            str(market.get("bulker") or shared_bulker)
                        ),
                        configurator=configurator,
                    )
                )
        seeds.sort(
            key=lambda seed: (seed.chain_id, seed.market_name, seed.comet.lower())
        )
        return seeds

    def _find_market_seed(self, *, chain_id: int, comet: str) -> CompoundMarketSeed:
        checksum_comet = to_checksum_address(comet)
        for seed in self._list_market_seeds(chain_id):
            if seed.comet.lower() == checksum_comet.lower():
                return seed
        raise ValueError(
            f"Unknown Compound comet={checksum_comet} on chain_id={chain_id}"
        )

    async def _token_metadata(
        self,
        *,
        chain_id: int,
        token_address: str,
        web3: Any,
        fallback_decimals: int | None = None,
    ) -> TokenMetadata:
        checksum_token = to_checksum_address(token_address)
        cache_key = (int(chain_id), checksum_token.lower())
        cached = self._token_metadata_cache.get(cache_key)
        if cached:
            return cached

        try:
            symbol, name, decimals = await get_erc20_metadata(
                checksum_token,
                web3=web3,
                block_identifier="latest",
            )
            metadata = TokenMetadata(
                address=checksum_token,
                symbol=str(symbol or ""),
                name=str(name or ""),
                decimals=int(decimals),
            )
        except Exception:
            metadata = TokenMetadata(
                address=checksum_token,
                symbol="",
                name="",
                decimals=int(
                    fallback_decimals if fallback_decimals is not None else 18
                ),
            )

        self._token_metadata_cache[cache_key] = metadata
        return metadata

    async def _resolve_base_token(
        self,
        *,
        chain_id: int,
        comet: str,
        base_token: str | None = None,
    ) -> str:
        async with web3_from_chain_id(int(chain_id)) as web3:
            contract = web3.eth.contract(
                address=to_checksum_address(comet), abi=COMET_ABI
            )
            onchain_base_token = await contract.functions.baseToken().call(
                block_identifier="latest"
            )
        resolved = to_checksum_address(str(onchain_base_token))
        if (
            base_token is not None
            and resolved.lower() != to_checksum_address(base_token).lower()
        ):
            raise ValueError(
                f"base_token mismatch for comet={to_checksum_address(comet)}: "
                f"expected {resolved}, got {to_checksum_address(base_token)}"
            )
        return resolved

    async def _get_collateral_asset_info(
        self,
        *,
        chain_id: int,
        comet: str,
        asset: str,
    ) -> dict[str, Any]:
        checksum_comet = to_checksum_address(comet)
        checksum_asset = to_checksum_address(asset)
        async with web3_from_chain_id(int(chain_id)) as web3:
            contract = web3.eth.contract(address=checksum_comet, abi=COMET_ABI)
            raw_info = await contract.functions.getAssetInfoByAddress(
                checksum_asset
            ).call(block_identifier="latest")
        info = _parse_asset_info(raw_info)
        if info["asset"].lower() != checksum_asset.lower():
            raise ValueError(
                f"asset {checksum_asset} is not a supported collateral asset for {checksum_comet}"
            )
        return info

    async def _reward_config(
        self,
        *,
        rewards_contract: str,
        comet: str,
        chain_id: int,
    ) -> dict[str, Any]:
        checksum_rewards = to_checksum_address(rewards_contract)
        checksum_comet = to_checksum_address(comet)
        async with web3_from_chain_id(int(chain_id)) as web3:
            contract = web3.eth.contract(
                address=checksum_rewards, abi=COMET_REWARDS_ABI
            )
            try:
                raw = await contract.functions.rewardConfig(checksum_comet).call(
                    block_identifier="latest"
                )
            except Exception:
                return {
                    "token": None,
                    "rescale_factor": 0,
                    "should_upscale": False,
                    "multiplier": 0,
                }

        token = _coerce_tuple_value(raw, 0, "token")
        return {
            "token": (
                None
                if not token or str(token) == ZERO_ADDRESS
                else to_checksum_address(str(token))
            ),
            "rescale_factor": int(_coerce_tuple_value(raw, 1, "rescaleFactor") or 0),
            "should_upscale": bool(
                _coerce_tuple_value(raw, 2, "shouldUpscale") or False
            ),
            "multiplier": int(_coerce_tuple_value(raw, 3, "multiplier") or 0),
        }

    async def _get_reward_owed(
        self,
        *,
        chain_id: int,
        rewards_contract: str,
        comet: str,
        account: str,
        configured_reward_token: str | None,
    ) -> dict[str, Any]:
        if configured_reward_token is None:
            return {"reward_token": None, "reward_owed": 0, "reward_error": None}

        checksum_rewards = to_checksum_address(rewards_contract)
        checksum_comet = to_checksum_address(comet)
        checksum_account = to_checksum_address(account)
        async with web3_from_chain_id(int(chain_id)) as web3:
            contract = web3.eth.contract(
                address=checksum_rewards, abi=COMET_REWARDS_ABI
            )
            try:
                raw_owed = await contract.functions.getRewardOwed(
                    checksum_comet,
                    checksum_account,
                ).call(block_identifier="pending")
            except Exception as exc:
                return {
                    "reward_token": configured_reward_token,
                    "reward_owed": 0,
                    "reward_error": str(exc),
                }

        reward_token, reward_owed = _parse_reward_owed(raw_owed)
        return {
            "reward_token": reward_token or configured_reward_token,
            "reward_owed": int(reward_owed),
            "reward_error": None,
        }

    async def _load_market_snapshot(
        self,
        *,
        seed: CompoundMarketSeed,
        include_prices: bool = True,
    ) -> dict[str, Any]:
        async with web3_from_chain_id(seed.chain_id) as web3:
            comet = web3.eth.contract(address=seed.comet, abi=COMET_ABI)

            core_rows = await read_only_calls_multicall_or_gather(
                web3=web3,
                chain_id=seed.chain_id,
                calls=[
                    Call(comet, "name", postprocess=str),
                    Call(comet, "symbol", postprocess=str),
                    Call(
                        comet,
                        "baseToken",
                        postprocess=lambda value: to_checksum_address(str(value)),
                    ),
                    Call(
                        comet,
                        "baseTokenPriceFeed",
                        postprocess=lambda value: to_checksum_address(str(value)),
                    ),
                    Call(comet, "baseScale", postprocess=int),
                    Call(comet, "decimals", postprocess=int),
                    Call(comet, "numAssets", postprocess=int),
                    Call(comet, "totalSupply", postprocess=int),
                    Call(comet, "totalBorrow", postprocess=int),
                    Call(comet, "totalsBasic", postprocess=_parse_totals_basic),
                    Call(comet, "getUtilization", postprocess=int),
                    Call(comet, "baseBorrowMin", postprocess=int),
                    Call(comet, "baseMinForRewards", postprocess=int),
                    Call(comet, "baseTrackingSupplySpeed", postprocess=int),
                    Call(comet, "baseTrackingBorrowSpeed", postprocess=int),
                    Call(comet, "targetReserves", postprocess=int),
                ],
                block_identifier="pending",
            )

            (
                comet_name,
                comet_symbol,
                base_token,
                base_token_price_feed,
                base_scale,
                base_decimals,
                num_assets,
                total_supply,
                total_borrow,
                totals_basic,
                utilization,
                base_borrow_min,
                base_min_for_rewards,
                base_tracking_supply_speed,
                base_tracking_borrow_speed,
                target_reserves,
            ) = core_rows

            supply_rate, borrow_rate = await read_only_calls_multicall_or_gather(
                web3=web3,
                chain_id=seed.chain_id,
                calls=[
                    Call(
                        comet,
                        "getSupplyRate",
                        args=(int(utilization),),
                        postprocess=int,
                    ),
                    Call(
                        comet,
                        "getBorrowRate",
                        args=(int(utilization),),
                        postprocess=int,
                    ),
                ],
                block_identifier="pending",
            )

            asset_infos: list[dict[str, Any]] = []
            if int(num_assets) > 0:
                raw_asset_infos = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=seed.chain_id,
                    calls=[
                        Call(
                            comet,
                            "getAssetInfo",
                            args=(i,),
                            postprocess=_parse_asset_info,
                        )
                        for i in range(int(num_assets))
                    ],
                    block_identifier="latest",
                )
                asset_infos = [dict(row) for row in raw_asset_infos]

            total_collateral_rows: list[int] = []
            if asset_infos:
                totals_raw = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=seed.chain_id,
                    calls=[
                        Call(
                            comet,
                            "totalsCollateral",
                            args=(str(info["asset"]),),
                            postprocess=lambda row: int(
                                _coerce_tuple_value(row, 0, "totalSupplyAsset") or 0
                            ),
                        )
                        for info in asset_infos
                    ],
                    block_identifier="pending",
                )
                total_collateral_rows = [int(value or 0) for value in totals_raw]

            base_price_raw: int | None = None
            collateral_price_rows: list[int | None] = [None for _ in asset_infos]
            if include_prices:
                price_calls = [
                    Call(
                        comet,
                        "getPrice",
                        args=(base_token_price_feed,),
                        postprocess=int,
                    )
                ] + [
                    Call(
                        comet,
                        "getPrice",
                        args=(str(info["price_feed"]),),
                        postprocess=int,
                    )
                    for info in asset_infos
                ]
                price_rows = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=seed.chain_id,
                    calls=price_calls,
                    block_identifier="pending",
                )
                if price_rows:
                    base_price_raw = int(price_rows[0])
                    collateral_price_rows = [int(value) for value in price_rows[1:]]

            reward_cfg = await self._reward_config(
                rewards_contract=seed.rewards,
                comet=seed.comet,
                chain_id=seed.chain_id,
            )

            base_meta = await self._token_metadata(
                chain_id=seed.chain_id,
                token_address=str(base_token),
                web3=web3,
                fallback_decimals=int(base_decimals),
            )

            reward_meta: TokenMetadata | None = None
            if reward_cfg["token"] is not None:
                reward_meta = await self._token_metadata(
                    chain_id=seed.chain_id,
                    token_address=str(reward_cfg["token"]),
                    web3=web3,
                )

            collateral_assets: list[dict[str, Any]] = []
            for asset_info, total_supply_asset, price_raw in zip(
                asset_infos,
                total_collateral_rows or [0 for _ in asset_infos],
                collateral_price_rows,
                strict=True,
            ):
                fallback_collateral_decimals = _scale_to_decimals(
                    int(asset_info["scale"])
                )
                asset_meta = await self._token_metadata(
                    chain_id=seed.chain_id,
                    token_address=str(asset_info["asset"]),
                    web3=web3,
                    fallback_decimals=fallback_collateral_decimals,
                )
                collateral_assets.append(
                    {
                        "asset": asset_meta.address,
                        "symbol": asset_meta.symbol,
                        "name": asset_meta.name,
                        "decimals": asset_meta.decimals,
                        "price_feed": str(asset_info["price_feed"]),
                        "price": int(price_raw) if price_raw is not None else None,
                        "price_usd": _price_to_float(int(price_raw))
                        if price_raw is not None
                        else None,
                        "scale": int(asset_info["scale"]),
                        "offset": int(asset_info["offset"]),
                        "borrow_collateral_factor_raw": int(
                            asset_info["borrow_collateral_factor_raw"]
                        ),
                        "borrow_collateral_factor": _factor_to_float(
                            int(asset_info["borrow_collateral_factor_raw"])
                        ),
                        "liquidate_collateral_factor_raw": int(
                            asset_info["liquidate_collateral_factor_raw"]
                        ),
                        "liquidate_collateral_factor": _factor_to_float(
                            int(asset_info["liquidate_collateral_factor_raw"])
                        ),
                        "liquidation_factor_raw": int(
                            asset_info["liquidation_factor_raw"]
                        ),
                        "liquidation_factor": _factor_to_float(
                            int(asset_info["liquidation_factor_raw"])
                        ),
                        "supply_cap": int(asset_info["supply_cap"]),
                        "total_supply_asset": int(total_supply_asset),
                    }
                )

        base_supply_apr = _rate_to_apr(int(supply_rate))
        base_borrow_apr = _rate_to_apr(int(borrow_rate))

        return {
            "protocol": "compound",
            "chain_id": int(seed.chain_id),
            "chain_name": seed.chain_name,
            "market_name": seed.market_name,
            "market_key": f"{seed.chain_name}:{seed.market_name}",
            "comet": seed.comet,
            "comet_name": str(comet_name),
            "comet_symbol": str(comet_symbol),
            "rewards": seed.rewards,
            "bulker": seed.bulker,
            "configurator": seed.configurator,
            "base_token": base_meta.address,
            "base_token_symbol": base_meta.symbol,
            "base_token_name": base_meta.name,
            "base_token_decimals": int(base_meta.decimals),
            "base_token_price_feed": str(base_token_price_feed),
            "base_token_price": int(base_price_raw)
            if base_price_raw is not None
            else None,
            "base_token_price_usd": _price_to_float(base_price_raw),
            "base_scale": int(base_scale),
            "num_assets": int(num_assets),
            "collateral_assets": collateral_assets,
            "total_supply": int(total_supply),
            "total_borrow": int(total_borrow),
            "totals_basic": dict(totals_basic),
            "pause_state": _pause_flags_to_dict(int(totals_basic["pause_flags"])),
            "utilization": int(utilization),
            "base_supply_rate": int(supply_rate),
            "base_borrow_rate": int(borrow_rate),
            "base_supply_apr": float(base_supply_apr),
            "base_borrow_apr": float(base_borrow_apr),
            "base_supply_apy": float(apr_to_apy(base_supply_apr)),
            "base_borrow_apy": float(apr_to_apy(base_borrow_apr)),
            "base_borrow_min": int(base_borrow_min),
            "base_min_for_rewards": int(base_min_for_rewards),
            "base_tracking_supply_speed": int(base_tracking_supply_speed),
            "base_tracking_borrow_speed": int(base_tracking_borrow_speed),
            "target_reserves": int(target_reserves),
            "reward_token": reward_meta.address if reward_meta else None,
            "reward_token_symbol": reward_meta.symbol if reward_meta else None,
            "reward_token_name": reward_meta.name if reward_meta else None,
            "reward_token_decimals": reward_meta.decimals if reward_meta else None,
            "reward_config": reward_cfg,
        }

    async def get_market(
        self,
        *,
        chain_id: int,
        comet: str,
        include_prices: bool = True,
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            seed = self._find_market_seed(chain_id=int(chain_id), comet=comet)
            market = await self._load_market_snapshot(
                seed=seed,
                include_prices=bool(include_prices),
            )
            return True, market
        except Exception as exc:
            return False, str(exc)

    async def get_all_markets(
        self,
        *,
        chain_id: int | None = None,
        include_prices: bool = True,
        concurrency: int = 4,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        seeds = self._list_market_seeds(chain_id)
        semaphore = asyncio.Semaphore(max(1, int(concurrency)))

        async def _load(seed: CompoundMarketSeed) -> tuple[bool, dict[str, Any] | str]:
            async with semaphore:
                try:
                    market = await self._load_market_snapshot(
                        seed=seed,
                        include_prices=bool(include_prices),
                    )
                    return True, market
                except Exception as exc:
                    return False, str(exc)

        results = await asyncio.gather(*[_load(seed) for seed in seeds])
        markets = [result for ok, result in results if ok and isinstance(result, dict)]
        errors = [
            result for ok, result in results if not ok and isinstance(result, str)
        ]

        if not markets:
            return False, errors[0] if errors else "no Compound markets available"
        if chain_id is not None and errors:
            return False, errors[0]

        markets.sort(
            key=lambda market: (
                int(market["chain_id"]),
                str(market["market_name"]),
                str(market["comet"]).lower(),
            )
        )
        return True, markets

    async def get_pos(
        self,
        *,
        chain_id: int,
        comet: str,
        account: str,
        include_prices: bool = True,
        include_zero_collateral: bool = True,
    ) -> tuple[bool, dict[str, Any] | str]:
        checksum_account = to_checksum_address(account)
        try:
            ok, market_or_error = await self.get_market(
                chain_id=int(chain_id),
                comet=comet,
                include_prices=bool(include_prices),
            )
            if not ok or not isinstance(market_or_error, dict):
                return False, str(market_or_error)
            market = dict(market_or_error)

            async with web3_from_chain_id(int(chain_id)) as web3:
                comet_contract = web3.eth.contract(
                    address=to_checksum_address(comet),
                    abi=COMET_ABI,
                )
                read_calls = [
                    Call(
                        comet_contract,
                        "balanceOf",
                        args=(checksum_account,),
                        postprocess=int,
                    ),
                    Call(
                        comet_contract,
                        "borrowBalanceOf",
                        args=(checksum_account,),
                        postprocess=int,
                    ),
                    Call(
                        comet_contract,
                        "baseTrackingAccrued",
                        args=(checksum_account,),
                        postprocess=int,
                    ),
                    Call(
                        comet_contract,
                        "isBorrowCollateralized",
                        args=(checksum_account,),
                        postprocess=bool,
                    ),
                    Call(
                        comet_contract,
                        "isLiquidatable",
                        args=(checksum_account,),
                        postprocess=bool,
                    ),
                    Call(
                        comet_contract,
                        "userBasic",
                        args=(checksum_account,),
                        postprocess=_parse_user_basic,
                    ),
                ] + [
                    Call(
                        comet_contract,
                        "collateralBalanceOf",
                        args=(checksum_account, str(asset["asset"])),
                        postprocess=int,
                    )
                    for asset in market.get("collateral_assets") or []
                ]

                rows = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=int(chain_id),
                    calls=read_calls,
                    block_identifier="pending",
                )

            supplied_base = int(rows[0])
            borrowed_base = int(rows[1])
            base_tracking_accrued = int(rows[2])
            is_borrow_collateralized = bool(rows[3])
            is_liquidatable = bool(rows[4])
            user_basic = dict(rows[5])
            collateral_balances = [int(value) for value in rows[6:]]

            reward_read = await self._get_reward_owed(
                chain_id=int(chain_id),
                rewards_contract=str(market["rewards"]),
                comet=str(market["comet"]),
                account=checksum_account,
                configured_reward_token=market.get("reward_token"),
            )

            reward_decimals = market.get("reward_token_decimals")
            collateral_positions: list[dict[str, Any]] = []
            for asset, balance in zip(
                market.get("collateral_assets") or [],
                collateral_balances,
                strict=True,
            ):
                if not include_zero_collateral and int(balance) == 0:
                    continue
                price_raw = asset.get("price")
                asset_decimals = int(asset.get("decimals") or 0)
                balance_decimal = _amount_to_decimal(int(balance), asset_decimals)
                price_usd = (
                    _price_to_float(int(price_raw)) if price_raw is not None else None
                )
                usd_value = (
                    balance_decimal * price_usd if price_usd is not None else None
                )
                collateral_positions.append(
                    {
                        "asset": str(asset["asset"]),
                        "symbol": str(asset.get("symbol") or ""),
                        "name": str(asset.get("name") or ""),
                        "decimals": asset_decimals,
                        "balance": int(balance),
                        "balance_decimal": balance_decimal,
                        "price_feed": str(asset.get("price_feed") or ""),
                        "price": price_raw,
                        "price_usd": price_usd,
                        "usd_value": usd_value,
                        "scale": int(asset.get("scale") or 0),
                        "borrow_collateral_factor_raw": int(
                            asset.get("borrow_collateral_factor_raw") or 0
                        ),
                        "borrow_collateral_factor": float(
                            asset.get("borrow_collateral_factor") or 0.0
                        ),
                        "liquidate_collateral_factor_raw": int(
                            asset.get("liquidate_collateral_factor_raw") or 0
                        ),
                        "liquidate_collateral_factor": float(
                            asset.get("liquidate_collateral_factor") or 0.0
                        ),
                        "liquidation_factor_raw": int(
                            asset.get("liquidation_factor_raw") or 0
                        ),
                        "liquidation_factor": float(
                            asset.get("liquidation_factor") or 0.0
                        ),
                        "supply_cap": int(asset.get("supply_cap") or 0),
                        "total_supply_asset": int(asset.get("total_supply_asset") or 0),
                    }
                )

            base_decimals = int(market.get("base_token_decimals") or 0)
            reward_owed = int(reward_read["reward_owed"])
            return (
                True,
                {
                    "protocol": "compound",
                    "chain_id": int(chain_id),
                    "chain_name": str(market["chain_name"]),
                    "market_name": str(market["market_name"]),
                    "market_key": str(market["market_key"]),
                    "account": checksum_account,
                    "comet": str(market["comet"]),
                    "base_token": str(market["base_token"]),
                    "base_token_symbol": str(market.get("base_token_symbol") or ""),
                    "base_token_decimals": base_decimals,
                    "supplied_base": supplied_base,
                    "borrowed_base": borrowed_base,
                    "net_base": int(supplied_base - borrowed_base),
                    "supplied_base_decimal": _amount_to_decimal(
                        supplied_base, base_decimals
                    ),
                    "borrowed_base_decimal": _amount_to_decimal(
                        borrowed_base, base_decimals
                    ),
                    "net_base_decimal": _amount_to_decimal(
                        supplied_base - borrowed_base, base_decimals
                    ),
                    "base_tracking_accrued": base_tracking_accrued,
                    "is_borrow_collateralized": is_borrow_collateralized,
                    "is_liquidatable": is_liquidatable,
                    "user_basic": user_basic,
                    "reward_token": reward_read["reward_token"],
                    "reward_token_symbol": market.get("reward_token_symbol"),
                    "reward_token_name": market.get("reward_token_name"),
                    "reward_token_decimals": reward_decimals,
                    "reward_owed": reward_owed,
                    "reward_owed_decimal": (
                        _amount_to_decimal(reward_owed, int(reward_decimals))
                        if reward_decimals is not None
                        else None
                    ),
                    "reward_error": reward_read["reward_error"],
                    "collateral_positions": collateral_positions,
                },
            )
        except Exception as exc:
            return False, str(exc)

    async def get_full_user_state(
        self,
        *,
        account: str | None = None,
        chain_id: int | None = None,
        include_zero_positions: bool = False,
        include_prices: bool = True,
        include_zero_collateral: bool = True,
        concurrency: int = 4,
    ) -> tuple[bool, dict[str, Any] | str]:
        resolved_account = account or self.wallet_address
        if not resolved_account:
            return False, "account or wallet_address is required"

        checksum_account = to_checksum_address(resolved_account)
        seeds = self._list_market_seeds(chain_id)
        semaphore = asyncio.Semaphore(max(1, int(concurrency)))

        async def _load(seed: CompoundMarketSeed) -> tuple[bool, dict[str, Any] | str]:
            async with semaphore:
                return await self.get_pos(
                    chain_id=seed.chain_id,
                    comet=seed.comet,
                    account=checksum_account,
                    include_prices=bool(include_prices),
                    include_zero_collateral=bool(include_zero_collateral),
                )

        results = await asyncio.gather(*[_load(seed) for seed in seeds])
        positions: list[dict[str, Any]] = []
        errors: list[str] = []
        for ok, payload in results:
            if ok and isinstance(payload, dict):
                has_collateral = any(
                    int(item.get("balance") or 0) > 0
                    for item in payload.get("collateral_positions") or []
                )
                has_base = (
                    int(payload.get("supplied_base") or 0) > 0
                    or int(payload.get("borrowed_base") or 0) > 0
                )
                if include_zero_positions or has_collateral or has_base:
                    positions.append(payload)
            elif isinstance(payload, str):
                errors.append(payload)

        if not positions and errors:
            return False, errors[0]

        positions.sort(
            key=lambda position: (
                int(position["chain_id"]),
                str(position["market_name"]),
                str(position["comet"]).lower(),
            )
        )
        return (
            True,
            {
                "protocol": "compound",
                "account": checksum_account,
                "chain_id": int(chain_id) if chain_id is not None else None,
                "position_count": len(positions),
                "positions": positions,
                "errors": errors,
            },
        )

    @require_wallet
    async def lend(
        self,
        *,
        chain_id: int,
        comet: str,
        base_token: str,
        amount: int,
    ) -> tuple[bool, Any]:
        if not self.sign_callback:
            return False, "sign_callback is required"
        if int(amount) <= 0:
            return False, "amount must be positive"

        try:
            checksum_comet = to_checksum_address(comet)
            checksum_base = await self._resolve_base_token(
                chain_id=int(chain_id),
                comet=checksum_comet,
                base_token=base_token,
            )

            approved = await ensure_allowance(
                token_address=checksum_base,
                owner=self.wallet_address,
                spender=checksum_comet,
                amount=int(amount),
                chain_id=int(chain_id),
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=checksum_comet,
                abi=COMET_ABI,
                fn_name="supply",
                args=[checksum_base, int(amount)],
                from_address=self.wallet_address,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def unlend(
        self,
        *,
        chain_id: int,
        comet: str,
        base_token: str,
        amount: int,
        withdraw_full: bool = False,
    ) -> tuple[bool, Any]:
        if not self.sign_callback:
            return False, "sign_callback is required"
        if int(amount) <= 0 and not withdraw_full:
            return False, "amount must be positive unless withdraw_full=True"

        try:
            checksum_comet = to_checksum_address(comet)
            checksum_base = await self._resolve_base_token(
                chain_id=int(chain_id),
                comet=checksum_comet,
                base_token=base_token,
            )
            withdraw_amount = int(MAX_UINT256 if withdraw_full else amount)
            tx = await encode_call(
                target=checksum_comet,
                abi=COMET_ABI,
                fn_name="withdraw",
                args=[checksum_base, withdraw_amount],
                from_address=self.wallet_address,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def borrow(
        self,
        *,
        chain_id: int,
        comet: str,
        base_token: str,
        amount: int,
    ) -> tuple[bool, Any]:
        if not self.sign_callback:
            return False, "sign_callback is required"
        if int(amount) <= 0:
            return False, "amount must be positive"

        try:
            checksum_comet = to_checksum_address(comet)
            checksum_base = await self._resolve_base_token(
                chain_id=int(chain_id),
                comet=checksum_comet,
                base_token=base_token,
            )

            ok, market_or_error = await self.get_market(
                chain_id=int(chain_id),
                comet=checksum_comet,
                include_prices=False,
            )
            if not ok or not isinstance(market_or_error, dict):
                return False, str(market_or_error)
            market = dict(market_or_error)
            base_borrow_min = int(market.get("base_borrow_min") or 0)
            if base_borrow_min > 0 and int(amount) < base_borrow_min:
                return (
                    False,
                    f"amount must be >= baseBorrowMin ({base_borrow_min}) for comet={checksum_comet}",
                )

            tx = await encode_call(
                target=checksum_comet,
                abi=COMET_ABI,
                fn_name="withdraw",
                args=[checksum_base, int(amount)],
                from_address=self.wallet_address,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def repay(
        self,
        *,
        chain_id: int,
        comet: str,
        base_token: str,
        amount: int,
        repay_full: bool = False,
    ) -> tuple[bool, Any]:
        if not self.sign_callback:
            return False, "sign_callback is required"
        if int(amount) <= 0 and not repay_full:
            return False, "amount must be positive unless repay_full=True"

        try:
            checksum_comet = to_checksum_address(comet)
            checksum_base = await self._resolve_base_token(
                chain_id=int(chain_id),
                comet=checksum_comet,
                base_token=base_token,
            )
            supply_amount = int(MAX_UINT256 if repay_full else amount)
            allowance_amount = int(MAX_UINT256 if repay_full else amount)
            approved = await ensure_allowance(
                token_address=checksum_base,
                owner=self.wallet_address,
                spender=checksum_comet,
                amount=allowance_amount,
                chain_id=int(chain_id),
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=checksum_comet,
                abi=COMET_ABI,
                fn_name="supply",
                args=[checksum_base, supply_amount],
                from_address=self.wallet_address,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def supply_collateral(
        self,
        *,
        chain_id: int,
        comet: str,
        collateral_asset: str,
        amount: int,
    ) -> tuple[bool, Any]:
        if not self.sign_callback:
            return False, "sign_callback is required"
        if int(amount) <= 0:
            return False, "amount must be positive"

        try:
            checksum_comet = to_checksum_address(comet)
            asset_info = await self._get_collateral_asset_info(
                chain_id=int(chain_id),
                comet=checksum_comet,
                asset=collateral_asset,
            )
            checksum_asset = to_checksum_address(str(asset_info["asset"]))

            approved = await ensure_allowance(
                token_address=checksum_asset,
                owner=self.wallet_address,
                spender=checksum_comet,
                amount=int(amount),
                chain_id=int(chain_id),
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=checksum_comet,
                abi=COMET_ABI,
                fn_name="supply",
                args=[checksum_asset, int(amount)],
                from_address=self.wallet_address,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def withdraw_collateral(
        self,
        *,
        chain_id: int,
        comet: str,
        collateral_asset: str,
        amount: int,
        withdraw_full: bool = False,
    ) -> tuple[bool, Any]:
        if not self.sign_callback:
            return False, "sign_callback is required"
        if int(amount) <= 0 and not withdraw_full:
            return False, "amount must be positive unless withdraw_full=True"

        try:
            checksum_comet = to_checksum_address(comet)
            asset_info = await self._get_collateral_asset_info(
                chain_id=int(chain_id),
                comet=checksum_comet,
                asset=collateral_asset,
            )
            checksum_asset = to_checksum_address(str(asset_info["asset"]))
            withdraw_amount = int(amount)

            if withdraw_full:
                async with web3_from_chain_id(int(chain_id)) as web3:
                    contract = web3.eth.contract(address=checksum_comet, abi=COMET_ABI)
                    withdraw_amount = int(
                        await contract.functions.collateralBalanceOf(
                            self.wallet_address,
                            checksum_asset,
                        ).call(block_identifier="pending")
                    )
                if withdraw_amount <= 0:
                    return False, "no collateral balance available to withdraw"

            tx = await encode_call(
                target=checksum_comet,
                abi=COMET_ABI,
                fn_name="withdraw",
                args=[checksum_asset, int(withdraw_amount)],
                from_address=self.wallet_address,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def claim_rewards(
        self,
        *,
        chain_id: int,
        comet: str,
        rewards_contract: str | None = None,
        should_accrue: bool = True,
    ) -> tuple[bool, Any]:
        if not self.sign_callback:
            return False, "sign_callback is required"

        try:
            seed = self._find_market_seed(chain_id=int(chain_id), comet=comet)
            checksum_comet = to_checksum_address(comet)
            checksum_rewards = to_checksum_address(rewards_contract or seed.rewards)
            reward_cfg = await self._reward_config(
                rewards_contract=checksum_rewards,
                comet=checksum_comet,
                chain_id=int(chain_id),
            )
            if reward_cfg["token"] is None:
                return False, f"rewards not configured for comet={checksum_comet}"

            tx = await encode_call(
                target=checksum_rewards,
                abi=COMET_REWARDS_ABI,
                fn_name="claim",
                args=[checksum_comet, self.wallet_address, bool(should_accrue)],
                from_address=self.wallet_address,
                chain_id=int(chain_id),
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:
            return False, str(exc)
