from __future__ import annotations

import asyncio
from collections.abc import Sequence
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

def _parse_asset_info(value: Sequence[Any]) -> dict[str, Any]:
    return {
        "offset": int(value[0] or 0),
        "asset": to_checksum_address(value[1]),
        "price_feed": to_checksum_address(value[2]),
        "scale": int(value[3] or 0),
        "borrow_collateral_factor_raw": int(value[4] or 0),
        "liquidate_collateral_factor_raw": int(value[5] or 0),
        "liquidation_factor_raw": int(value[6] or 0),
        "supply_cap": int(value[7] or 0),
    }


def _parse_totals_basic(value: Sequence[Any]) -> dict[str, int]:
    return {
        "base_supply_index": int(value[0] or 0),
        "base_borrow_index": int(value[1] or 0),
        "tracking_supply_index": int(value[2] or 0),
        "tracking_borrow_index": int(value[3] or 0),
        "total_supply_base": int(value[4] or 0),
        "total_borrow_base": int(value[5] or 0),
        "last_accrual_time": int(value[6] or 0),
        "pause_flags": int(value[7] or 0),
    }


def _parse_total_collateral(value: Sequence[Any]) -> int:
    return int(value[0] or 0)


def _factor_to_float(raw: int) -> float:
    return raw / FACTOR_SCALE if raw else 0.0


def _price_to_float(raw: int | None) -> float | None:
    if raw is None:
        return None
    return raw / PRICE_SCALE


def _amount_to_decimal(raw: int, decimals: int) -> float:
    if decimals < 0:
        return float(raw)
    return raw / (10**decimals)


def _rate_to_apr(raw_rate: int) -> float:
    if raw_rate <= 0:
        return 0.0
    return (raw_rate / MANTISSA) * SECONDS_PER_YEAR


def _scale_to_decimals(scale: int) -> int | None:
    if scale <= 0:
        return None
    value = scale
    decimals = 0
    while value > 1 and value % 10 == 0:
        value //= 10
        decimals += 1
    if value == 1:
        return decimals
    return None


def _pause_flags_to_dict(flags: int) -> dict[str, bool]:
    return {
        "supply_paused": bool(flags & (1 << 0)),
        "transfer_paused": bool(flags & (1 << 1)),
        "withdraw_paused": bool(flags & (1 << 2)),
        "absorb_paused": bool(flags & (1 << 3)),
        "buy_paused": bool(flags & (1 << 4)),
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
        entry = COMPOUND_COMET_BY_CHAIN.get(chain_id)
        if not entry:
            raise ValueError(f"Unsupported Compound chain_id={chain_id}")
        return entry

    def _list_market_seeds(
        self, chain_id: int | None = None
    ) -> list[CompoundMarketSeed]:
        chain_ids = (
            [chain_id] if chain_id is not None else COMPOUND_COMET_BY_CHAIN.keys()
        )
        seeds: list[CompoundMarketSeed] = []
        for cid in chain_ids:
            entry = self._entry(cid)
            shared_rewards = to_checksum_address(entry["rewards"])
            shared_bulker = to_checksum_address(entry["bulker"])
            configurator = to_checksum_address(entry["configurator"])
            chain_name = entry["chain_name"]
            markets = entry.get("markets") or {}
            for market_name, market_cfg in markets.items():
                seeds.append(
                    CompoundMarketSeed(
                        chain_id=cid,
                        chain_name=chain_name,
                        market_name=market_name,
                        comet=to_checksum_address(market_cfg["comet"]),
                        rewards=shared_rewards,
                        bulker=to_checksum_address(
                            market_cfg.get("bulker") or shared_bulker
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
        cache_key = (chain_id, checksum_token.lower())
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
                symbol=symbol or "",
                name=name or "",
                decimals=decimals,
            )
        except Exception:
            metadata = TokenMetadata(
                address=checksum_token,
                symbol="",
                name="",
                decimals=fallback_decimals if fallback_decimals is not None else 18,
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
        async with web3_from_chain_id(chain_id) as web3:
            contract = web3.eth.contract(
                address=to_checksum_address(comet), abi=COMET_ABI
            )
            onchain_base_token = await contract.functions.baseToken().call(
                block_identifier="latest"
            )
        resolved = to_checksum_address(onchain_base_token)
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
        async with web3_from_chain_id(chain_id) as web3:
            contract = web3.eth.contract(address=checksum_comet, abi=COMET_ABI)
            raw_info = tuple(
                await contract.functions.getAssetInfoByAddress(checksum_asset).call(
                    block_identifier="latest"
                )
            )
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
        web3: Any,
    ) -> dict[str, Any]:
        checksum_rewards = to_checksum_address(rewards_contract)
        checksum_comet = to_checksum_address(comet)
        contract = web3.eth.contract(address=checksum_rewards, abi=COMET_REWARDS_ABI)
        try:
            raw = tuple(
                await contract.functions.rewardConfig(checksum_comet).call(
                    block_identifier="latest"
                )
            )
        except Exception:
            return {
                "token": None,
                "rescale_factor": 0,
                "should_upscale": False,
                "multiplier": 0,
            }
        token = raw[0]
        return {
            "token": (
                None
                if not token or token == ZERO_ADDRESS
                else to_checksum_address(token)
            ),
            "rescale_factor": int(raw[1] or 0),
            "should_upscale": bool(raw[2] or False),
            "multiplier": int(raw[3] or 0),
        }

    async def _get_reward_owed(
        self,
        *,
        chain_id: int,
        rewards_contract: str,
        comet: str,
        account: str,
        configured_reward_token: str | None,
        web3: Any,
    ) -> dict[str, Any]:
        if configured_reward_token is None:
            return {"reward_token": None, "reward_owed": 0, "reward_error": None}

        checksum_rewards = to_checksum_address(rewards_contract)
        checksum_comet = to_checksum_address(comet)
        checksum_account = to_checksum_address(account)
        contract = web3.eth.contract(address=checksum_rewards, abi=COMET_REWARDS_ABI)
        try:
            raw_owed = tuple(
                await contract.functions.getRewardOwed(
                    checksum_comet,
                    checksum_account,
                ).call(block_identifier="pending")
            )
        except Exception as exc:
            return {
                "reward_token": configured_reward_token,
                "reward_owed": 0,
                "reward_error": str(exc),
            }

        reward_token = raw_owed[0]
        reward_owed = int(raw_owed[1] or 0)
        if not reward_token or reward_token == ZERO_ADDRESS:
            reward_token = None
        else:
            reward_token = to_checksum_address(reward_token)
        return {
            "reward_token": reward_token or configured_reward_token,
            "reward_owed": reward_owed,
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
                    Call(comet, "name"),
                    Call(comet, "symbol"),
                    Call(
                        comet,
                        "baseToken",
                        postprocess=to_checksum_address,
                    ),
                    Call(
                        comet,
                        "baseTokenPriceFeed",
                        postprocess=to_checksum_address,
                    ),
                    Call(comet, "baseScale"),
                    Call(comet, "decimals"),
                    Call(comet, "numAssets"),
                    Call(comet, "totalSupply"),
                    Call(comet, "totalBorrow"),
                        Call(
                            comet,
                            "totalsBasic",
                            postprocess=lambda row: _parse_totals_basic(tuple(row)),
                        ),
                    Call(comet, "getUtilization"),
                    Call(comet, "baseBorrowMin"),
                    Call(comet, "baseMinForRewards"),
                    Call(comet, "baseTrackingSupplySpeed"),
                    Call(comet, "baseTrackingBorrowSpeed"),
                    Call(comet, "targetReserves"),
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

            rate_rows, reward_cfg, base_meta = await asyncio.gather(
                read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=seed.chain_id,
                    calls=[
                        Call(
                            comet,
                            "getSupplyRate",
                            args=(utilization,),
                        ),
                        Call(
                            comet,
                            "getBorrowRate",
                            args=(utilization,),
                        ),
                    ],
                    block_identifier="pending",
                ),
                self._reward_config(
                    rewards_contract=seed.rewards,
                    comet=seed.comet,
                    chain_id=seed.chain_id,
                    web3=web3,
                ),
                self._token_metadata(
                    chain_id=seed.chain_id,
                    token_address=base_token,
                    web3=web3,
                    fallback_decimals=base_decimals,
                ),
            )
            supply_rate, borrow_rate = rate_rows

            asset_infos: list[dict[str, Any]] = []
            if num_assets > 0:
                raw_asset_infos = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=seed.chain_id,
                    calls=[
                        Call(
                            comet,
                            "getAssetInfo",
                            args=(i,),
                            postprocess=lambda row: _parse_asset_info(tuple(row)),
                        )
                        for i in range(num_assets)
                    ],
                    block_identifier="latest",
                )
                asset_infos = raw_asset_infos

            total_collateral_rows: list[int] = []
            if asset_infos:
                total_collateral_rows = [0 for _ in asset_infos]

            base_price_raw: int | None = None
            collateral_price_rows: list[int | None] = [None for _ in asset_infos]
            metadata_coros = []
            if reward_cfg["token"] is not None:
                metadata_coros.append(
                    self._token_metadata(
                        chain_id=seed.chain_id,
                        token_address=reward_cfg["token"],
                        web3=web3,
                    )
                )
            metadata_coros.extend(
                self._token_metadata(
                    chain_id=seed.chain_id,
                    token_address=asset_info["asset"],
                    web3=web3,
                    fallback_decimals=_scale_to_decimals(asset_info["scale"]),
                )
                for asset_info in asset_infos
            )

            metadata_rows: list[TokenMetadata] = []
            if asset_infos and include_prices:
                totals_task = read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=seed.chain_id,
                    calls=[
                        Call(
                            comet,
                            "totalsCollateral",
                            args=(info["asset"],),
                            postprocess=lambda row: _parse_total_collateral(tuple(row)),
                        )
                        for info in asset_infos
                    ],
                    block_identifier="pending",
                )
                price_task = read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=seed.chain_id,
                    calls=[
                        Call(
                            comet,
                            "getPrice",
                            args=(base_token_price_feed,),
                        )
                    ]
                    + [
                        Call(
                            comet,
                            "getPrice",
                            args=(info["price_feed"],),
                        )
                        for info in asset_infos
                    ],
                    block_identifier="pending",
                )
                if metadata_coros:
                    totals_raw, price_rows, metadata_rows = await asyncio.gather(
                        totals_task,
                        price_task,
                        asyncio.gather(*metadata_coros),
                    )
                else:
                    totals_raw, price_rows = await asyncio.gather(
                        totals_task,
                        price_task,
                    )
                total_collateral_rows = [value or 0 for value in totals_raw]
                if price_rows:
                    base_price_raw = price_rows[0]
                    collateral_price_rows = price_rows[1:]
            elif asset_infos:
                totals_task = read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=seed.chain_id,
                    calls=[
                        Call(
                            comet,
                            "totalsCollateral",
                            args=(info["asset"],),
                            postprocess=lambda row: _parse_total_collateral(tuple(row)),
                        )
                        for info in asset_infos
                    ],
                    block_identifier="pending",
                )
                if metadata_coros:
                    totals_raw, metadata_rows = await asyncio.gather(
                        totals_task,
                        asyncio.gather(*metadata_coros),
                    )
                else:
                    totals_raw = await totals_task
                total_collateral_rows = [value or 0 for value in totals_raw]
            elif include_prices:
                price_task = comet.functions.getPrice(base_token_price_feed).call(
                    block_identifier="pending"
                )
                if metadata_coros:
                    base_price_raw, metadata_rows = await asyncio.gather(
                        price_task,
                        asyncio.gather(*metadata_coros),
                    )
                else:
                    base_price_raw = await price_task
            elif metadata_coros:
                metadata_rows = await asyncio.gather(*metadata_coros)

            reward_meta: TokenMetadata | None = None
            collateral_metas: list[TokenMetadata] = metadata_rows
            if reward_cfg["token"] is not None:
                reward_meta = metadata_rows[0]
                collateral_metas = metadata_rows[1:]

            collateral_assets: list[dict[str, Any]] = []
            for asset_info, asset_meta, total_supply_asset, price_raw in zip(
                asset_infos,
                collateral_metas,
                total_collateral_rows or [0 for _ in asset_infos],
                collateral_price_rows,
                strict=True,
            ):
                collateral_assets.append(
                    {
                        "asset": asset_meta.address,
                        "symbol": asset_meta.symbol,
                        "name": asset_meta.name,
                        "decimals": asset_meta.decimals,
                        "price_feed": asset_info["price_feed"],
                        "price": price_raw,
                        "price_usd": (
                            _price_to_float(price_raw)
                            if price_raw is not None
                            else None
                        ),
                        "scale": asset_info["scale"],
                        "offset": asset_info["offset"],
                        "borrow_collateral_factor_raw": asset_info[
                            "borrow_collateral_factor_raw"
                        ],
                        "borrow_collateral_factor": _factor_to_float(
                            asset_info["borrow_collateral_factor_raw"]
                        ),
                        "liquidate_collateral_factor_raw": asset_info[
                            "liquidate_collateral_factor_raw"
                        ],
                        "liquidate_collateral_factor": _factor_to_float(
                            asset_info["liquidate_collateral_factor_raw"]
                        ),
                        "liquidation_factor_raw": asset_info["liquidation_factor_raw"],
                        "liquidation_factor": _factor_to_float(
                            asset_info["liquidation_factor_raw"]
                        ),
                        "supply_cap": asset_info["supply_cap"],
                        "total_supply_asset": total_supply_asset,
                    }
                )

        base_supply_apr = _rate_to_apr(supply_rate)
        base_borrow_apr = _rate_to_apr(borrow_rate)

        return {
            "protocol": "compound",
            "chain_id": seed.chain_id,
            "chain_name": seed.chain_name,
            "market_name": seed.market_name,
            "market_key": f"{seed.chain_name}:{seed.market_name}",
            "comet": seed.comet,
            "comet_name": comet_name,
            "comet_symbol": comet_symbol,
            "rewards": seed.rewards,
            "bulker": seed.bulker,
            "configurator": seed.configurator,
            "base_token": base_meta.address,
            "base_token_symbol": base_meta.symbol,
            "base_token_name": base_meta.name,
            "base_token_decimals": base_meta.decimals,
            "base_token_price_feed": base_token_price_feed,
            "base_token_price": base_price_raw,
            "base_token_price_usd": _price_to_float(base_price_raw),
            "base_scale": base_scale,
            "num_assets": num_assets,
            "collateral_assets": collateral_assets,
            "total_supply": total_supply,
            "total_borrow": total_borrow,
            "totals_basic": totals_basic,
            "pause_state": _pause_flags_to_dict(totals_basic["pause_flags"]),
            "utilization": utilization,
            "base_supply_rate": supply_rate,
            "base_borrow_rate": borrow_rate,
            "base_supply_apr": base_supply_apr,
            "base_borrow_apr": base_borrow_apr,
            "base_supply_apy": apr_to_apy(base_supply_apr),
            "base_borrow_apy": apr_to_apy(base_borrow_apr),
            "base_borrow_min": base_borrow_min,
            "base_min_for_rewards": base_min_for_rewards,
            "base_tracking_supply_speed": base_tracking_supply_speed,
            "base_tracking_borrow_speed": base_tracking_borrow_speed,
            "target_reserves": target_reserves,
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
            seed = self._find_market_seed(chain_id=chain_id, comet=comet)
            market = await self._load_market_snapshot(
                seed=seed,
                include_prices=include_prices,
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
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def _load(seed: CompoundMarketSeed) -> tuple[bool, dict[str, Any] | str]:
            async with semaphore:
                try:
                    market = await self._load_market_snapshot(
                        seed=seed,
                        include_prices=include_prices,
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
                market["chain_id"],
                market["market_name"],
                market["comet"].lower(),
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
                chain_id=chain_id,
                comet=comet,
                include_prices=include_prices,
            )
            if not ok:
                return False, (
                    market_or_error
                    if isinstance(market_or_error, str)
                    else "failed to load Compound market"
                )
            if not isinstance(market_or_error, dict):
                return False, "invalid market payload"
            market = market_or_error

            async with web3_from_chain_id(chain_id) as web3:
                comet_contract = web3.eth.contract(
                    address=to_checksum_address(comet),
                    abi=COMET_ABI,
                )
                read_calls = [
                    Call(
                        comet_contract,
                        "balanceOf",
                        args=(checksum_account,),
                    ),
                    Call(
                        comet_contract,
                        "borrowBalanceOf",
                        args=(checksum_account,),
                    ),
                    Call(
                        comet_contract,
                        "baseTrackingAccrued",
                        args=(checksum_account,),
                    ),
                    Call(
                        comet_contract,
                        "isBorrowCollateralized",
                        args=(checksum_account,),
                    ),
                    Call(
                        comet_contract,
                        "isLiquidatable",
                        args=(checksum_account,),
                    ),
                    Call(
                        comet_contract,
                        "userBasic",
                        args=(checksum_account,),
                    ),
                ] + [
                    Call(
                        comet_contract,
                        "collateralBalanceOf",
                        args=(checksum_account, asset["asset"]),
                    )
                    for asset in market.get("collateral_assets") or []
                ]

                rows, reward_read = await asyncio.gather(
                    read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=chain_id,
                        calls=read_calls,
                        block_identifier="pending",
                    ),
                    self._get_reward_owed(
                        chain_id=chain_id,
                        rewards_contract=market["rewards"],
                        comet=market["comet"],
                        account=checksum_account,
                        configured_reward_token=market.get("reward_token"),
                        web3=web3,
                    ),
                )

            supplied_base = rows[0]
            borrowed_base = rows[1]
            base_tracking_accrued = rows[2]
            is_borrow_collateralized = rows[3]
            is_liquidatable = rows[4]
            user_basic_row = tuple(rows[5])
            user_basic = {
                "principal": int(user_basic_row[0] or 0),
                "base_tracking_index": int(user_basic_row[1] or 0),
                "base_tracking_accrued": int(user_basic_row[2] or 0),
                "assets_in": int(user_basic_row[3] or 0),
            }
            collateral_balances = rows[6:]

            reward_decimals = market.get("reward_token_decimals")
            collateral_positions: list[dict[str, Any]] = []
            for asset, balance in zip(
                market.get("collateral_assets") or [],
                collateral_balances,
                strict=True,
            ):
                if not include_zero_collateral and balance == 0:
                    continue
                price_raw = asset.get("price")
                asset_decimals = asset.get("decimals") or 0
                balance_decimal = _amount_to_decimal(balance, asset_decimals)
                price_usd = (
                    _price_to_float(price_raw) if price_raw is not None else None
                )
                usd_value = (
                    balance_decimal * price_usd if price_usd is not None else None
                )
                collateral_positions.append(
                    {
                        "asset": asset["asset"],
                        "symbol": asset.get("symbol") or "",
                        "name": asset.get("name") or "",
                        "decimals": asset_decimals,
                        "balance": balance,
                        "balance_decimal": balance_decimal,
                        "price_feed": asset.get("price_feed") or "",
                        "price": price_raw,
                        "price_usd": price_usd,
                        "usd_value": usd_value,
                        "scale": asset.get("scale") or 0,
                        "borrow_collateral_factor_raw": asset.get(
                            "borrow_collateral_factor_raw"
                        )
                        or 0,
                        "borrow_collateral_factor": asset.get(
                            "borrow_collateral_factor"
                        )
                        or 0.0,
                        "liquidate_collateral_factor_raw": asset.get(
                            "liquidate_collateral_factor_raw"
                        )
                        or 0,
                        "liquidate_collateral_factor": asset.get(
                            "liquidate_collateral_factor"
                        )
                        or 0.0,
                        "liquidation_factor_raw": asset.get("liquidation_factor_raw")
                        or 0,
                        "liquidation_factor": asset.get("liquidation_factor") or 0.0,
                        "supply_cap": asset.get("supply_cap") or 0,
                        "total_supply_asset": asset.get("total_supply_asset") or 0,
                    }
                )

            base_decimals = market.get("base_token_decimals") or 0
            reward_owed = reward_read["reward_owed"]
            return (
                True,
                {
                    "protocol": "compound",
                    "chain_id": chain_id,
                    "chain_name": market["chain_name"],
                    "market_name": market["market_name"],
                    "market_key": market["market_key"],
                    "account": checksum_account,
                    "comet": market["comet"],
                    "base_token": market["base_token"],
                    "base_token_symbol": market.get("base_token_symbol") or "",
                    "base_token_decimals": base_decimals,
                    "supplied_base": supplied_base,
                    "borrowed_base": borrowed_base,
                    "net_base": supplied_base - borrowed_base,
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
                        _amount_to_decimal(reward_owed, reward_decimals)
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
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def _load(seed: CompoundMarketSeed) -> tuple[bool, dict[str, Any] | str]:
            async with semaphore:
                return await self.get_pos(
                    chain_id=seed.chain_id,
                    comet=seed.comet,
                    account=checksum_account,
                    include_prices=include_prices,
                    include_zero_collateral=include_zero_collateral,
                )

        results = await asyncio.gather(*[_load(seed) for seed in seeds])
        positions: list[dict[str, Any]] = []
        errors: list[str] = []
        for ok, payload in results:
            if ok and isinstance(payload, dict):
                has_collateral = any(
                    (item.get("balance") or 0) > 0
                    for item in payload.get("collateral_positions", [])
                )
                has_base = (payload.get("supplied_base") or 0) > 0 or (
                    payload.get("borrowed_base") or 0
                ) > 0
                if include_zero_positions or has_collateral or has_base:
                    positions.append(payload)
            elif isinstance(payload, str):
                errors.append(payload)

        if not positions and errors:
            return False, errors[0]

        positions.sort(
            key=lambda position: (
                position["chain_id"],
                position["market_name"],
                position["comet"].lower(),
            )
        )
        return (
            True,
            {
                "protocol": "compound",
                "account": checksum_account,
                "chain_id": chain_id,
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
        if amount <= 0:
            return False, "amount must be positive"

        try:
            checksum_comet = to_checksum_address(comet)
            checksum_base = await self._resolve_base_token(
                chain_id=chain_id,
                comet=checksum_comet,
                base_token=base_token,
            )

            approved = await ensure_allowance(
                token_address=checksum_base,
                owner=self.wallet_address,
                spender=checksum_comet,
                amount=amount,
                chain_id=chain_id,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=checksum_comet,
                abi=COMET_ABI,
                fn_name="supply",
                args=[checksum_base, amount],
                from_address=self.wallet_address,
                chain_id=chain_id,
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
        if amount <= 0 and not withdraw_full:
            return False, "amount must be positive unless withdraw_full=True"

        try:
            checksum_comet = to_checksum_address(comet)
            checksum_base = await self._resolve_base_token(
                chain_id=chain_id,
                comet=checksum_comet,
                base_token=base_token,
            )
            withdraw_amount = MAX_UINT256 if withdraw_full else amount
            tx = await encode_call(
                target=checksum_comet,
                abi=COMET_ABI,
                fn_name="withdraw",
                args=[checksum_base, withdraw_amount],
                from_address=self.wallet_address,
                chain_id=chain_id,
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
        if amount <= 0:
            return False, "amount must be positive"

        try:
            checksum_comet = to_checksum_address(comet)
            checksum_base, market_result = await asyncio.gather(
                self._resolve_base_token(
                    chain_id=chain_id,
                    comet=checksum_comet,
                    base_token=base_token,
                ),
                self.get_market(
                    chain_id=chain_id,
                    comet=checksum_comet,
                    include_prices=False,
                ),
            )
            ok, market_or_error = market_result
            if not ok:
                return False, (
                    market_or_error
                    if isinstance(market_or_error, str)
                    else "failed to load Compound market"
                )
            if not isinstance(market_or_error, dict):
                return False, "invalid market payload"
            market = market_or_error
            base_borrow_min = market.get("base_borrow_min") or 0
            if base_borrow_min > 0 and amount < base_borrow_min:
                return (
                    False,
                    f"amount must be >= baseBorrowMin ({base_borrow_min}) for comet={checksum_comet}",
                )

            tx = await encode_call(
                target=checksum_comet,
                abi=COMET_ABI,
                fn_name="withdraw",
                args=[checksum_base, amount],
                from_address=self.wallet_address,
                chain_id=chain_id,
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
        if amount <= 0 and not repay_full:
            return False, "amount must be positive unless repay_full=True"

        try:
            checksum_comet = to_checksum_address(comet)
            checksum_base = await self._resolve_base_token(
                chain_id=chain_id,
                comet=checksum_comet,
                base_token=base_token,
            )
            supply_amount = MAX_UINT256 if repay_full else amount
            allowance_amount = MAX_UINT256 if repay_full else amount
            approved = await ensure_allowance(
                token_address=checksum_base,
                owner=self.wallet_address,
                spender=checksum_comet,
                amount=allowance_amount,
                chain_id=chain_id,
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
                chain_id=chain_id,
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
        if amount <= 0:
            return False, "amount must be positive"

        try:
            checksum_comet = to_checksum_address(comet)
            asset_info = await self._get_collateral_asset_info(
                chain_id=chain_id,
                comet=checksum_comet,
                asset=collateral_asset,
            )
            checksum_asset = to_checksum_address(asset_info["asset"])

            approved = await ensure_allowance(
                token_address=checksum_asset,
                owner=self.wallet_address,
                spender=checksum_comet,
                amount=amount,
                chain_id=chain_id,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=checksum_comet,
                abi=COMET_ABI,
                fn_name="supply",
                args=[checksum_asset, amount],
                from_address=self.wallet_address,
                chain_id=chain_id,
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
        if amount <= 0 and not withdraw_full:
            return False, "amount must be positive unless withdraw_full=True"

        try:
            checksum_comet = to_checksum_address(comet)
            asset_info = await self._get_collateral_asset_info(
                chain_id=chain_id,
                comet=checksum_comet,
                asset=collateral_asset,
            )
            checksum_asset = to_checksum_address(asset_info["asset"])
            withdraw_amount = amount

            if withdraw_full:
                async with web3_from_chain_id(chain_id) as web3:
                    contract = web3.eth.contract(address=checksum_comet, abi=COMET_ABI)
                    withdraw_amount = await contract.functions.collateralBalanceOf(
                        self.wallet_address,
                        checksum_asset,
                    ).call(block_identifier="pending")
                if withdraw_amount <= 0:
                    return False, "no collateral balance available to withdraw"

            tx = await encode_call(
                target=checksum_comet,
                abi=COMET_ABI,
                fn_name="withdraw",
                args=[checksum_asset, withdraw_amount],
                from_address=self.wallet_address,
                chain_id=chain_id,
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
            seed = self._find_market_seed(chain_id=chain_id, comet=comet)
            checksum_comet = to_checksum_address(comet)
            checksum_rewards = to_checksum_address(rewards_contract or seed.rewards)
            async with web3_from_chain_id(chain_id) as web3:
                reward_cfg = await self._reward_config(
                    rewards_contract=checksum_rewards,
                    comet=checksum_comet,
                    chain_id=chain_id,
                    web3=web3,
                )
            if reward_cfg["token"] is None:
                return False, f"rewards not configured for comet={checksum_comet}"

            tx = await encode_call(
                target=checksum_rewards,
                abi=COMET_REWARDS_ABI,
                fn_name="claim",
                args=[checksum_comet, self.wallet_address, should_accrue],
                from_address=self.wallet_address,
                chain_id=chain_id,
            )
            txn_hash = await send_transaction(tx, self.sign_callback)
            return True, txn_hash
        except Exception as exc:
            return False, str(exc)
