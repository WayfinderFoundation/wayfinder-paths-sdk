from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from eth_utils import to_checksum_address

import wayfinder_paths.adapters.aerodrome_common as aerodrome_common
from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter, require_wallet
from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.aerodrome_abi import (
    AERODROME_GAUGE_ABI,
    AERODROME_POOL_ABI,
    AERODROME_POOL_FACTORY_ABI,
    AERODROME_REWARDS_DISTRIBUTOR_ABI,
    AERODROME_ROUTER_ABI,
    AERODROME_SUGAR_ABI,
    AERODROME_VOTER_ABI,
    AERODROME_VOTING_ESCROW_ABI,
)
from wayfinder_paths.core.constants.aerodrome_contracts import AERODROME_BY_CHAIN
from wayfinder_paths.core.constants.base import MAX_UINT256
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import BASE_USDC, BASE_WETH
from wayfinder_paths.core.utils.multicall import (
    Call,
    read_only_calls_multicall_or_gather,
)
from wayfinder_paths.core.utils.tokens import (
    ensure_allowance,
    get_erc20_metadata,
    is_native_token,
)
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.uniswap_v3_math import deadline as default_deadline
from wayfinder_paths.core.utils.uniswap_v3_math import slippage_min
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

_SUGAR_CALL_GAS = 30_000_000


@dataclass(frozen=True)
class Route:
    from_token: str
    to_token: str
    stable: bool
    factory: str = ZERO_ADDRESS

    def as_tuple(self) -> tuple[str, str, bool, str]:
        return (
            to_checksum_address(self.from_token),
            to_checksum_address(self.to_token),
            self.stable,
            to_checksum_address(self.factory),
        )


@dataclass(frozen=True)
class SugarReward:
    token: str
    amount: int


@dataclass(frozen=True)
class SugarEpoch:
    ts: int
    lp: str
    votes: int
    emissions: int
    bribes: list[SugarReward]
    fees: list[SugarReward]


@dataclass(frozen=True)
class SugarPool:
    lp: str
    symbol: str
    lp_decimals: int
    lp_total_supply: int
    pool_type: int
    tick: int
    sqrt_ratio: int
    token0: str
    reserve0: int
    staked0: int
    token1: str
    reserve1: int
    staked1: int
    gauge: str
    gauge_liquidity: int
    gauge_alive: bool
    fee: str
    bribe: str
    factory: str
    emissions_per_sec: int
    emissions_token: str
    pool_fee_pips: int
    unstaked_fee_pips: int
    token0_fees: int
    token1_fees: int
    created_at: int

    @property
    def is_cl(self) -> bool:
        return self.pool_type > 0

    @property
    def is_v2(self) -> bool:
        return self.pool_type <= 0

    @property
    def stable(self) -> bool:
        return self.pool_type == 0


class AerodromeAdapter(
    aerodrome_common.AerodromeTokenHelpersMixin,
    aerodrome_common.AerodromeVotingRewardsMixin,
    BaseAdapter,
):
    """
    Aerodrome classic pool/gauge/veAERO adapter (Base mainnet only).

    Mental model:
    - LP positions live at Pool (ERC20 LP token) level; fees can be claimed by unstaked LPs.
    - Staking LP in a Gauge earns emissions; pool fees are redirected to ve voters.
    - veAERO positions are VotingEscrow NFTs; voters earn fees/bribes/rebases.
    """

    adapter_type = "AERODROME"
    chain_id = CHAIN_ID_BASE

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        sign_callback: Callable | None = None,
        wallet_address: str | None = None,
    ) -> None:
        super().__init__("aerodrome_adapter", config or {})
        self.sign_callback = sign_callback

        deployment = AERODROME_BY_CHAIN.get(CHAIN_ID_BASE)
        if not deployment:
            raise ValueError("Aerodrome Base deployment constants missing")

        self.core_contracts: dict[str, str] = deployment

        self.wallet_address: str | None = (
            to_checksum_address(wallet_address) if wallet_address else None
        )
        self._token_decimals_cache: dict[str, int] = {}
        self._token_symbol_cache: dict[str, str] = {}
        self._token_price_usdc_cache: dict[str, tuple[float, float | None]] = {}
        self._sugar_pools_cache: list[SugarPool] | None = None
        self._sugar_pools_by_lp_cache: dict[str, SugarPool] | None = None

    async def get_amounts_out(self, amount_in: int, routes: list[Route]) -> list[int]:
        if amount_in <= 0:
            raise ValueError("amount_in must be positive")
        async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
            router = web3.eth.contract(
                address=self.core_contracts["router"],
                abi=AERODROME_ROUTER_ABI,
            )
            amounts = await router.functions.getAmountsOut(
                amount_in,
                [route.as_tuple() for route in routes],
            ).call(block_identifier="latest")
        return amounts

    async def quote_best_route(
        self,
        *,
        amount_in: int,
        token_in: str,
        token_out: str,
        intermediates: list[str] | None = None,
    ) -> tuple[list[Route], int]:
        token_in = to_checksum_address(token_in)
        token_out = to_checksum_address(token_out)
        if token_in == token_out:
            return [], amount_in

        factory = self.core_contracts["pool_factory"]
        mids = [to_checksum_address(token) for token in (intermediates or [])]

        candidates: list[list[Route]] = [
            [
                Route(
                    from_token=token_in,
                    to_token=token_out,
                    stable=False,
                    factory=factory,
                )
            ],
            [
                Route(
                    from_token=token_in,
                    to_token=token_out,
                    stable=True,
                    factory=factory,
                )
            ],
        ]

        for mid in mids:
            if mid in (token_in, token_out):
                continue
            for stable0 in (False, True):
                for stable1 in (False, True):
                    candidates.append(
                        [
                            Route(
                                from_token=token_in,
                                to_token=mid,
                                stable=stable0,
                                factory=factory,
                            ),
                            Route(
                                from_token=mid,
                                to_token=token_out,
                                stable=stable1,
                                factory=factory,
                            ),
                        ]
                    )

        best_out = 0
        best_routes: list[Route] | None = None
        # Quote route candidates concurrently. Some paths legitimately revert when a
        # hop does not exist, so keep failures isolated per candidate.
        quote_results = await asyncio.gather(
            *[self.get_amounts_out(amount_in, routes) for routes in candidates],
            return_exceptions=True,
        )
        for routes, result in zip(candidates, quote_results, strict=True):
            if isinstance(result, Exception):
                continue
            out = result[-1]
            if out > best_out:
                best_out = out
                best_routes = routes

        if best_routes is None or best_out <= 0:
            raise ValueError("No viable Aerodrome route found")
        return best_routes, best_out

    async def _load_token_metadata(self, token: str) -> tuple[str, int]:
        token = to_checksum_address(token)
        symbol = self._token_symbol_cache.get(token)
        decimals = self._token_decimals_cache.get(token)
        if symbol is not None and decimals is not None:
            return symbol, decimals

        async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
            symbol, _name, decimals = await get_erc20_metadata(token, web3=web3)

        self._token_symbol_cache[token] = symbol
        self._token_decimals_cache[token] = decimals
        return symbol, decimals

    async def _fetch_token_decimals(self, token_addr: str) -> int:
        _symbol, decimals = await self._load_token_metadata(token_addr)
        return decimals

    async def token_symbol(self, token: str) -> str:
        token = to_checksum_address(token)
        if token in self._token_symbol_cache:
            return self._token_symbol_cache[token]
        symbol, _decimals = await self._load_token_metadata(token)
        return symbol

    async def token_price_usdc(self, token: str) -> float | None:
        token = to_checksum_address(token)
        if token == BASE_USDC:
            return 1.0
        now = time.monotonic()
        cached = self._token_price_usdc_cache.get(token)
        if cached is not None:
            cached_at, cached_price = cached
            if (
                now - cached_at
                <= aerodrome_common.AERODROME_TOKEN_PRICE_USDC_TTL_SECONDS
            ):
                return cached_price

        decimals = await self.token_decimals(token)
        try:
            _routes, out = await self.quote_best_route(
                amount_in=10**decimals,
                token_in=token,
                token_out=BASE_USDC,
                intermediates=[BASE_WETH],
            )
        except Exception:
            self._token_price_usdc_cache[token] = (time.monotonic(), None)
            return None

        if out <= 0:
            self._token_price_usdc_cache[token] = (time.monotonic(), None)
            return None

        price = out / 10**6
        self._token_price_usdc_cache[token] = (time.monotonic(), price)
        return price

    async def _resolve_token_price_usdc(
        self,
        token: str,
        *,
        price_usdc: float | None = None,
    ) -> float | None:
        if price_usdc is not None:
            return price_usdc
        return await self.token_price_usdc(token)

    @staticmethod
    def _parse_sugar_rewards(rows: Any) -> list[SugarReward]:
        if not rows:
            return []
        out: list[SugarReward] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            out.append(
                SugarReward(
                    token=to_checksum_address(row[0]),
                    amount=row[1],
                )
            )
        return out

    @classmethod
    def _parse_sugar_epoch(cls, row: Any) -> SugarEpoch:
        if not isinstance(row, (list, tuple)):
            raise TypeError("Sugar epoch row must be a tuple/list")
        if len(row) < 6:
            raise ValueError(f"Unexpected Sugar epoch tuple length: {len(row)}")

        return SugarEpoch(
            ts=row[0],
            lp=to_checksum_address(row[1]),
            votes=row[2],
            emissions=row[3],
            bribes=cls._parse_sugar_rewards(row[4]),
            fees=cls._parse_sugar_rewards(row[5]),
        )

    @staticmethod
    def _parse_sugar_pool(row: Any) -> SugarPool:
        if not isinstance(row, (list, tuple)):
            raise TypeError("Sugar pool row must be a tuple/list")
        if len(row) < 26:
            raise ValueError(f"Unexpected Sugar pool tuple length: {len(row)}")

        return SugarPool(
            lp=to_checksum_address(row[0]),
            symbol=row[1],
            lp_decimals=row[2],
            lp_total_supply=row[3],
            pool_type=row[4],
            tick=row[5],
            sqrt_ratio=row[6],
            token0=to_checksum_address(row[7]),
            reserve0=row[8],
            staked0=row[9],
            token1=to_checksum_address(row[10]),
            reserve1=row[11],
            staked1=row[12],
            gauge=to_checksum_address(row[13]),
            gauge_liquidity=row[14],
            gauge_alive=row[15],
            fee=to_checksum_address(row[16]),
            bribe=to_checksum_address(row[17]),
            factory=to_checksum_address(row[18]),
            emissions_per_sec=row[19],
            emissions_token=to_checksum_address(row[20]),
            pool_fee_pips=row[21],
            unstaked_fee_pips=row[22],
            token0_fees=row[23],
            token1_fees=row[24],
            created_at=row[25],
        )

    async def sugar_all(self, *, limit: int = 500, offset: int = 0) -> list[SugarPool]:
        async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
            sugar = web3.eth.contract(
                address=self.core_contracts["sugar"],
                abi=AERODROME_SUGAR_ABI,
            )
            rows = await sugar.functions.all(limit, offset).call(
                transaction={"gas": _SUGAR_CALL_GAS}, block_identifier="latest"
            )
        return [self._parse_sugar_pool(row) for row in rows]

    async def list_pools(
        self,
        *,
        page_size: int = 500,
        max_pools: int | None = None,
    ) -> list[SugarPool]:
        out: list[SugarPool] = []
        offset = 0
        while True:
            remaining = None if max_pools is None else max(0, max_pools - len(out))
            if remaining is not None and remaining == 0:
                break

            batch_limit = page_size
            if remaining is not None:
                batch_limit = min(batch_limit, remaining)

            try:
                batch = await self.sugar_all(limit=batch_limit, offset=offset)
            except Exception as exc:
                msg = str(exc).lower()
                if (
                    "execution reverted" in msg
                    or "revert" in msg
                    or "out of bounds" in msg
                ):
                    break
                raise

            if not batch:
                break
            out.extend(batch)
            offset += batch_limit

        return out

    async def _ensure_sugar_pools_cache(self) -> list[SugarPool]:
        if self._sugar_pools_cache is None:
            self._sugar_pools_cache = await self.list_pools()
        return self._sugar_pools_cache

    async def pools_by_lp(self) -> dict[str, SugarPool]:
        if self._sugar_pools_by_lp_cache is None:
            pools = await self._ensure_sugar_pools_cache()
            self._sugar_pools_by_lp_cache = {pool.lp: pool for pool in pools}
        return self._sugar_pools_by_lp_cache

    async def sugar_epochs_latest(
        self,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> list[SugarEpoch]:
        async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
            sugar = web3.eth.contract(
                address=self.core_contracts["sugar"],
                abi=AERODROME_SUGAR_ABI,
            )
            rows = await sugar.functions.epochsLatest(limit, offset).call(
                transaction={"gas": _SUGAR_CALL_GAS}, block_identifier="latest"
            )
        return [self._parse_sugar_epoch(row) for row in rows]

    async def sugar_epochs_by_address(
        self,
        *,
        pool: str,
        limit: int = 500,
        offset: int = 0,
    ) -> list[SugarEpoch]:
        pool = to_checksum_address(pool)
        async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
            sugar = web3.eth.contract(
                address=self.core_contracts["sugar"],
                abi=AERODROME_SUGAR_ABI,
            )
            rows = await sugar.functions.epochsByAddress(limit, offset, pool).call(
                transaction={"gas": _SUGAR_CALL_GAS},
                block_identifier="latest",
            )
        return [self._parse_sugar_epoch(row) for row in rows]

    @staticmethod
    def _is_out_of_gas_error(exc: Exception) -> bool:
        return "out of gas" in str(exc).lower()

    async def _latest_epochs_for_ranking(self, *, limit: int) -> list[SugarEpoch]:
        try:
            return await self.sugar_epochs_latest(limit=limit, offset=0)
        except Exception as exc:
            if not self._is_out_of_gas_error(exc):
                raise

        if self._sugar_pools_cache is not None:
            pools = self._sugar_pools_cache[:limit]
        else:
            pools = await self.list_pools(max_pools=limit)

        epochs: list[SugarEpoch] = []
        for i in range(0, len(pools), 25):
            pool_batch = pools[i : i + 25]
            batch_results = await asyncio.gather(
                *[
                    self.sugar_epochs_by_address(
                        pool=pool.lp,
                        limit=1,
                        offset=0,
                    )
                    for pool in pool_batch
                ]
            )
            for rows in batch_results:
                if rows:
                    epochs.append(rows[0])
        return epochs

    async def epoch_total_incentives_usdc(
        self,
        epoch: SugarEpoch,
        *,
        require_all_prices: bool = True,
    ) -> float | None:
        rewards = [*epoch.bribes, *epoch.fees]
        if not rewards:
            return 0.0

        values = await asyncio.gather(
            *[
                self.token_amount_usdc(
                    token=reward.token,
                    amount_raw=reward.amount,
                )
                for reward in rewards
            ]
        )

        total = 0.0
        for value in values:
            if value is None:
                if require_all_prices:
                    return None
                continue
            total += value
        return total

    async def rank_pools_by_usdc_per_ve(
        self,
        *,
        top_n: int = 10,
        limit: int = 1000,
        require_all_prices: bool = True,
    ) -> list[tuple[float, SugarEpoch, float]]:
        epochs = await self._latest_epochs_for_ranking(limit=limit)
        latest_by_lp: dict[str, SugarEpoch] = {}
        for epoch in epochs:
            if epoch.lp not in latest_by_lp:
                latest_by_lp[epoch.lp] = epoch

        ranked: list[tuple[float, SugarEpoch, float]] = []
        epochs_to_rank = [epoch for epoch in latest_by_lp.values() if epoch.votes > 0]
        for i in range(0, len(epochs_to_rank), 50):
            epoch_batch = epochs_to_rank[i : i + 50]
            totals = await asyncio.gather(
                *[
                    self.epoch_total_incentives_usdc(
                        epoch,
                        require_all_prices=require_all_prices,
                    )
                    for epoch in epoch_batch
                ]
            )
            for epoch, total_usdc in zip(epoch_batch, totals, strict=True):
                if total_usdc is None or total_usdc <= 0:
                    continue
                usdc_per_ve = (total_usdc * 1e18) / epoch.votes
                ranked.append((usdc_per_ve, epoch, total_usdc))

        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[: max(1, top_n)]

    async def get_pool(
        self,
        *,
        tokenA: str,
        tokenB: str,
        stable: bool,
    ) -> tuple[bool, Any]:
        try:
            tA = to_checksum_address(tokenA)
            tB = to_checksum_address(tokenB)
            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                factory = web3.eth.contract(
                    address=self.core_contracts["pool_factory"],
                    abi=AERODROME_POOL_FACTORY_ABI,
                )
                pool = await factory.functions.getPool(tA, tB, stable).call(
                    block_identifier="latest"
                )
            pool = to_checksum_address(pool)
            if pool.lower() == ZERO_ADDRESS:
                return False, "Pool does not exist"
            return True, pool
        except Exception as exc:
            return False, str(exc)

    async def get_gauge(
        self,
        *,
        pool: str,
    ) -> tuple[bool, Any]:
        try:
            pool = to_checksum_address(pool)
            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                voter = web3.eth.contract(
                    address=self.core_contracts["voter"],
                    abi=AERODROME_VOTER_ABI,
                )
                gauge = await voter.functions.gauges(pool).call(
                    block_identifier="latest"
                )
            gauge = to_checksum_address(gauge)
            if gauge.lower() == ZERO_ADDRESS:
                return False, "Gauge not found for pool"
            return True, gauge
        except Exception as exc:
            return False, str(exc)

    async def get_all_markets(
        self,
        *,
        start: int = 0,
        limit: int | None = 50,
        include_gauge_state: bool = True,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        """
        Enumerate gauge-enabled pools via Voter.length() + Voter.pools(i).

        Pagination:
        - start: starting index (0-based)
        - limit: max items; set None to fetch all (can be slow)
        """
        try:
            start_i = max(0, start)

            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                voter = web3.eth.contract(
                    address=self.core_contracts["voter"],
                    abi=AERODROME_VOTER_ABI,
                )
                total = await voter.functions.length().call(
                    block_identifier=block_identifier
                )

                if total == 0 or start_i >= total:
                    return True, {
                        "protocol": "aerodrome",
                        "chain_id": CHAIN_ID_BASE,
                        "start": start_i,
                        "limit": limit,
                        "total": total,
                        "markets": [],
                    }

                end_i = total if limit is None else min(total, start_i + limit)

                pool_calls = [
                    Call(
                        voter,
                        "pools",
                        args=(i,),
                        postprocess=lambda a: to_checksum_address(a),
                    )
                    for i in range(start_i, end_i)
                ]
                pools = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=CHAIN_ID_BASE,
                    calls=pool_calls,
                    block_identifier=block_identifier,
                    chunk_size=100,
                )

                pool_contracts = [
                    web3.eth.contract(address=p, abi=AERODROME_POOL_ABI) for p in pools
                ]
                market_state_calls: list[Call] = []
                for pc, pool in zip(pool_contracts, pools, strict=True):
                    market_state_calls.extend(
                        [
                            Call(pc, "metadata"),
                            Call(
                                voter,
                                "gauges",
                                args=(pool,),
                                postprocess=lambda a: to_checksum_address(a),
                            ),
                        ]
                    )
                market_state = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=CHAIN_ID_BASE,
                    calls=market_state_calls,
                    block_identifier=block_identifier,
                    chunk_size=100,
                )
                metadata_list = market_state[0::2]
                gauges = market_state[1::2]

                fees_rewards: list[str] = [ZERO_ADDRESS] * len(gauges)
                bribe_rewards: list[str] = [ZERO_ADDRESS] * len(gauges)
                gauge_reward_tokens: list[str] = [ZERO_ADDRESS] * len(gauges)
                gauge_reward_rates: list[int] = [0] * len(gauges)
                gauge_total_supplies: list[int] = [0] * len(gauges)
                gauge_period_finishes: list[int] = [0] * len(gauges)

                if include_gauge_state:
                    gauges_nonzero = [g for g in gauges if g.lower() != ZERO_ADDRESS]
                    gauge_contracts = [
                        web3.eth.contract(address=g, abi=AERODROME_GAUGE_ABI)
                        for g in gauges_nonzero
                    ]

                    gauge_state_calls: list[Call] = []
                    for g, gc in zip(gauges_nonzero, gauge_contracts, strict=True):
                        gauge_state_calls.extend(
                            [
                                Call(
                                    voter,
                                    "gaugeToFees",
                                    args=(g,),
                                    postprocess=lambda a: to_checksum_address(a),
                                ),
                                Call(
                                    voter,
                                    "gaugeToBribe",
                                    args=(g,),
                                    postprocess=lambda a: to_checksum_address(a),
                                ),
                                Call(
                                    gc,
                                    "rewardToken",
                                    postprocess=lambda a: to_checksum_address(a),
                                ),
                                Call(gc, "rewardRate", postprocess=int),
                                Call(gc, "totalSupply", postprocess=int),
                                Call(gc, "periodFinish", postprocess=int),
                            ]
                        )
                    gauge_state = await read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=CHAIN_ID_BASE,
                        calls=gauge_state_calls,
                        block_identifier=block_identifier,
                        chunk_size=300,
                    )
                    fee_res = gauge_state[0::6]
                    bribe_res = gauge_state[1::6]
                    reward_token_res = gauge_state[2::6]
                    reward_rate_res = gauge_state[3::6]
                    total_supply_res = gauge_state[4::6]
                    period_finish_res = gauge_state[5::6]

                    # Map back to original gauge list indices.
                    j = 0
                    for i, g in enumerate(gauges):
                        if g.lower() == ZERO_ADDRESS:
                            continue
                        fees_rewards[i] = fee_res[j]
                        bribe_rewards[i] = bribe_res[j]
                        gauge_reward_tokens[i] = reward_token_res[j]
                        gauge_reward_rates[i] = reward_rate_res[j]
                        gauge_total_supplies[i] = total_supply_res[j]
                        gauge_period_finishes[i] = period_finish_res[j]
                        j += 1

                markets: list[dict[str, Any]] = []
                for i, (pool, md, gauge) in enumerate(
                    zip(pools, metadata_list, gauges, strict=True)
                ):
                    dec0, dec1, r0, r1, st, t0, t1 = md
                    markets.append(
                        {
                            "pool": to_checksum_address(pool),
                            "stable": st,
                            "token0": to_checksum_address(t0),
                            "token1": to_checksum_address(t1),
                            "decimals0": dec0,
                            "decimals1": dec1,
                            "reserve0": r0,
                            "reserve1": r1,
                            "gauge": to_checksum_address(gauge),
                            "fees_reward": to_checksum_address(fees_rewards[i]),
                            "bribe_reward": to_checksum_address(bribe_rewards[i]),
                            "gauge_reward_token": to_checksum_address(
                                gauge_reward_tokens[i]
                            ),
                            "gauge_reward_rate": gauge_reward_rates[i],
                            "gauge_total_supply": gauge_total_supplies[i],
                            "gauge_period_finish": gauge_period_finishes[i],
                        }
                    )

            return True, {
                "protocol": "aerodrome",
                "chain_id": CHAIN_ID_BASE,
                "start": start_i,
                "limit": limit,
                "total": total,
                "markets": markets,
            }
        except Exception as exc:
            return False, str(exc)

    async def quote_add_liquidity(
        self,
        *,
        tokenA: str | None,
        tokenB: str | None,
        stable: bool,
        amountA_desired: int,
        amountB_desired: int,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        try:
            if amountA_desired <= 0 or amountB_desired <= 0:
                return False, "amounts must be positive"

            tA_native = is_native_token(tokenA)
            tB_native = is_native_token(tokenB)
            if tA_native and tB_native:
                return False, "tokenA and tokenB cannot both be native"

            if tA_native:
                token = to_checksum_address(tokenB)
                token_amt = amountB_desired
                eth_amt = amountA_desired
                tokenA_q, tokenB_q = token, BASE_WETH
                amtA_q, amtB_q = token_amt, eth_amt
            elif tB_native:
                token = to_checksum_address(tokenA)
                token_amt = amountA_desired
                eth_amt = amountB_desired
                tokenA_q, tokenB_q = token, BASE_WETH
                amtA_q, amtB_q = token_amt, eth_amt
            else:
                tokenA_q, tokenB_q = (
                    to_checksum_address(tokenA),
                    to_checksum_address(tokenB),
                )
                amtA_q, amtB_q = amountA_desired, amountB_desired

            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                router = web3.eth.contract(
                    address=self.core_contracts["router"],
                    abi=AERODROME_ROUTER_ABI,
                )
                a, b, liq = await router.functions.quoteAddLiquidity(
                    tokenA_q,
                    tokenB_q,
                    stable,
                    self.core_contracts["pool_factory"],
                    amtA_q,
                    amtB_q,
                ).call(block_identifier=block_identifier)

            if tA_native:
                return True, {
                    "amount_token": a,
                    "amount_eth": b,
                    "liquidity": liq,
                    "token": token,
                }
            if tB_native:
                return True, {
                    "amount_token": a,
                    "amount_eth": b,
                    "liquidity": liq,
                    "token": token,
                }
            return True, {
                "amountA": a,
                "amountB": b,
                "liquidity": liq,
            }
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def add_liquidity(
        self,
        *,
        tokenA: str | None,
        tokenB: str | None,
        stable: bool,
        amountA_desired: int,
        amountB_desired: int,
        slippage_bps: int = 50,
        amountA_min: int | None = None,
        amountB_min: int | None = None,
        to: str | None = None,
        deadline: int | None = None,
    ) -> tuple[bool, Any]:
        """
        Add liquidity (ERC20-ERC20) or (ERC20-ETH) when either token is native.
        """
        if amountA_desired <= 0 or amountB_desired <= 0:
            return False, "amounts must be positive"

        if self.sign_callback is None:
            return False, "sign_callback is required"

        try:
            tA_native = is_native_token(tokenA)
            tB_native = is_native_token(tokenB)
            if tA_native and tB_native:
                return False, "tokenA and tokenB cannot both be native"

            recipient = (
                to_checksum_address(to)
                if to
                else to_checksum_address(self.wallet_address)
            )
            dl = deadline if deadline is not None else default_deadline()

            # ETH path (token + WETH via addLiquidityETH)
            if tA_native or tB_native:
                token = to_checksum_address(tokenB if tA_native else tokenA)
                token_amt = amountB_desired if tA_native else amountA_desired
                eth_amt = amountA_desired if tA_native else amountB_desired

                ok_q, q = await self.quote_add_liquidity(
                    tokenA=token,
                    tokenB=BASE_WETH,
                    stable=stable,
                    amountA_desired=token_amt,
                    amountB_desired=eth_amt,
                )
                if not ok_q:
                    return False, q
                amount_token_q = q["amountA"]
                amount_eth_q = q["amountB"]

                token_min = (
                    amountB_min
                    if (tA_native and amountB_min is not None)
                    else amountA_min
                    if (tB_native and amountA_min is not None)
                    else slippage_min(amount_token_q, slippage_bps)
                )
                eth_min = (
                    amountA_min
                    if (tA_native and amountA_min is not None)
                    else amountB_min
                    if (tB_native and amountB_min is not None)
                    else slippage_min(amount_eth_q, slippage_bps)
                )

                approved = await ensure_allowance(
                    token_address=token,
                    owner=to_checksum_address(self.wallet_address),
                    spender=self.core_contracts["router"],
                    amount=token_amt,
                    chain_id=CHAIN_ID_BASE,
                    signing_callback=self.sign_callback,
                    approval_amount=MAX_UINT256,
                )
                if not approved[0]:
                    return approved

                tx = await encode_call(
                    target=self.core_contracts["router"],
                    abi=AERODROME_ROUTER_ABI,
                    fn_name="addLiquidityETH",
                    args=[
                        token,
                        stable,
                        token_amt,
                        token_min,
                        eth_min,
                        recipient,
                        dl,
                    ],
                    from_address=to_checksum_address(self.wallet_address),
                    chain_id=CHAIN_ID_BASE,
                    value=eth_amt,
                )
                tx_hash = await send_transaction(tx, self.sign_callback)
                return True, tx_hash

            # ERC20-ERC20 path
            tA = to_checksum_address(tokenA)
            tB = to_checksum_address(tokenB)

            ok_q, q = await self.quote_add_liquidity(
                tokenA=tA,
                tokenB=tB,
                stable=stable,
                amountA_desired=amountA_desired,
                amountB_desired=amountB_desired,
            )
            if not ok_q:
                return False, q

            a_q = q["amountA"]
            b_q = q["amountB"]

            a_min = (
                amountA_min
                if amountA_min is not None
                else slippage_min(a_q, slippage_bps)
            )
            b_min = (
                amountB_min
                if amountB_min is not None
                else slippage_min(b_q, slippage_bps)
            )

            approvedA = await ensure_allowance(
                token_address=tA,
                owner=to_checksum_address(self.wallet_address),
                spender=self.core_contracts["router"],
                amount=amountA_desired,
                chain_id=CHAIN_ID_BASE,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approvedA[0]:
                return approvedA

            approvedB = await ensure_allowance(
                token_address=tB,
                owner=to_checksum_address(self.wallet_address),
                spender=self.core_contracts["router"],
                amount=amountB_desired,
                chain_id=CHAIN_ID_BASE,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approvedB[0]:
                return approvedB

            tx = await encode_call(
                target=self.core_contracts["router"],
                abi=AERODROME_ROUTER_ABI,
                fn_name="addLiquidity",
                args=[
                    tA,
                    tB,
                    stable,
                    amountA_desired,
                    amountB_desired,
                    a_min,
                    b_min,
                    recipient,
                    dl,
                ],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=CHAIN_ID_BASE,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    async def quote_remove_liquidity(
        self,
        *,
        tokenA: str | None,
        tokenB: str | None,
        stable: bool,
        liquidity: int,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        try:
            if liquidity <= 0:
                return False, "liquidity must be positive"

            tA_native = is_native_token(tokenA)
            tB_native = is_native_token(tokenB)
            if tA_native and tB_native:
                return False, "tokenA and tokenB cannot both be native"

            if tA_native:
                token = to_checksum_address(tokenB)
                tokenA_q, tokenB_q = token, BASE_WETH
            elif tB_native:
                token = to_checksum_address(tokenA)
                tokenA_q, tokenB_q = token, BASE_WETH
            else:
                tokenA_q, tokenB_q = (
                    to_checksum_address(tokenA),
                    to_checksum_address(tokenB),
                )

            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                router = web3.eth.contract(
                    address=self.core_contracts["router"],
                    abi=AERODROME_ROUTER_ABI,
                )
                a, b = await router.functions.quoteRemoveLiquidity(
                    tokenA_q,
                    tokenB_q,
                    stable,
                    self.core_contracts["pool_factory"],
                    liquidity,
                ).call(block_identifier=block_identifier)

            if tA_native or tB_native:
                return True, {"amount_token": a, "amount_eth": b, "token": token}
            return True, {"amountA": a, "amountB": b}
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def remove_liquidity(
        self,
        *,
        tokenA: str | None,
        tokenB: str | None,
        stable: bool,
        liquidity: int,
        slippage_bps: int = 50,
        amountA_min: int | None = None,
        amountB_min: int | None = None,
        to: str | None = None,
        deadline: int | None = None,
    ) -> tuple[bool, Any]:
        """Remove liquidity (wallet-held LP only)."""
        if liquidity <= 0:
            return False, "liquidity must be positive"
        if self.sign_callback is None:
            return False, "sign_callback is required"

        try:
            tA_native = is_native_token(tokenA)
            tB_native = is_native_token(tokenB)
            if tA_native and tB_native:
                return False, "tokenA and tokenB cannot both be native"

            recipient = (
                to_checksum_address(to)
                if to
                else to_checksum_address(self.wallet_address)
            )
            dl = deadline if deadline is not None else default_deadline()

            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                factory = web3.eth.contract(
                    address=self.core_contracts["pool_factory"],
                    abi=AERODROME_POOL_FACTORY_ABI,
                )

                # Determine pool (LP token) to approve.
                if tA_native or tB_native:
                    token = to_checksum_address(tokenB if tA_native else tokenA)
                    pool = await factory.functions.getPool(
                        token, BASE_WETH, stable
                    ).call(block_identifier="latest")
                else:
                    tA = to_checksum_address(tokenA)
                    tB = to_checksum_address(tokenB)
                    pool = await factory.functions.getPool(tA, tB, stable).call(
                        block_identifier="latest"
                    )

            pool = to_checksum_address(pool)
            if pool.lower() == ZERO_ADDRESS:
                return False, "Pool does not exist"

            approved = await ensure_allowance(
                token_address=pool,
                owner=to_checksum_address(self.wallet_address),
                spender=self.core_contracts["router"],
                amount=liquidity,
                chain_id=CHAIN_ID_BASE,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            # Compute min amounts using quote.
            if tA_native or tB_native:
                token = to_checksum_address(tokenB if tA_native else tokenA)
                ok_q, q = await self.quote_remove_liquidity(
                    tokenA=token,
                    tokenB=BASE_WETH,
                    stable=stable,
                    liquidity=liquidity,
                )
                if not ok_q:
                    return False, q
                token_min = (
                    amountB_min
                    if (tA_native and amountB_min is not None)
                    else amountA_min
                    if (tB_native and amountA_min is not None)
                    else slippage_min(q["amountA"], slippage_bps)
                )
                eth_min = (
                    amountA_min
                    if (tA_native and amountA_min is not None)
                    else amountB_min
                    if (tB_native and amountB_min is not None)
                    else slippage_min(q["amountB"], slippage_bps)
                )

                tx = await encode_call(
                    target=self.core_contracts["router"],
                    abi=AERODROME_ROUTER_ABI,
                    fn_name="removeLiquidityETH",
                    args=[
                        token,
                        stable,
                        liquidity,
                        token_min,
                        eth_min,
                        recipient,
                        dl,
                    ],
                    from_address=to_checksum_address(self.wallet_address),
                    chain_id=CHAIN_ID_BASE,
                )
                tx_hash = await send_transaction(tx, self.sign_callback)
                return True, tx_hash

            ok_q, q = await self.quote_remove_liquidity(
                tokenA=to_checksum_address(tokenA),
                tokenB=to_checksum_address(tokenB),
                stable=stable,
                liquidity=liquidity,
            )
            if not ok_q:
                return False, q

            a_min = (
                amountA_min
                if amountA_min is not None
                else slippage_min(q["amountA"], slippage_bps)
            )
            b_min = (
                amountB_min
                if amountB_min is not None
                else slippage_min(q["amountB"], slippage_bps)
            )

            tx = await encode_call(
                target=self.core_contracts["router"],
                abi=AERODROME_ROUTER_ABI,
                fn_name="removeLiquidity",
                args=[
                    to_checksum_address(tokenA),
                    to_checksum_address(tokenB),
                    stable,
                    liquidity,
                    a_min,
                    b_min,
                    recipient,
                    dl,
                ],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=CHAIN_ID_BASE,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def claim_pool_fees_unstaked(
        self,
        *,
        pool: str,
    ) -> tuple[bool, Any]:
        """Claim Pool fees for wallet-held LP (unstaked)."""
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            pool = to_checksum_address(pool)
            acct = to_checksum_address(self.wallet_address)

            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                pc = web3.eth.contract(address=pool, abi=AERODROME_POOL_ABI)
                c0, c1 = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=CHAIN_ID_BASE,
                    calls=[
                        Call(pc, "claimable0", args=(acct,)),
                        Call(pc, "claimable1", args=(acct,)),
                    ],
                    block_identifier="pending",
                )

            tx = await encode_call(
                target=pool,
                abi=AERODROME_POOL_ABI,
                fn_name="claimFees",
                args=[],
                from_address=acct,
                chain_id=CHAIN_ID_BASE,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, {"tx": tx_hash, "claimable0": c0, "claimable1": c1}
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def stake_lp(
        self,
        *,
        gauge: str,
        amount: int,
        recipient: str | None = None,
    ) -> tuple[bool, Any]:
        if amount <= 0:
            return False, "amount must be positive"
        if self.sign_callback is None:
            return False, "sign_callback is required"

        try:
            gauge = to_checksum_address(gauge)
            recipient_addr = to_checksum_address(recipient) if recipient else None

            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                voter = web3.eth.contract(
                    address=self.core_contracts["voter"],
                    abi=AERODROME_VOTER_ABI,
                )
                alive = await voter.functions.isAlive(gauge).call(
                    block_identifier="latest"
                )
                if not alive:
                    return False, "Gauge is not alive (killed/dead)"

                g = web3.eth.contract(address=gauge, abi=AERODROME_GAUGE_ABI)
                staking_token = await g.functions.stakingToken().call(
                    block_identifier="latest"
                )

            approved = await ensure_allowance(
                token_address=to_checksum_address(staking_token),
                owner=to_checksum_address(self.wallet_address),
                spender=gauge,
                amount=amount,
                chain_id=CHAIN_ID_BASE,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            fn_name = "deposit"
            args: list[Any]
            if (
                recipient_addr
                and recipient_addr.lower()
                != to_checksum_address(self.wallet_address).lower()
            ):
                args = [amount, recipient_addr]
            else:
                args = [amount]

            tx = await encode_call(
                target=gauge,
                abi=AERODROME_GAUGE_ABI,
                fn_name=fn_name,
                args=args,
                from_address=to_checksum_address(self.wallet_address),
                chain_id=CHAIN_ID_BASE,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def unstake_lp(
        self,
        *,
        gauge: str,
        amount: int,
    ) -> tuple[bool, Any]:
        if amount <= 0:
            return False, "amount must be positive"
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            gauge = to_checksum_address(gauge)
            tx = await encode_call(
                target=gauge,
                abi=AERODROME_GAUGE_ABI,
                fn_name="withdraw",
                args=[amount],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=CHAIN_ID_BASE,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    async def get_full_user_state(
        self,
        *,
        account: str,
        start: int = 0,
        limit: int | None = 200,
        include_votes: bool = False,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        """
        Aggregate wallet LP, staked gauge LP, pending emissions, and veAERO NFTs.

        Notes:
        - Enumerates voteable pools via Voter (paged).
        - For large scans, increase `limit` and page with `start`.
        """
        try:
            acct = to_checksum_address(account)
            start_i = max(0, start)

            async with web3_from_chain_id(CHAIN_ID_BASE) as web3:
                voter = web3.eth.contract(
                    address=self.core_contracts["voter"],
                    abi=AERODROME_VOTER_ABI,
                )
                ve = web3.eth.contract(
                    address=self.core_contracts["voting_escrow"],
                    abi=AERODROME_VOTING_ESCROW_ABI,
                )
                rd = web3.eth.contract(
                    address=self.core_contracts["rewards_distributor"],
                    abi=AERODROME_REWARDS_DISTRIBUTOR_ABI,
                )

                total, ve_balance = await asyncio.gather(
                    voter.functions.length().call(block_identifier=block_identifier),
                    ve.functions.balanceOf(acct).call(
                        block_identifier=block_identifier
                    ),
                )
                end_i = total if limit is None else min(total, start_i + limit)
                if start_i >= total:
                    end_i = start_i

                pool_calls = [
                    Call(voter, "pools", args=(i,), postprocess=to_checksum_address)
                    for i in range(start_i, end_i)
                ]
                pools = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=CHAIN_ID_BASE,
                    calls=pool_calls,
                    block_identifier=block_identifier,
                    chunk_size=100,
                )

                token_ids: list[int] = []
                token_ids_coro = None
                if ve_balance > 0:
                    token_ids_coro = read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=CHAIN_ID_BASE,
                        calls=[
                            Call(
                                ve,
                                "ownerToNFTokenIdList",
                                args=(acct, i),
                            )
                            for i in range(ve_balance)
                        ],
                        block_identifier=block_identifier,
                        chunk_size=100,
                    )

                pool_contracts = [
                    web3.eth.contract(address=p, abi=AERODROME_POOL_ABI) for p in pools
                ]
                pool_state_calls: list[Call] = []
                for pc, pool in zip(pool_contracts, pools, strict=True):
                    pool_state_calls.extend(
                        [
                            Call(pc, "balanceOf", args=(acct,), postprocess=int),
                            Call(
                                voter,
                                "gauges",
                                args=(pool,),
                                postprocess=to_checksum_address,
                            ),
                        ]
                    )
                pool_state_coro = read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=CHAIN_ID_BASE,
                    calls=pool_state_calls,
                    block_identifier=block_identifier,
                    chunk_size=200,
                )
                if token_ids_coro is None:
                    pool_state = await pool_state_coro
                else:
                    token_ids, pool_state = await asyncio.gather(
                        token_ids_coro,
                        pool_state_coro,
                    )
                pool_balances = pool_state[0::2]
                gauges = pool_state[1::2]

                gauge_contracts: dict[str, Any] = {}
                for g in gauges:
                    if g.lower() == ZERO_ADDRESS:
                        continue
                    if g.lower() not in gauge_contracts:
                        gauge_contracts[g.lower()] = web3.eth.contract(
                            address=to_checksum_address(g), abi=AERODROME_GAUGE_ABI
                        )

                gauge_state = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=CHAIN_ID_BASE,
                    calls=[
                        item
                        for g in gauge_contracts.values()
                        for item in (
                            Call(g, "balanceOf", args=(acct,), postprocess=int),
                            Call(g, "earned", args=(acct,), postprocess=int),
                        )
                    ],
                    block_identifier=block_identifier,
                    chunk_size=200,
                )
                g_bal = gauge_state[0::2]
                g_earned = gauge_state[1::2]

                gauge_items: dict[str, dict[str, Any]] = {}
                for (addr_l, contract), bal, earned in zip(
                    gauge_contracts.items(), g_bal, g_earned, strict=True
                ):
                    gauge_items[addr_l] = {
                        "gauge": to_checksum_address(contract.address),
                        "staked_balance": bal,
                        "earned": earned,
                    }

                pools_out: list[dict[str, Any]] = []
                for pool, bal, gauge in zip(pools, pool_balances, gauges, strict=True):
                    pool_addr = to_checksum_address(pool)
                    gauge_addr = to_checksum_address(gauge)
                    pools_out.append(
                        {
                            "pool": pool_addr,
                            "wallet_lp_balance": bal,
                            "gauge": gauge_addr,
                            "gauge_staked_balance": gauge_items.get(
                                gauge_addr.lower(), {}
                            ).get("staked_balance", 0),
                            "gauge_earned": gauge_items.get(gauge_addr.lower(), {}).get(
                                "earned", 0
                            ),
                        }
                    )

                ve_items: list[dict[str, Any]] = []
                if token_ids:
                    ve_state = await read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=CHAIN_ID_BASE,
                        calls=[
                            item
                            for tid in token_ids
                            for item in (
                                Call(ve, "balanceOfNFT", args=(tid,), postprocess=int),
                                Call(ve, "voted", args=(tid,), postprocess=bool),
                                Call(
                                    rd,
                                    "claimable",
                                    args=(tid,),
                                    postprocess=int,
                                ),
                            )
                        ],
                        block_identifier=block_identifier,
                        chunk_size=300,
                    )
                    powers = ve_state[0::3]
                    voted_flags = ve_state[1::3]
                    claimables = ve_state[2::3]

                    votes_by_token: dict[int, dict[str, int]] = {}
                    if include_votes and pools:
                        # Potentially expensive; only enable when needed.
                        vote_calls = []
                        for tid in token_ids:
                            for p in pools:
                                vote_calls.append(
                                    Call(
                                        voter,
                                        "votes",
                                        args=(tid, to_checksum_address(p)),
                                        postprocess=int,
                                    )
                                )
                        vote_values = await read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=CHAIN_ID_BASE,
                            calls=vote_calls,
                            block_identifier=block_identifier,
                            chunk_size=200,
                        )
                        k = 0
                        for tid in token_ids:
                            votes_by_token[tid] = {}
                            for p in pools:
                                votes_by_token[tid][to_checksum_address(p)] = (
                                    vote_values[k]
                                )
                                k += 1

                    for tid, pwr, vflag, cl in zip(
                        token_ids, powers, voted_flags, claimables, strict=True
                    ):
                        item: dict[str, Any] = {
                            "token_id": tid,
                            "voting_power": pwr,
                            "voted": vflag,
                            "rebase_claimable": cl,
                        }
                        if include_votes:
                            item["votes"] = votes_by_token.get(tid, {})
                        ve_items.append(item)

            return True, {
                "protocol": "aerodrome",
                "chain_id": CHAIN_ID_BASE,
                "account": acct,
                "markets_scan": {
                    "start": start_i,
                    "limit": limit,
                    "total": total,
                },
                "lp_positions": pools_out,
                "ve_nfts": ve_items,
            }
        except Exception as exc:
            return False, str(exc)
