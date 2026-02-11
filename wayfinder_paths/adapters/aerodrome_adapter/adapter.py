from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from eth_utils import keccak, to_checksum_address

from wayfinder_paths.adapters.multicall_adapter.adapter import MulticallAdapter
from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.constants.aerodrome import (
    AERODROME_REWARDS_DISTRIBUTOR,
    AERODROME_ROUTER,
    AERODROME_SLIPSTREAM_FACTORY,
    AERODROME_SLIPSTREAM_HELPER,
    AERODROME_SLIPSTREAM_NFPM,
    AERODROME_SLIPSTREAM_QUOTER,
    AERODROME_SUGAR,
    AERODROME_VOTER,
    AERODROME_VOTING_ESCROW,
    BASE_AERO,
)
from wayfinder_paths.core.constants.aerodrome_abi import (
    GAUGE_ABI,
    POOL_FACTORY_ABI,
    REWARDS_DISTRIBUTOR_ABI,
    ROUTER_ABI,
    SLIPSTREAM_CLPOOL_ABI,
    SLIPSTREAM_FACTORY_ABI,
    SLIPSTREAM_GAUGE_ABI,
    SLIPSTREAM_HELPER_ABI,
    SLIPSTREAM_NFPM_ABI,
    SLIPSTREAM_QUOTER_ABI,
    SUGAR_ABI,
    VOTER_ABI,
    VOTING_ESCROW_ABI,
    VOTING_REWARD_ABI,
)
from wayfinder_paths.core.constants.base import MAX_UINT256, SECONDS_PER_YEAR
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import BASE_USDC, BASE_WETH, ZERO_ADDRESS
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
from wayfinder_paths.core.utils.tokens import ensure_allowance, get_token_balance
from wayfinder_paths.core.utils.transaction import (
    encode_call,
    send_transaction,
    wait_for_transaction_receipt,
)
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

VE_MAXTIME_S = 4 * 365 * 24 * 60 * 60  # 4 years
WEEK_S = 7 * 24 * 60 * 60
SLIPSTREAM_SWAP_TOPIC0 = (
    "0x"
    + keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()
)
SLIPSTREAM_TICK_SPACING_CANDIDATES = (1, 5, 10, 20, 50, 100, 200, 500, 1000)


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
            bool(self.stable),
            to_checksum_address(self.factory),
        )


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
        return int(self.pool_type) > 0

    @property
    def is_v2(self) -> bool:
        return int(self.pool_type) <= 0

    @property
    def stable(self) -> bool:
        # Sugar convention (Aerodrome): v2 stable pools are 0, volatile pools are -1
        return int(self.pool_type) == 0


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
class SlipstreamPoolState:
    pool: str
    token0: str
    token1: str
    sqrt_price_x96: int
    tick: int
    tick_spacing: int
    liquidity: int
    fee_pips: int
    unstaked_fee_pips: int


@dataclass(frozen=True)
class SlipstreamRangeMetrics:
    pool: str
    token0: str
    token1: str
    tick_lower: int
    tick_upper: int
    current_tick: int
    in_range: bool
    sqrt_price_x96: int
    price_token1_per_token0: float
    liquidity_total: int
    liquidity_position: int
    share_of_active_liquidity: float
    amount0_now: int
    amount1_now: int
    fee_pips: int
    unstaked_fee_pips: int
    effective_fee_fraction_for_unstaked: float


class AerodromeAdapter(BaseAdapter):
    adapter_type = "AERODROME"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        strategy_wallet_signing_callback: Callable[[dict], Awaitable[Any]]
        | None = None,
    ) -> None:
        super().__init__("aerodrome_adapter", config)
        cfg = config or {}
        self.chain_id = CHAIN_ID_BASE

        self.router = AERODROME_ROUTER
        self.voter = AERODROME_VOTER
        self.ve = AERODROME_VOTING_ESCROW
        self.sugar = AERODROME_SUGAR

        strategy_wallet = cfg.get("strategy_wallet") or {}
        self.strategy_wallet_address = (
            to_checksum_address(strategy_wallet["address"])
            if strategy_wallet.get("address")
            else None
        )
        self.strategy_wallet_signing_callback = strategy_wallet_signing_callback

        self._token_decimals_cache: dict[str, int] = {}
        self._token_symbol_cache: dict[str, str] = {}
        self._token_price_usdc_cache: dict[str, float] = {}
        self._sugar_pools_cache: list[SugarPool] | None = None
        self._sugar_pools_by_lp_cache: dict[str, SugarPool] | None = None
        self._slipstream_tick_spacings_by_pair_cache: dict[
            tuple[str, str], list[int]
        ] = {}

    # -----------------------------
    # Read helpers
    # -----------------------------

    async def default_factory(self) -> str:
        async with web3_from_chain_id(self.chain_id) as web3:
            c = web3.eth.contract(address=self.router, abi=ROUTER_ABI)
            return to_checksum_address(await c.functions.defaultFactory().call())

    async def get_pool(self, token_a: str, token_b: str, stable: bool) -> str:
        token_a = to_checksum_address(token_a)
        token_b = to_checksum_address(token_b)
        factory = await self.default_factory()
        async with web3_from_chain_id(self.chain_id) as web3:
            c = web3.eth.contract(address=factory, abi=POOL_FACTORY_ABI)
            pool = await c.functions.getPool(token_a, token_b, bool(stable)).call()
            return (
                to_checksum_address(pool)
                if pool and int(pool, 16) != 0
                else ZERO_ADDRESS
            )

    async def gauge_for_pool(self, pool: str) -> str:
        pool = to_checksum_address(pool)
        async with web3_from_chain_id(self.chain_id) as web3:
            c = web3.eth.contract(address=self.voter, abi=VOTER_ABI)
            gauge = await c.functions.gauges(pool).call()
            return (
                to_checksum_address(gauge)
                if gauge and int(gauge, 16) != 0
                else ZERO_ADDRESS
            )

    async def get_amounts_out(self, amount_in: int, routes: list[Route]) -> list[int]:
        amount_in = int(amount_in)
        async with web3_from_chain_id(self.chain_id) as web3:
            c = web3.eth.contract(address=self.router, abi=ROUTER_ABI)
            route_tuples = [r.as_tuple() for r in routes]
            amounts = await c.functions.getAmountsOut(amount_in, route_tuples).call()
            return [int(a) for a in amounts]

    async def choose_best_single_hop_route(
        self, amount_in: int, token_in: str, token_out: str
    ) -> Route:
        token_in = to_checksum_address(token_in)
        token_out = to_checksum_address(token_out)
        best: tuple[int, Route] | None = None
        for stable in (False, True):
            r = Route(token_in, token_out, stable=stable)
            try:
                out = (await self.get_amounts_out(amount_in, [r]))[-1]
            except Exception:
                continue
            if out <= 0:
                continue
            if best is None or out > best[0]:
                best = (out, r)
        if best is None:
            raise ValueError("No viable Aerodrome single-hop route found")
        return best[1]

    async def token_decimals(self, token: str) -> int:
        token = to_checksum_address(token)
        if token in self._token_decimals_cache:
            return self._token_decimals_cache[token]
        async with web3_from_chain_id(self.chain_id) as web3:
            c = web3.eth.contract(address=token, abi=ERC20_ABI)
            d = int(await c.functions.decimals().call())
        self._token_decimals_cache[token] = d
        return d

    async def token_symbol(self, token: str) -> str:
        token = to_checksum_address(token)
        if token in self._token_symbol_cache:
            return self._token_symbol_cache[token]
        async with web3_from_chain_id(self.chain_id) as web3:
            c = web3.eth.contract(address=token, abi=ERC20_ABI)
            s = str(await c.functions.symbol().call())
        self._token_symbol_cache[token] = s
        return s

    async def quote_best_route(
        self,
        *,
        amount_in: int,
        token_in: str,
        token_out: str,
        intermediates: list[str] | None = None,
    ) -> tuple[list[Route], int]:
        amount_in = int(amount_in)
        token_in = to_checksum_address(token_in)
        token_out = to_checksum_address(token_out)
        if token_in == token_out:
            return ([], amount_in)

        mids = [to_checksum_address(t) for t in (intermediates or [])]

        candidates: list[list[Route]] = [
            [Route(token_in, token_out, stable=False)],
            [Route(token_in, token_out, stable=True)],
        ]

        for mid in mids:
            if mid in (token_in, token_out):
                continue
            for s1 in (False, True):
                for s2 in (False, True):
                    candidates.append(
                        [
                            Route(token_in, mid, stable=s1),
                            Route(mid, token_out, stable=s2),
                        ]
                    )

        best_out = 0
        best_routes: list[Route] | None = None
        for routes in candidates:
            try:
                out = (await self.get_amounts_out(amount_in, routes))[-1]
            except Exception:
                continue
            if out > best_out:
                best_out = out
                best_routes = routes

        if not best_routes or best_out <= 0:
            raise ValueError("No viable Aerodrome route found")
        return best_routes, int(best_out)

    async def token_price_usdc(self, token: str) -> float:
        token = to_checksum_address(token)
        if token == BASE_USDC:
            return 1.0
        if token in self._token_price_usdc_cache:
            return self._token_price_usdc_cache[token]

        dec = await self.token_decimals(token)
        amount_in = 10**dec
        out: int | None = None
        try:
            _, out = await self.quote_best_route(
                amount_in=amount_in,
                token_in=token,
                token_out=BASE_USDC,
                intermediates=[BASE_WETH],
            )
        except Exception:
            out = None

        if out is None or out <= 0:
            try:
                out = await self._slipstream_quote_to_usdc(
                    amount_in=amount_in, token=token
                )
            except Exception:
                out = None

        if out is None or out <= 0:
            self._token_price_usdc_cache[token] = float("nan")
            return float("nan")

        usdc_price = out / 10**6
        self._token_price_usdc_cache[token] = float(usdc_price)
        return float(usdc_price)

    async def _ensure_sugar_pools_cache(self) -> list[SugarPool]:
        if self._sugar_pools_cache is None:
            self._sugar_pools_cache = await self.list_pools()
        return self._sugar_pools_cache

    async def pools_by_lp(self) -> dict[str, SugarPool]:
        if self._sugar_pools_by_lp_cache is None:
            pools = await self._ensure_sugar_pools_cache()
            self._sugar_pools_by_lp_cache = {p.lp: p for p in pools}
        return self._sugar_pools_by_lp_cache

    async def _slipstream_tick_spacings_for_pair(
        self, *, token_a: str, token_b: str
    ) -> list[int]:
        token_a = to_checksum_address(token_a)
        token_b = to_checksum_address(token_b)
        key = (token_a, token_b) if token_a < token_b else (token_b, token_a)
        if key in self._slipstream_tick_spacings_by_pair_cache:
            return self._slipstream_tick_spacings_by_pair_cache[key]

        async with web3_from_chain_id(self.chain_id) as web3:
            factory = web3.eth.contract(
                address=AERODROME_SLIPSTREAM_FACTORY, abi=SLIPSTREAM_FACTORY_ABI
            )
            tick_spacings: list[int] = []
            for ts in SLIPSTREAM_TICK_SPACING_CANDIDATES:
                try:
                    pool = await factory.functions.getPool(
                        token_a, token_b, int(ts)
                    ).call()
                except Exception:
                    continue
                if pool and int(pool, 16) != 0:
                    tick_spacings.append(int(ts))

        tick_spacings = sorted(set(tick_spacings))
        self._slipstream_tick_spacings_by_pair_cache[key] = tick_spacings
        return tick_spacings

    async def slipstream_best_pool_for_pair(self, *, token_a: str, token_b: str) -> str:
        """Return the Slipstream CL pool (for any tickSpacing) with highest liquidity."""
        token_a = to_checksum_address(token_a)
        token_b = to_checksum_address(token_b)

        tick_spacings = await self._slipstream_tick_spacings_for_pair(
            token_a=token_a, token_b=token_b
        )
        if not tick_spacings:
            raise ValueError("No Slipstream pool found for pair (no tick spacings)")

        best_pool: str | None = None
        best_liquidity = -1

        async with web3_from_chain_id(self.chain_id) as web3:
            factory = web3.eth.contract(
                address=AERODROME_SLIPSTREAM_FACTORY, abi=SLIPSTREAM_FACTORY_ABI
            )
            for ts in tick_spacings:
                try:
                    pool = await factory.functions.getPool(
                        token_a, token_b, int(ts)
                    ).call()
                except Exception:
                    continue
                if not pool or int(pool, 16) == 0:
                    continue
                pool_cs = to_checksum_address(pool)
                try:
                    st = await self._slipstream_pool_state_with_web3(
                        pool=pool_cs, web3=web3
                    )
                except Exception:
                    continue
                liq = int(st.liquidity)
                if liq > best_liquidity:
                    best_liquidity = liq
                    best_pool = pool_cs

        if best_pool is None or best_liquidity <= 0:
            raise ValueError("Slipstream pools exist but none have liquidity > 0")
        return best_pool

    async def _slipstream_quote_exact_input_single(
        self,
        *,
        token_in: str,
        token_out: str,
        tick_spacing: int,
        amount_in: int,
    ) -> int:
        token_in = to_checksum_address(token_in)
        token_out = to_checksum_address(token_out)
        tick_spacing = int(tick_spacing)
        async with web3_from_chain_id(self.chain_id) as web3:
            c = web3.eth.contract(
                address=AERODROME_SLIPSTREAM_QUOTER, abi=SLIPSTREAM_QUOTER_ABI
            )
            # Use quoteExactInput with a single-hop path because quoteExactInputSingle
            # signatures differ across deployments (int24 vs uint24 tickSpacing, v1 vs v2).
            ts = tick_spacing if tick_spacing >= 0 else (1 << 24) + tick_spacing
            path = (
                bytes.fromhex(token_in[2:])
                + ts.to_bytes(3, "big")
                + bytes.fromhex(token_out[2:])
            )
            out = await c.functions.quoteExactInput(path, int(amount_in)).call()
            return int(out)

    async def _slipstream_quote_best_single_hop(
        self,
        *,
        token_in: str,
        token_out: str,
        amount_in: int,
    ) -> int | None:
        amount_in = int(amount_in)
        if amount_in <= 0:
            return None

        token_in = to_checksum_address(token_in)
        token_out = to_checksum_address(token_out)
        tick_spacings = await self._slipstream_tick_spacings_for_pair(
            token_a=token_in, token_b=token_out
        )
        if not tick_spacings:
            return None

        best = 0
        for ts in tick_spacings:
            try:
                out = await self._slipstream_quote_exact_input_single(
                    token_in=token_in,
                    token_out=token_out,
                    tick_spacing=int(ts),
                    amount_in=amount_in,
                )
            except Exception:
                continue
            if int(out) > best:
                best = int(out)
        return best if best > 0 else None

    async def _slipstream_quote_to_usdc(
        self, *, amount_in: int, token: str
    ) -> int | None:
        token = to_checksum_address(token)
        amount_in = int(amount_in)
        if amount_in <= 0:
            return None
        if token == BASE_USDC:
            return amount_in

        direct = await self._slipstream_quote_best_single_hop(
            token_in=token, token_out=BASE_USDC, amount_in=amount_in
        )
        if direct is not None and direct > 0:
            return int(direct)

        # Try via WETH.
        to_weth = await self._slipstream_quote_best_single_hop(
            token_in=token, token_out=BASE_WETH, amount_in=amount_in
        )
        if to_weth is None or to_weth <= 0:
            return None

        try:
            _, weth_to_usdc = await self.quote_best_route(
                amount_in=int(to_weth),
                token_in=BASE_WETH,
                token_out=BASE_USDC,
                intermediates=[],
            )
            if weth_to_usdc and int(weth_to_usdc) > 0:
                return int(weth_to_usdc)
        except Exception:
            # Pathfinding can fail when v2 routes are temporarily unavailable;
            # fall back to slipstream-only quoting below.
            pass

        weth_direct = await self._slipstream_quote_best_single_hop(
            token_in=BASE_WETH, token_out=BASE_USDC, amount_in=int(to_weth)
        )
        return int(weth_direct) if weth_direct and int(weth_direct) > 0 else None

    async def ve_balance_of_nft(self, token_id: int) -> int:
        async with web3_from_chain_id(self.chain_id) as web3:
            c = web3.eth.contract(address=self.ve, abi=VOTING_ESCROW_ABI)
            return int(await c.functions.balanceOfNFT(int(token_id)).call())

    async def ve_locked(self, token_id: int) -> tuple[int, int, bool]:
        async with web3_from_chain_id(self.chain_id) as web3:
            c = web3.eth.contract(address=self.ve, abi=VOTING_ESCROW_ABI)
            locked = await c.functions.locked(int(token_id)).call()
            tup = (
                locked[0]
                if isinstance(locked, (list, tuple)) and len(locked) == 1
                else locked
            )
            return int(tup[0]), int(tup[1]), bool(tup[2])

    # -----------------------------
    # veAPR computations (fees + bribes)
    # -----------------------------

    @staticmethod
    def _parse_sugar_rewards(rows: Any) -> list[SugarReward]:
        if not rows:
            return []
        out: list[SugarReward] = []
        for r in rows:
            if not isinstance(r, (list, tuple)) or len(r) < 2:
                continue
            out.append(
                SugarReward(
                    token=to_checksum_address(r[0]),
                    amount=int(r[1]),
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
            ts=int(row[0]),
            lp=to_checksum_address(row[1]),
            votes=int(row[2]),
            emissions=int(row[3]),
            bribes=cls._parse_sugar_rewards(row[4]),
            fees=cls._parse_sugar_rewards(row[5]),
        )

    async def sugar_epochs_latest(
        self, *, limit: int = 500, offset: int = 0
    ) -> list[SugarEpoch]:
        async with web3_from_chain_id(self.chain_id) as web3:
            c = web3.eth.contract(address=self.sugar, abi=SUGAR_ABI)
            rows = await c.functions.epochsLatest(int(limit), int(offset)).call()
            return [self._parse_sugar_epoch(r) for r in rows]

    async def sugar_epochs_by_address(
        self, *, pool: str, limit: int = 500, offset: int = 0
    ) -> list[SugarEpoch]:
        pool = to_checksum_address(pool)
        async with web3_from_chain_id(self.chain_id) as web3:
            c = web3.eth.contract(address=self.sugar, abi=SUGAR_ABI)
            rows = await c.functions.epochsByAddress(
                int(limit), int(offset), pool
            ).call()
            return [self._parse_sugar_epoch(r) for r in rows]

    async def token_amount_usdc(self, *, token: str, amount_raw: int) -> float | None:
        amount_raw = int(amount_raw)
        if amount_raw == 0:
            return 0.0
        if amount_raw < 0:
            return None

        token = to_checksum_address(token)
        dec = await self.token_decimals(token)
        px = await self.token_price_usdc(token)
        if not math.isfinite(px) or px <= 0:
            return None
        return float((amount_raw / (10**dec)) * px)

    async def epoch_total_incentives_usdc(
        self, epoch: SugarEpoch, *, require_all_prices: bool = True
    ) -> float | None:
        total = 0.0
        for reward in [*epoch.bribes, *epoch.fees]:
            val = await self.token_amount_usdc(
                token=reward.token, amount_raw=reward.amount
            )
            if val is None:
                if require_all_prices:
                    return None
                continue
            total += float(val)
        return total

    async def rank_pools_by_usdc_per_ve(
        self,
        *,
        top_n: int = 10,
        limit: int = 1000,
        require_all_prices: bool = True,
    ) -> list[tuple[float, SugarEpoch, float]]:
        epochs = await self.sugar_epochs_latest(limit=int(limit), offset=0)
        latest_by_lp: dict[str, SugarEpoch] = {}
        for ep in epochs:
            if ep.lp not in latest_by_lp:
                latest_by_lp[ep.lp] = ep

        ranked: list[tuple[float, SugarEpoch, float]] = []
        for ep in latest_by_lp.values():
            if ep.votes <= 0:
                continue
            total_usdc = await self.epoch_total_incentives_usdc(
                ep, require_all_prices=require_all_prices
            )
            if total_usdc is None or total_usdc <= 0:
                continue
            usdc_per_ve = (total_usdc * 1e18) / float(ep.votes)
            ranked.append((float(usdc_per_ve), ep, float(total_usdc)))

        ranked.sort(key=lambda x: x[0], reverse=True)
        return ranked[: max(1, int(top_n))]

    async def estimate_votes_for_lock(
        self, *, aero_amount_raw: int, lock_duration_s: int
    ) -> int:
        aero_amount_raw = int(aero_amount_raw)
        lock_duration_s = int(lock_duration_s)
        if aero_amount_raw <= 0 or lock_duration_s <= 0:
            return 0
        return int(aero_amount_raw * lock_duration_s // VE_MAXTIME_S)

    async def estimate_ve_apr_percent(
        self,
        *,
        usdc_per_ve: float,
        votes_raw: int,
        aero_locked_raw: int,
    ) -> float | None:
        votes_raw = int(votes_raw)
        aero_locked_raw = int(aero_locked_raw)
        if votes_raw <= 0 or aero_locked_raw <= 0:
            return None

        aero_px = await self.token_price_usdc(BASE_AERO)
        if not math.isfinite(aero_px) or aero_px <= 0:
            return None
        aero_dec = await self.token_decimals(BASE_AERO)
        locked_value_usdc = (aero_locked_raw / (10**aero_dec)) * aero_px
        if locked_value_usdc <= 0:
            return None

        weekly_reward_usdc = (float(votes_raw) / 1e18) * float(usdc_per_ve)
        return float((weekly_reward_usdc * 52.0 / locked_value_usdc) * 100.0)

    # -----------------------------
    # Slipstream CL analytics
    # -----------------------------

    @staticmethod
    def q96_to_price_token1_per_token0(
        *, sqrt_price_x96: int, decimals0: int, decimals1: int
    ) -> float:
        sp = float(int(sqrt_price_x96)) / float(2**96)
        p_raw = sp * sp
        return float(p_raw * (10 ** (int(decimals0) - int(decimals1))))

    @staticmethod
    def _q96_to_price_token1_per_token0(
        *, sqrt_price_x96: int, decimals0: int, decimals1: int
    ) -> float:
        # Backwards-compatible alias.
        return AerodromeAdapter.q96_to_price_token1_per_token0(
            sqrt_price_x96=sqrt_price_x96,
            decimals0=decimals0,
            decimals1=decimals1,
        )

    @staticmethod
    def floor_tick_to_spacing(tick: int, spacing: int) -> int:
        return (int(tick) // int(spacing)) * int(spacing)

    @staticmethod
    def ceil_tick_to_spacing(tick: int, spacing: int) -> int:
        spacing = int(spacing)
        return int((-(-int(tick) // spacing)) * spacing)

    async def _slipstream_pool_state_with_web3(
        self, *, pool: str, web3: Any
    ) -> SlipstreamPoolState:
        pool = to_checksum_address(pool)
        c = web3.eth.contract(address=pool, abi=SLIPSTREAM_CLPOOL_ABI)
        sqrt_price_x96, tick, *_ = await c.functions.slot0().call()
        token0 = to_checksum_address(await c.functions.token0().call())
        token1 = to_checksum_address(await c.functions.token1().call())
        tick_spacing = int(await c.functions.tickSpacing().call())
        liquidity = int(await c.functions.liquidity().call())
        fee_pips = int(await c.functions.fee().call())
        unstaked_fee_pips = int(await c.functions.unstakedFee().call())

        return SlipstreamPoolState(
            pool=pool,
            token0=token0,
            token1=token1,
            sqrt_price_x96=int(sqrt_price_x96),
            tick=int(tick),
            tick_spacing=int(tick_spacing),
            liquidity=int(liquidity),
            fee_pips=int(fee_pips),
            unstaked_fee_pips=int(unstaked_fee_pips),
        )

    async def slipstream_pool_state(self, *, pool: str) -> SlipstreamPoolState:
        async with web3_from_chain_id(self.chain_id) as web3:
            return await self._slipstream_pool_state_with_web3(pool=pool, web3=web3)

    async def slipstream_range_metrics(
        self,
        *,
        pool: str,
        tick_lower: int,
        tick_upper: int,
        amount0_raw: int,
        amount1_raw: int,
    ) -> SlipstreamRangeMetrics:
        pool_state = await self.slipstream_pool_state(pool=pool)

        if int(tick_lower) >= int(tick_upper):
            raise ValueError("tick_lower must be < tick_upper")
        amount0_raw = int(amount0_raw)
        amount1_raw = int(amount1_raw)
        if amount0_raw < 0 or amount1_raw < 0:
            raise ValueError("amount0_raw and amount1_raw must be non-negative")

        async with web3_from_chain_id(self.chain_id) as web3:
            helper = web3.eth.contract(
                address=AERODROME_SLIPSTREAM_HELPER, abi=SLIPSTREAM_HELPER_ABI
            )
            sqrt_a = await helper.functions.getSqrtRatioAtTick(int(tick_lower)).call()
            sqrt_b = await helper.functions.getSqrtRatioAtTick(int(tick_upper)).call()

        l_pos = self._liquidity_for_amounts(
            sqrt_ratio_x96=int(pool_state.sqrt_price_x96),
            sqrt_ratio_a_x96=int(sqrt_a),
            sqrt_ratio_b_x96=int(sqrt_b),
            amount0=int(amount0_raw),
            amount1=int(amount1_raw),
        )
        amt0_now, amt1_now = self._amounts_for_liquidity(
            sqrt_ratio_x96=int(pool_state.sqrt_price_x96),
            sqrt_ratio_a_x96=int(sqrt_a),
            sqrt_ratio_b_x96=int(sqrt_b),
            liquidity=int(l_pos),
        )

        d0 = await self.token_decimals(pool_state.token0)
        d1 = await self.token_decimals(pool_state.token1)
        price = self._q96_to_price_token1_per_token0(
            sqrt_price_x96=pool_state.sqrt_price_x96,
            decimals0=d0,
            decimals1=d1,
        )

        in_range = int(tick_lower) <= int(pool_state.tick) < int(tick_upper)
        eff_fee = (float(pool_state.fee_pips) / 1e6) * (
            1.0 - float(pool_state.unstaked_fee_pips) / 1e6
        )

        l_total = int(pool_state.liquidity)
        share = float(int(l_pos)) / float(l_total) if l_total > 0 else 0.0

        return SlipstreamRangeMetrics(
            pool=pool_state.pool,
            token0=pool_state.token0,
            token1=pool_state.token1,
            tick_lower=int(tick_lower),
            tick_upper=int(tick_upper),
            current_tick=int(pool_state.tick),
            in_range=bool(in_range),
            sqrt_price_x96=int(pool_state.sqrt_price_x96),
            price_token1_per_token0=float(price),
            liquidity_total=int(l_total),
            liquidity_position=int(l_pos),
            share_of_active_liquidity=float(share),
            amount0_now=int(amt0_now),
            amount1_now=int(amt1_now),
            fee_pips=int(pool_state.fee_pips),
            unstaked_fee_pips=int(pool_state.unstaked_fee_pips),
            effective_fee_fraction_for_unstaked=float(eff_fee),
        )

    @staticmethod
    def _mul_div(a: int, b: int, denom: int) -> int:
        denom = int(denom)
        if denom == 0:
            raise ZeroDivisionError("mul_div denominator is zero")
        return (int(a) * int(b)) // denom

    @classmethod
    def _liquidity_for_amount0(
        cls, *, sqrt_ratio_a_x96: int, sqrt_ratio_b_x96: int, amount0: int
    ) -> int:
        a = int(sqrt_ratio_a_x96)
        b = int(sqrt_ratio_b_x96)
        if a > b:
            a, b = b, a
        intermediate = cls._mul_div(a, b, 2**96)
        return cls._mul_div(int(amount0), intermediate, b - a)

    @classmethod
    def _liquidity_for_amount1(
        cls, *, sqrt_ratio_a_x96: int, sqrt_ratio_b_x96: int, amount1: int
    ) -> int:
        a = int(sqrt_ratio_a_x96)
        b = int(sqrt_ratio_b_x96)
        if a > b:
            a, b = b, a
        return cls._mul_div(int(amount1), 2**96, b - a)

    @classmethod
    def _liquidity_for_amounts(
        cls,
        *,
        sqrt_ratio_x96: int,
        sqrt_ratio_a_x96: int,
        sqrt_ratio_b_x96: int,
        amount0: int,
        amount1: int,
    ) -> int:
        x = int(sqrt_ratio_x96)
        a = int(sqrt_ratio_a_x96)
        b = int(sqrt_ratio_b_x96)
        if a > b:
            a, b = b, a

        if x <= a:
            return cls._liquidity_for_amount0(
                sqrt_ratio_a_x96=a,
                sqrt_ratio_b_x96=b,
                amount0=int(amount0),
            )
        if x < b:
            l0 = cls._liquidity_for_amount0(
                sqrt_ratio_a_x96=x,
                sqrt_ratio_b_x96=b,
                amount0=int(amount0),
            )
            l1 = cls._liquidity_for_amount1(
                sqrt_ratio_a_x96=a,
                sqrt_ratio_b_x96=x,
                amount1=int(amount1),
            )
            return int(min(l0, l1))
        return cls._liquidity_for_amount1(
            sqrt_ratio_a_x96=a,
            sqrt_ratio_b_x96=b,
            amount1=int(amount1),
        )

    @classmethod
    def _amount0_for_liquidity(
        cls, *, sqrt_ratio_a_x96: int, sqrt_ratio_b_x96: int, liquidity: int
    ) -> int:
        a = int(sqrt_ratio_a_x96)
        b = int(sqrt_ratio_b_x96)
        if a > b:
            a, b = b, a
        return cls._mul_div(int(liquidity) << 96, b - a, b) // a

    @classmethod
    def _amount1_for_liquidity(
        cls, *, sqrt_ratio_a_x96: int, sqrt_ratio_b_x96: int, liquidity: int
    ) -> int:
        a = int(sqrt_ratio_a_x96)
        b = int(sqrt_ratio_b_x96)
        if a > b:
            a, b = b, a
        return cls._mul_div(int(liquidity), b - a, 2**96)

    @classmethod
    def _amounts_for_liquidity(
        cls,
        *,
        sqrt_ratio_x96: int,
        sqrt_ratio_a_x96: int,
        sqrt_ratio_b_x96: int,
        liquidity: int,
    ) -> tuple[int, int]:
        x = int(sqrt_ratio_x96)
        a = int(sqrt_ratio_a_x96)
        b = int(sqrt_ratio_b_x96)
        if a > b:
            a, b = b, a

        if x <= a:
            amt0 = cls._amount0_for_liquidity(
                sqrt_ratio_a_x96=a,
                sqrt_ratio_b_x96=b,
                liquidity=int(liquidity),
            )
            return int(amt0), 0

        if x < b:
            amt0 = cls._amount0_for_liquidity(
                sqrt_ratio_a_x96=x,
                sqrt_ratio_b_x96=b,
                liquidity=int(liquidity),
            )
            amt1 = cls._amount1_for_liquidity(
                sqrt_ratio_a_x96=a,
                sqrt_ratio_b_x96=x,
                liquidity=int(liquidity),
            )
            return int(amt0), int(amt1)

        amt1 = cls._amount1_for_liquidity(
            sqrt_ratio_a_x96=a,
            sqrt_ratio_b_x96=b,
            liquidity=int(liquidity),
        )
        return 0, int(amt1)

    async def slipstream_volume_usdc_per_day(
        self,
        *,
        pool: str,
        lookback_blocks: int = 2000,
        max_logs: int = 5000,
    ) -> float | None:
        pool = to_checksum_address(pool)
        lookback_blocks = int(lookback_blocks)
        max_logs = int(max_logs)
        if lookback_blocks <= 0:
            raise ValueError("lookback_blocks must be > 0")
        if max_logs <= 0:
            raise ValueError("max_logs must be > 0")

        state = await self.slipstream_pool_state(pool=pool)
        d0 = await self.token_decimals(state.token0)
        d1 = await self.token_decimals(state.token1)
        px0 = await self.token_price_usdc(state.token0)
        px1 = await self.token_price_usdc(state.token1)

        async with web3_from_chain_id(self.chain_id) as web3:
            latest = int(await web3.eth.block_number)
            from_block = max(0, latest - lookback_blocks)
            to_block = latest

            logs = await self._get_logs_bounded(
                web3,
                from_block=from_block,
                to_block=to_block,
                address=pool,
                topics=[SLIPSTREAM_SWAP_TOPIC0],
                max_logs=max_logs,
            )

            if not logs:
                return 0.0

            block_numbers = [
                int(lg.get("blockNumber"))
                for lg in logs
                if lg.get("blockNumber") is not None
            ]
            if not block_numbers:
                return 0.0
            bn_min = min(block_numbers)
            bn_max = max(block_numbers)
            b0 = await web3.eth.get_block(bn_min)
            b1 = await web3.eth.get_block(bn_max)
            dt = max(1, int(b1["timestamp"]) - int(b0["timestamp"]))

            total = 0.0
            for lg in logs:
                data = lg.get("data")
                if not data:
                    continue
                try:
                    amount0, amount1, *_ = web3.codec.decode(
                        ["int256", "int256", "uint160", "uint128", "int24"], data
                    )
                except Exception:
                    continue

                v0 = float("nan")
                v1 = float("nan")
                if math.isfinite(px0) and px0 > 0:
                    v0 = abs(int(amount0)) / (10**d0) * float(px0)
                if math.isfinite(px1) and px1 > 0:
                    v1 = abs(int(amount1)) / (10**d1) * float(px1)

                if math.isfinite(v0) and math.isfinite(v1):
                    total += max(v0, v1)
                elif math.isfinite(v0):
                    total += v0
                elif math.isfinite(v1):
                    total += v1

        return float(total * 86400.0 / float(dt))

    async def slipstream_fee_apr_percent(
        self,
        *,
        metrics: SlipstreamRangeMetrics,
        volume_usdc_per_day: float,
        expected_in_range_fraction: float = 1.0,
    ) -> float | None:
        vol = float(volume_usdc_per_day)
        if vol <= 0:
            return 0.0

        px0 = await self.token_price_usdc(metrics.token0)
        px1 = await self.token_price_usdc(metrics.token1)
        d0 = await self.token_decimals(metrics.token0)
        d1 = await self.token_decimals(metrics.token1)
        if not (math.isfinite(px0) and math.isfinite(px1)):
            return None

        pos_value_usdc = (metrics.amount0_now / (10**d0)) * px0 + (
            metrics.amount1_now / (10**d1)
        ) * px1
        if pos_value_usdc <= 0:
            return None

        share = float(metrics.share_of_active_liquidity)
        eff_fee = float(metrics.effective_fee_fraction_for_unstaked)
        in_range_mult = float(expected_in_range_fraction)
        if not metrics.in_range:
            # out-of-range -> ~0 fees unless price returns
            in_range_mult = 0.0

        fees_per_day = vol * eff_fee * share * in_range_mult
        return float(fees_per_day * 365.0 / pos_value_usdc * 100.0)

    async def slipstream_sigma_annual_from_swaps(
        self,
        *,
        pool: str,
        lookback_blocks: int = 20_000,
        max_logs: int = 5000,
    ) -> float | None:
        pool = to_checksum_address(pool)
        state = await self.slipstream_pool_state(pool=pool)
        d0 = await self.token_decimals(state.token0)
        d1 = await self.token_decimals(state.token1)

        async with web3_from_chain_id(self.chain_id) as web3:
            latest = int(await web3.eth.block_number)
            from_block = max(0, latest - int(lookback_blocks))
            to_block = latest
            logs = await self._get_logs_bounded(
                web3,
                from_block=from_block,
                to_block=to_block,
                address=pool,
                topics=[SLIPSTREAM_SWAP_TOPIC0],
                max_logs=max_logs,
            )
            if not logs:
                return None

            # Cache timestamps by block number to keep RPC calls bounded.
            ts_cache: dict[int, int] = {}

            prices: list[tuple[int, float]] = []
            for lg in logs:
                data = lg.get("data")
                if not data:
                    continue
                try:
                    _, _, sqrt_p, _, _ = web3.codec.decode(
                        ["int256", "int256", "uint160", "uint128", "int24"], data
                    )
                except Exception:
                    continue
                bn = int(lg.get("blockNumber"))
                if bn not in ts_cache:
                    blk = await web3.eth.get_block(bn)
                    ts_cache[bn] = int(blk["timestamp"])
                ts = ts_cache[bn]
                p = self._q96_to_price_token1_per_token0(
                    sqrt_price_x96=int(sqrt_p),
                    decimals0=d0,
                    decimals1=d1,
                )
                if p > 0:
                    prices.append((ts, float(p)))

        if len(prices) < 5:
            return None

        prices.sort(key=lambda x: x[0])
        sum_r2 = 0.0
        sum_dt = 0.0
        for i in range(1, len(prices)):
            t0, p0 = prices[i - 1]
            t1, p1 = prices[i]
            dt = int(t1) - int(t0)
            if dt <= 0:
                continue
            r = math.log(float(p1) / float(p0))
            sum_r2 += float(r * r)
            sum_dt += float(dt)

        if sum_dt <= 0:
            return None
        sigma_per_s = math.sqrt(sum_r2 / sum_dt)
        return float(sigma_per_s * math.sqrt(SECONDS_PER_YEAR))

    @staticmethod
    async def _get_logs_bounded(
        web3: Any,
        *,
        from_block: int,
        to_block: int,
        address: str,
        topics: list[Any] | None,
        max_logs: int,
        initial_chunk_size: int = 2000,
    ) -> list[Any]:
        """
        Fetch logs while respecting common RPC limits (e.g. "too many results"),
        walking backward from `to_block` until `max_logs` is reached or `from_block`
        is hit.
        """
        from web3.exceptions import Web3RPCError

        if max_logs <= 0:
            return []

        address = to_checksum_address(address)
        topics = topics or []
        from_block = int(from_block)
        to_block = int(to_block)
        if from_block > to_block:
            return []

        chunk = max(1, int(initial_chunk_size))
        cur_to = to_block
        logs: list[Any] = []

        while cur_to >= from_block and len(logs) < max_logs:
            cur_from = max(from_block, cur_to - chunk + 1)
            try:
                batch = await web3.eth.get_logs(
                    {
                        "fromBlock": cur_from,
                        "toBlock": cur_to,
                        "address": address,
                        "topics": topics,
                    }
                )
            except Web3RPCError:
                # Provider refused due to response size; reduce chunk and retry.
                if chunk == 1:
                    raise
                chunk = max(1, chunk // 2)
                continue

            if batch:
                logs.extend(batch)
                # Keep only the most recent logs by blockNumber/logIndex.
                logs.sort(
                    key=lambda lg: (
                        int(lg.get("blockNumber", 0)),
                        int(lg.get("logIndex", 0)),
                    )
                )
                if len(logs) > max_logs:
                    logs = logs[-max_logs:]

            cur_to = cur_from - 1

        return logs

    @staticmethod
    def _phi(x: float) -> float:
        return 0.5 * (1.0 + math.erf(float(x) / math.sqrt(2.0)))

    async def slipstream_prob_in_range_week(
        self,
        *,
        pool: str,
        tick_lower: int,
        tick_upper: int,
        sigma_annual: float,
    ) -> float | None:
        if int(tick_lower) >= int(tick_upper):
            raise ValueError("tick_lower must be < tick_upper")
        sigma = float(sigma_annual)
        if not math.isfinite(sigma) or sigma <= 0:
            return None

        state = await self.slipstream_pool_state(pool=pool)
        d0 = await self.token_decimals(state.token0)
        d1 = await self.token_decimals(state.token1)

        async with web3_from_chain_id(self.chain_id) as web3:
            helper = web3.eth.contract(
                address=AERODROME_SLIPSTREAM_HELPER, abi=SLIPSTREAM_HELPER_ABI
            )
            sqrt_a = await helper.functions.getSqrtRatioAtTick(int(tick_lower)).call()
            sqrt_b = await helper.functions.getSqrtRatioAtTick(int(tick_upper)).call()

        p0 = self._q96_to_price_token1_per_token0(
            sqrt_price_x96=int(state.sqrt_price_x96),
            decimals0=d0,
            decimals1=d1,
        )
        p_low = self._q96_to_price_token1_per_token0(
            sqrt_price_x96=int(sqrt_a),
            decimals0=d0,
            decimals1=d1,
        )
        p_high = self._q96_to_price_token1_per_token0(
            sqrt_price_x96=int(sqrt_b),
            decimals0=d0,
            decimals1=d1,
        )
        if p0 <= 0 or p_low <= 0 or p_high <= 0:
            return None

        t_years = 7.0 / 365.0
        denom = sigma * math.sqrt(t_years)
        if denom <= 0:
            return None

        z1 = math.log(p_low / p0) / denom
        z2 = math.log(p_high / p0) / denom
        return float(max(0.0, min(1.0, self._phi(z2) - self._phi(z1))))

    async def v2_pool_tvl_usdc(self, pool: SugarPool) -> float | None:
        if not pool.is_v2:
            return None

        d0 = await self.token_decimals(pool.token0)
        d1 = await self.token_decimals(pool.token1)
        px0 = await self.token_price_usdc(pool.token0)
        px1 = await self.token_price_usdc(pool.token1)
        if not (math.isfinite(px0) and math.isfinite(px1)):
            return None

        r0 = pool.reserve0 / (10**d0)
        r1 = pool.reserve1 / (10**d1)
        return float(r0 * px0 + r1 * px1)

    async def v2_staked_tvl_usdc(self, pool: SugarPool) -> float | None:
        tvl = await self.v2_pool_tvl_usdc(pool)
        if tvl is None:
            return None
        if pool.lp_total_supply <= 0:
            return None
        ratio = float(pool.gauge_liquidity) / float(pool.lp_total_supply)
        if ratio <= 0:
            return None
        return tvl * min(1.0, ratio)

    async def v2_emissions_apr(self, pool: SugarPool) -> float | None:
        if not pool.is_v2:
            return None
        if not pool.gauge_alive or pool.gauge == ZERO_ADDRESS:
            return None
        if pool.emissions_per_sec <= 0:
            return None

        staked_tvl = await self.v2_staked_tvl_usdc(pool)
        if not staked_tvl or staked_tvl <= 0:
            return None

        reward_dec = await self.token_decimals(pool.emissions_token)
        reward_px = await self.token_price_usdc(pool.emissions_token)
        if not math.isfinite(reward_px) or reward_px <= 0:
            return None

        emissions_per_sec = pool.emissions_per_sec / (10**reward_dec)
        annual_rewards_usdc = emissions_per_sec * SECONDS_PER_YEAR * reward_px
        return float(annual_rewards_usdc / staked_tvl)

    @staticmethod
    def _parse_sugar_pool(row: Any) -> SugarPool:
        if not isinstance(row, (list, tuple)):
            raise TypeError("Sugar pool row must be a tuple/list")
        if len(row) < 26:
            raise ValueError(f"Unexpected Sugar pool tuple length: {len(row)}")

        # Keep this mapping minimal and explicit so we can safely evolve it.
        return SugarPool(
            lp=to_checksum_address(row[0]),
            symbol=str(row[1]),
            lp_decimals=int(row[2]),
            lp_total_supply=int(row[3]),
            pool_type=int(row[4]),
            tick=int(row[5]),
            sqrt_ratio=int(row[6]),
            token0=to_checksum_address(row[7]),
            reserve0=int(row[8]),
            staked0=int(row[9]),
            token1=to_checksum_address(row[10]),
            reserve1=int(row[11]),
            staked1=int(row[12]),
            gauge=to_checksum_address(row[13]),
            gauge_liquidity=int(row[14]),
            gauge_alive=bool(row[15]),
            fee=to_checksum_address(row[16]),
            bribe=to_checksum_address(row[17]),
            factory=to_checksum_address(row[18]),
            emissions_per_sec=int(row[19]),
            emissions_token=to_checksum_address(row[20]),
            pool_fee_pips=int(row[21]),
            unstaked_fee_pips=int(row[22]),
            token0_fees=int(row[23]),
            token1_fees=int(row[24]),
            created_at=int(row[25]),
        )

    async def sugar_all(self, *, limit: int = 500, offset: int = 0) -> list[SugarPool]:
        async with web3_from_chain_id(self.chain_id) as web3:
            c = web3.eth.contract(address=self.sugar, abi=SUGAR_ABI)
            rows = await c.functions.all(int(limit), int(offset)).call()
            return [self._parse_sugar_pool(r) for r in rows]

    async def list_pools(
        self, *, page_size: int = 500, max_pools: int | None = None
    ) -> list[SugarPool]:
        out: list[SugarPool] = []
        offset = 0
        while True:
            remaining = None if max_pools is None else max(0, int(max_pools) - len(out))
            if remaining is not None and remaining == 0:
                break

            batch_limit = int(page_size)
            if remaining is not None:
                batch_limit = min(batch_limit, remaining)

            try:
                batch = await self.sugar_all(limit=batch_limit, offset=offset)
            except Exception as exc:
                # Sugar reverts once offset exceeds its internal max pool count.
                # Only swallow known pagination-end errors; unexpected provider/
                # transport errors should surface to callers.
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

    async def rank_v2_pools_by_emissions_apr(
        self,
        *,
        top_n: int = 10,
        candidate_count: int = 200,
        page_size: int = 500,
    ) -> list[tuple[float, SugarPool]]:
        pools = await self.list_pools(page_size=page_size)
        v2 = [
            p
            for p in pools
            if p.is_v2
            and p.gauge_alive
            and p.gauge != ZERO_ADDRESS
            and p.emissions_per_sec > 0
            and p.gauge_liquidity > 0
            and p.lp_total_supply > 0
            and p.reserve0 > 0
            and p.reserve1 > 0
        ]
        v2.sort(key=lambda p: int(p.emissions_per_sec), reverse=True)
        if candidate_count > 0:
            v2 = v2[: int(candidate_count)]

        ranked: list[tuple[float, SugarPool]] = []
        for p in v2:
            apr = await self.v2_emissions_apr(p)
            if apr is None:
                continue
            ranked.append((apr, p))
        ranked.sort(key=lambda x: x[0], reverse=True)
        return ranked[: max(1, int(top_n))]

    async def get_full_user_state(
        self,
        *,
        account: str | None = None,
        include_zero_positions: bool = False,
        include_usd_values: bool = False,
        include_slipstream: bool = True,
        multicall_chunk_size: int = 250,
    ) -> tuple[bool, dict[str, Any] | str]:
        acct_raw = account or self.strategy_wallet_address
        if not acct_raw:
            return False, "account is required (or set config.strategy_wallet.address)"
        try:
            acct = to_checksum_address(acct_raw)
        except Exception:
            return False, f"invalid account address: {acct_raw}"
        if acct == "0x0000000000000000000000000000000000000000":
            return False, "account is required (or set config.strategy_wallet.address)"

        try:
            pools = await self._ensure_sugar_pools_cache()
            pools_by_lp = {p.lp: p for p in pools}

            transfer_topic0 = (
                "0x" + keccak(text="Transfer(address,address,uint256)").hex()
            )
            voted_topic0 = (
                "0x"
                + keccak(
                    text="Voted(address,address,uint256,uint256,uint256,uint256)"
                ).hex()
            )

            async with web3_from_chain_id(self.chain_id) as web3:
                latest_block = await web3.eth.get_block("latest")
                latest_bn = int(latest_block["number"])
                latest_ts = int(latest_block["timestamp"])

                multicall = MulticallAdapter(chain_id=self.chain_id, web3=web3)

                def iso(ts: int) -> str:
                    return datetime.fromtimestamp(int(ts), UTC).isoformat()

                def topic_addr(addr: str) -> str:
                    raw = addr.lower().removeprefix("0x")
                    return "0x" + raw.rjust(64, "0")

                def topic_uint(value: int) -> str:
                    return "0x" + hex(int(value))[2:].rjust(64, "0")

                async def multicall_uint256(calls: list[Any]) -> list[int]:
                    out: list[int] = []
                    chunk = max(1, int(multicall_chunk_size))
                    for i in range(0, len(calls), chunk):
                        res = await multicall.aggregate(calls[i : i + chunk])
                        out.extend(
                            [
                                MulticallAdapter.decode_uint256(d)
                                for d in res.return_data
                            ]
                        )
                    return out

                async def safe_symbol(token: str) -> str:
                    try:
                        return await self.token_symbol(token)
                    except Exception:
                        t = to_checksum_address(token)
                        return f"{t[:6]}…{t[-4:]}"

                async def safe_decimals(token: str) -> int:
                    try:
                        return await self.token_decimals(token)
                    except Exception:
                        return 18

                async def native_balance_wei() -> int:
                    return int(await web3.eth.get_balance(acct))

                async def erc721_token_ids_received(
                    *, nft: str, owner: str, max_logs: int = 5000
                ) -> list[int]:
                    from web3.exceptions import Web3RPCError

                    nft = to_checksum_address(nft)
                    owner = to_checksum_address(owner)

                    # Fast path: ERC721Enumerable (tokenOfOwnerByIndex), if supported.
                    enumerable_abi = [
                        {
                            "name": "balanceOf",
                            "type": "function",
                            "stateMutability": "view",
                            "inputs": [{"name": "owner", "type": "address"}],
                            "outputs": [{"type": "uint256"}],
                        },
                        {
                            "name": "tokenOfOwnerByIndex",
                            "type": "function",
                            "stateMutability": "view",
                            "inputs": [
                                {"name": "owner", "type": "address"},
                                {"name": "index", "type": "uint256"},
                            ],
                            "outputs": [{"type": "uint256"}],
                        },
                    ]
                    enumerable = web3.eth.contract(address=nft, abi=enumerable_abi)
                    try:
                        owned_count: int | None = int(
                            await asyncio.wait_for(
                                enumerable.functions.balanceOf(owner).call(),
                                timeout=10,
                            )
                        )
                    except Exception:
                        owned_count = None

                    if owned_count == 0:
                        return []

                    if owned_count > 0:
                        try:
                            # Probe support.
                            await asyncio.wait_for(
                                enumerable.functions.tokenOfOwnerByIndex(
                                    owner, 0
                                ).call(),
                                timeout=10,
                            )
                            calls = [
                                multicall.build_call(
                                    nft,
                                    enumerable.encode_abi(
                                        "tokenOfOwnerByIndex", args=[owner, int(i)]
                                    ),
                                )
                                for i in range(int(owned_count))
                            ]
                            ids = await multicall_uint256(calls)
                            return sorted({int(x) for x in ids})
                        except Exception:
                            # Enumerable method unsupported/unstable on some
                            # providers; use log-scan fallback below.
                            pass

                    # Log scan fallback (progressively increase lookback if needed).
                    topics = [transfer_topic0, None, topic_addr(owner)]
                    candidate_ids: set[int] = set()

                    async def add_ids_from_logs(logs: list[Any]) -> None:
                        for lg in logs:
                            tpcs = lg.get("topics") or []
                            if len(tpcs) < 4:
                                continue
                            t3 = tpcs[3]
                            t3_hex = t3.hex() if hasattr(t3, "hex") else str(t3)
                            try:
                                candidate_ids.add(int(t3_hex, 16))
                            except Exception:
                                continue

                    # Try full-range query, but timebox it to avoid hanging providers.
                    try:
                        logs_full = await asyncio.wait_for(
                            web3.eth.get_logs(
                                {
                                    "fromBlock": 0,
                                    "toBlock": latest_bn,
                                    "address": nft,
                                    "topics": topics,
                                }
                            ),
                            timeout=15,
                        )
                        await add_ids_from_logs(list(logs_full))
                        return sorted(candidate_ids)
                    except (Web3RPCError, TimeoutError):
                        # Full-range log query may exceed provider limits; use
                        # bounded backfill scanning below.
                        pass

                    # If we know the owner holds NFTs (balanceOf), scan recent history until we
                    # have at least that many candidates (then caller can ownerOf-filter).
                    expected = max(0, int(owned_count or 0))
                    lookbacks = [500_000, 2_000_000, 8_000_000, latest_bn]
                    for lb in lookbacks:
                        logs = await self._get_logs_bounded(
                            web3,
                            from_block=max(0, latest_bn - int(lb)),
                            to_block=latest_bn,
                            address=nft,
                            topics=topics,
                            max_logs=int(max_logs),
                        )
                        await add_ids_from_logs(list(logs))
                        if expected > 0 and len(candidate_ids) >= expected:
                            break

                    return sorted(candidate_ids)

                async def filter_owned_token_ids(
                    *,
                    nft: str,
                    token_ids: list[int],
                    owner: str,
                    abi: list[dict[str, Any]],
                ) -> list[int]:
                    c = web3.eth.contract(address=to_checksum_address(nft), abi=abi)
                    owned: list[int] = []
                    for tid in token_ids:
                        try:
                            cur = await c.functions.ownerOf(int(tid)).call()
                        except Exception:
                            continue
                        if str(cur).lower() == owner.lower():
                            owned.append(int(tid))
                    return owned

                # -----------------------------
                # Wallet balances
                # -----------------------------
                eth_wei = await native_balance_wei()
                aero_raw = int(
                    await get_token_balance(
                        token_address=BASE_AERO,
                        chain_id=self.chain_id,
                        wallet_address=acct,
                    )
                )
                usdc_raw = int(
                    await get_token_balance(
                        token_address=BASE_USDC,
                        chain_id=self.chain_id,
                        wallet_address=acct,
                    )
                )

                # -----------------------------
                # v2 LP positions (wallet LP + gauge stake)
                # -----------------------------
                lp_calls = [multicall.encode_erc20_balance(p.lp, acct) for p in pools]
                lp_balances = await multicall_uint256(lp_calls)
                lp_balance_by_lp = {
                    pools[i].lp: int(lp_balances[i]) for i in range(len(pools))
                }

                gauge_calls: list[Any] = []
                gauges: list[str] = []
                for p in pools:
                    gauges.append(p.gauge)
                    gauge_c = web3.eth.contract(address=p.gauge, abi=GAUGE_ABI)
                    gauge_calls.append(
                        multicall.build_call(
                            p.gauge, gauge_c.encode_abi("balanceOf", args=[acct])
                        )
                    )
                gauge_balances = await multicall_uint256(gauge_calls)
                gauge_balance_by_gauge = {
                    gauges[i]: int(gauge_balances[i]) for i in range(len(gauges))
                }

                earned_calls: list[Any] = []
                earned_gauges: list[str] = []
                for g in gauges:
                    if gauge_balance_by_gauge.get(g, 0) <= 0:
                        continue
                    gauge_c = web3.eth.contract(address=g, abi=GAUGE_ABI)
                    earned_gauges.append(g)
                    earned_calls.append(
                        multicall.build_call(
                            g, gauge_c.encode_abi("earned", args=[acct])
                        )
                    )
                earned_raw = await multicall_uint256(earned_calls)
                gauge_earned_by_gauge = {
                    earned_gauges[i]: int(earned_raw[i])
                    for i in range(len(earned_gauges))
                }

                lp_positions: list[dict[str, Any]] = []
                for p in pools:
                    lp_bal = int(lp_balance_by_lp.get(p.lp, 0))
                    gauge_bal = int(gauge_balance_by_gauge.get(p.gauge, 0))
                    if not include_zero_positions and lp_bal <= 0 and gauge_bal <= 0:
                        continue

                    token0_symbol = await safe_symbol(p.token0)
                    token1_symbol = await safe_symbol(p.token1)
                    pos: dict[str, Any] = {
                        "pool": p.lp,
                        "symbol": p.symbol,
                        "stable": bool(p.stable),
                        "token0": {"address": p.token0, "symbol": token0_symbol},
                        "token1": {"address": p.token1, "symbol": token1_symbol},
                        "walletLpBalanceRaw": lp_bal,
                        "walletLpDecimals": int(p.lp_decimals),
                        "gauge": p.gauge,
                        "gaugeStakedRaw": gauge_bal,
                        "gaugeEarnedRewardRaw": int(
                            gauge_earned_by_gauge.get(p.gauge, 0)
                        ),
                        "gaugeRewardToken": p.emissions_token,
                    }
                    lp_positions.append(pos)

                # -----------------------------
                # ve locks (veNFTs) + claimables (rebase + bribes/fees)
                # -----------------------------
                ve_received = await erc721_token_ids_received(nft=self.ve, owner=acct)
                ve_token_ids = await filter_owned_token_ids(
                    nft=self.ve,
                    token_ids=ve_received,
                    owner=acct,
                    abi=VOTING_ESCROW_ABI,
                )

                ve_c = web3.eth.contract(address=self.ve, abi=VOTING_ESCROW_ABI)
                voter_c = web3.eth.contract(address=self.voter, abi=VOTER_ABI)
                dist_c = web3.eth.contract(
                    address=AERODROME_REWARDS_DISTRIBUTOR, abi=REWARDS_DISTRIBUTOR_ABI
                )

                reward_tokens_cache: dict[str, list[str]] = {}

                async def reward_tokens(reward_contract: str) -> list[str]:
                    reward_contract = to_checksum_address(reward_contract)
                    if reward_contract in reward_tokens_cache:
                        return reward_tokens_cache[reward_contract]
                    r = web3.eth.contract(
                        address=reward_contract, abi=VOTING_REWARD_ABI
                    )
                    length = int(await r.functions.rewardsListLength().call())
                    tokens: list[str] = []
                    for i in range(length):
                        t = await r.functions.rewards(int(i)).call()
                        tokens.append(to_checksum_address(t))
                    reward_tokens_cache[reward_contract] = tokens
                    return tokens

                async def reward_claimables(
                    *, reward_contract: str, token_id: int
                ) -> list[dict[str, Any]]:
                    reward_contract = to_checksum_address(reward_contract)
                    if reward_contract == ZERO_ADDRESS:
                        return []
                    r = web3.eth.contract(
                        address=reward_contract, abi=VOTING_REWARD_ABI
                    )
                    tokens = await reward_tokens(reward_contract)
                    out: list[dict[str, Any]] = []
                    for t in tokens:
                        amt = int(await r.functions.earned(t, int(token_id)).call())
                        if not include_zero_positions and amt <= 0:
                            continue
                        dec = await safe_decimals(t)
                        sym = await safe_symbol(t)
                        item: dict[str, Any] = {
                            "token": t,
                            "symbol": sym,
                            "amountRaw": amt,
                            "decimals": int(dec),
                            "amount": amt / (10**dec) if dec >= 0 else None,
                        }
                        if include_usd_values:
                            usd = await self.token_amount_usdc(token=t, amount_raw=amt)
                            item["usdValue"] = usd
                        out.append(item)
                    return out

                ve_nfts: list[dict[str, Any]] = []
                for tid in ve_token_ids:
                    locked = await ve_c.functions.locked(int(tid)).call()
                    tup = (
                        locked[0]
                        if isinstance(locked, (list, tuple)) and len(locked) == 1
                        else locked
                    )
                    locked_amount = abs(int(tup[0]))
                    locked_end = int(tup[1])
                    is_perm = bool(tup[2])
                    voting_power = int(
                        await ve_c.functions.balanceOfNFT(int(tid)).call()
                    )
                    last_voted = int(await voter_c.functions.lastVoted(int(tid)).call())
                    used_weight = int(
                        await voter_c.functions.usedWeights(int(tid)).call()
                    )
                    rebase_claimable = int(
                        await dist_c.functions.claimable(int(tid)).call()
                    )

                    # Pools ever voted for by this tokenId, via events.
                    from web3.exceptions import Web3RPCError

                    voted_topics = [voted_topic0, None, None, topic_uint(int(tid))]
                    try:
                        voted_logs = await asyncio.wait_for(
                            web3.eth.get_logs(
                                {
                                    "fromBlock": 0,
                                    "toBlock": latest_bn,
                                    "address": self.voter,
                                    "topics": voted_topics,
                                }
                            ),
                            timeout=15,
                        )
                    except (Web3RPCError, TimeoutError):
                        voted_logs = await self._get_logs_bounded(
                            web3,
                            from_block=max(0, latest_bn - 2_000_000),
                            to_block=latest_bn,
                            address=self.voter,
                            topics=voted_topics,
                            max_logs=2000,
                        )
                    pools_seen: set[str] = set()
                    for lg in voted_logs:
                        topics = lg.get("topics") or []
                        if len(topics) < 3:
                            continue
                        pool_t = topics[2]
                        pool_hex = (
                            pool_t.hex() if hasattr(pool_t, "hex") else str(pool_t)
                        )
                        pools_seen.add(to_checksum_address("0x" + pool_hex[-40:]))

                    active_votes: list[dict[str, Any]] = []
                    for pool in sorted(pools_seen):
                        try:
                            w = int(
                                await voter_c.functions.votes(int(tid), pool).call()
                            )
                        except Exception:
                            continue
                        pinfo = pools_by_lp.get(pool)
                        claimable_fees: list[dict[str, Any]] = []
                        claimable_bribes: list[dict[str, Any]] = []
                        vote_entry: dict[str, Any] = {
                            "pool": pool,
                            "weightRaw": w,
                            "symbol": pinfo.symbol if pinfo else None,
                        }
                        if pinfo:
                            vote_entry["feeReward"] = pinfo.fee
                            vote_entry["bribeReward"] = pinfo.bribe
                            claimable_fees = await reward_claimables(
                                reward_contract=pinfo.fee,
                                token_id=int(tid),
                            )
                            claimable_bribes = await reward_claimables(
                                reward_contract=pinfo.bribe,
                                token_id=int(tid),
                            )
                            vote_entry["claimableFees"] = claimable_fees
                            vote_entry["claimableBribes"] = claimable_bribes

                        if (
                            not include_zero_positions
                            and w <= 0
                            and not claimable_fees
                            and not claimable_bribes
                        ):
                            continue
                        active_votes.append(vote_entry)

                    aero_dec = await self.token_decimals(BASE_AERO)
                    nft_state: dict[str, Any] = {
                        "tokenId": int(tid),
                        "locked": {
                            "amountRaw": locked_amount,
                            "decimals": int(aero_dec),
                            "amount": locked_amount / (10**aero_dec),
                            "end": locked_end,
                            "endIso": iso(locked_end) if locked_end else None,
                            "isPermanent": bool(is_perm),
                        },
                        "votingPowerRaw": voting_power,
                        "votingPower": voting_power / 1e18,
                        "lastVoted": last_voted,
                        "lastVotedIso": iso(last_voted) if last_voted else None,
                        "usedWeightsRaw": used_weight,
                        "rebaseClaimableRaw": rebase_claimable,
                        "rebaseClaimable": rebase_claimable / (10**aero_dec),
                        "votes": active_votes,
                    }
                    ve_nfts.append(nft_state)

                # -----------------------------
                # Slipstream CL positions (NFTs)
                # -----------------------------
                slipstream_positions: list[dict[str, Any]] = []
                if include_slipstream:
                    nfpm_received = await erc721_token_ids_received(
                        nft=AERODROME_SLIPSTREAM_NFPM, owner=acct
                    )
                    nfpm_token_ids = await filter_owned_token_ids(
                        nft=AERODROME_SLIPSTREAM_NFPM,
                        token_ids=nfpm_received,
                        owner=acct,
                        abi=SLIPSTREAM_NFPM_ABI,
                    )

                    nfpm_c = web3.eth.contract(
                        address=AERODROME_SLIPSTREAM_NFPM, abi=SLIPSTREAM_NFPM_ABI
                    )
                    factory_c = web3.eth.contract(
                        address=AERODROME_SLIPSTREAM_FACTORY, abi=SLIPSTREAM_FACTORY_ABI
                    )
                    helper_c = web3.eth.contract(
                        address=AERODROME_SLIPSTREAM_HELPER, abi=SLIPSTREAM_HELPER_ABI
                    )

                    for pos_id in nfpm_token_ids:
                        try:
                            pos = await nfpm_c.functions.positions(int(pos_id)).call()
                        except Exception:
                            continue
                        (
                            _nonce,
                            _operator,
                            token0,
                            token1,
                            tick_spacing,
                            tick_lower,
                            tick_upper,
                            liq,
                            _fg0,
                            _fg1,
                            owed0,
                            owed1,
                        ) = pos

                        token0 = to_checksum_address(token0)
                        token1 = to_checksum_address(token1)
                        pool_addr = await factory_c.functions.getPool(
                            token0, token1, int(tick_spacing)
                        ).call()
                        pool_addr = to_checksum_address(pool_addr)
                        if pool_addr == ZERO_ADDRESS:
                            continue

                        cl = web3.eth.contract(
                            address=pool_addr, abi=SLIPSTREAM_CLPOOL_ABI
                        )
                        sqrt_price_x96, cur_tick, *_ = await cl.functions.slot0().call()

                        sqrt_a = await helper_c.functions.getSqrtRatioAtTick(
                            int(tick_lower)
                        ).call()
                        sqrt_b = await helper_c.functions.getSqrtRatioAtTick(
                            int(tick_upper)
                        ).call()
                        amt0_now, amt1_now = self._amounts_for_liquidity(
                            sqrt_ratio_x96=int(sqrt_price_x96),
                            sqrt_ratio_a_x96=int(sqrt_a),
                            sqrt_ratio_b_x96=int(sqrt_b),
                            liquidity=int(liq),
                        )

                        d0 = await safe_decimals(token0)
                        d1 = await safe_decimals(token1)
                        s0 = await safe_symbol(token0)
                        s1 = await safe_symbol(token1)
                        in_range = int(tick_lower) <= int(cur_tick) < int(tick_upper)

                        slipstream_positions.append(
                            {
                                "tokenId": int(pos_id),
                                "pool": pool_addr,
                                "token0": {"address": token0, "symbol": s0},
                                "token1": {"address": token1, "symbol": s1},
                                "tickSpacing": int(tick_spacing),
                                "tickLower": int(tick_lower),
                                "tickUpper": int(tick_upper),
                                "liquidity": int(liq),
                                "currentTick": int(cur_tick),
                                "inRange": bool(in_range),
                                "amount0NowRaw": int(amt0_now),
                                "amount1NowRaw": int(amt1_now),
                                "tokensOwed0Raw": int(owed0),
                                "tokensOwed1Raw": int(owed1),
                                "decimals0": int(d0),
                                "decimals1": int(d1),
                                "amount0Now": int(amt0_now) / (10**d0),
                                "amount1Now": int(amt1_now) / (10**d1),
                                "tokensOwed0": int(owed0) / (10**d0),
                                "tokensOwed1": int(owed1) / (10**d1),
                            }
                        )

            state: dict[str, Any] = {
                "protocol": "aerodrome",
                "chainId": int(self.chain_id),
                "account": acct,
                "blockNumber": latest_bn,
                "blockTimestamp": latest_ts,
                "blockTimestampIso": iso(latest_ts),
                "wallet": {
                    "nativeBalanceWei": eth_wei,
                    "nativeBalanceEth": eth_wei / 1e18,
                    "tokenBalances": [
                        {
                            "token": BASE_USDC,
                            "symbol": "USDC",
                            "decimals": 6,
                            "balanceRaw": usdc_raw,
                            "balance": usdc_raw / 1e6,
                        },
                        {
                            "token": BASE_AERO,
                            "symbol": "AERO",
                            "decimals": await self.token_decimals(BASE_AERO),
                            "balanceRaw": aero_raw,
                            "balance": aero_raw
                            / (10 ** (await self.token_decimals(BASE_AERO))),
                        },
                    ],
                },
                "lp": {"positions": lp_positions},
                "ve": {"nfts": ve_nfts},
                "slipstream": {"positions": slipstream_positions},
            }
            return True, state
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    # -----------------------------
    # Tx helpers
    # -----------------------------

    def _require_wallet(self) -> str:
        if not self.strategy_wallet_address:
            raise ValueError("config.strategy_wallet.address is required")
        if not self.strategy_wallet_signing_callback:
            raise ValueError("strategy_wallet_signing_callback is required")
        return self.strategy_wallet_address

    def _deadline(self, seconds_from_now: int = 600) -> int:
        return int(time.time()) + int(seconds_from_now)

    @staticmethod
    def _validate_slippage_bps(slippage_bps: int) -> int:
        bps = int(slippage_bps)
        if bps < 0 or bps >= 10_000:
            raise ValueError("slippage_bps must be in [0, 10000)")
        return bps

    async def swap_exact_tokens_for_tokens(
        self,
        *,
        token_in: str,
        token_out: str,
        amount_in: int,
        slippage_bps: int = 100,
        to_address: str | None = None,
        deadline: int | None = None,
    ) -> tuple[str, Route, int]:
        """
        Executes a best-effort single-hop swap on Aerodrome using Router.swapExactTokensForTokens.

        Returns (tx_hash, route_used, amount_out_min).
        """
        strategy = self._require_wallet()
        to_address = to_checksum_address(to_address or strategy)
        deadline = int(deadline or self._deadline())
        slippage_bps_u = self._validate_slippage_bps(slippage_bps)

        amount_in = int(amount_in)
        if amount_in <= 0:
            raise ValueError("amount_in must be positive")

        await ensure_allowance(
            token_address=to_checksum_address(token_in),
            owner=strategy,
            spender=self.router,
            amount=amount_in,
            chain_id=self.chain_id,
            signing_callback=self.strategy_wallet_signing_callback,
            approval_amount=MAX_UINT256,
        )

        route = await self.choose_best_single_hop_route(amount_in, token_in, token_out)
        quoted_out = (await self.get_amounts_out(amount_in, [route]))[-1]
        amount_out_min = int(quoted_out * (10_000 - slippage_bps_u) // 10_000)

        tx = await encode_call(
            target=self.router,
            abi=ROUTER_ABI,
            fn_name="swapExactTokensForTokens",
            args=[
                amount_in,
                amount_out_min,
                [route.as_tuple()],
                to_address,
                deadline,
            ],
            from_address=strategy,
            chain_id=self.chain_id,
        )
        tx_hash = await send_transaction(tx, self.strategy_wallet_signing_callback)
        return tx_hash, route, amount_out_min

    async def swap_exact_tokens_for_tokens_best_route(
        self,
        *,
        token_in: str,
        token_out: str,
        amount_in: int,
        slippage_bps: int = 100,
        intermediates: list[str] | None = None,
        to_address: str | None = None,
        deadline: int | None = None,
    ) -> tuple[str, list[Route], int]:
        """
        Executes a best-effort swap on Aerodrome using Router.swapExactTokensForTokens,
        searching direct + 2-hop routes.

        Returns (tx_hash, routes_used, amount_out_min).
        """
        strategy = self._require_wallet()
        to_address = to_checksum_address(to_address or strategy)
        deadline = int(deadline or self._deadline())
        slippage_bps_u = self._validate_slippage_bps(slippage_bps)

        amount_in = int(amount_in)
        if amount_in <= 0:
            raise ValueError("amount_in must be positive")

        await ensure_allowance(
            token_address=to_checksum_address(token_in),
            owner=strategy,
            spender=self.router,
            amount=amount_in,
            chain_id=self.chain_id,
            signing_callback=self.strategy_wallet_signing_callback,
            approval_amount=MAX_UINT256,
        )

        routes, quoted_out = await self.quote_best_route(
            amount_in=amount_in,
            token_in=token_in,
            token_out=token_out,
            intermediates=intermediates or [BASE_WETH],
        )
        amount_out_min = int(quoted_out * (10_000 - slippage_bps_u) // 10_000)
        route_tuples = [r.as_tuple() for r in routes]

        tx = await encode_call(
            target=self.router,
            abi=ROUTER_ABI,
            fn_name="swapExactTokensForTokens",
            args=[
                amount_in,
                amount_out_min,
                route_tuples,
                to_address,
                deadline,
            ],
            from_address=strategy,
            chain_id=self.chain_id,
        )
        tx_hash = await send_transaction(tx, self.strategy_wallet_signing_callback)
        return tx_hash, routes, amount_out_min

    async def add_liquidity(
        self,
        *,
        token_a: str,
        token_b: str,
        stable: bool,
        amount_a_desired: int,
        amount_b_desired: int,
        amount_a_min: int = 0,
        amount_b_min: int = 0,
        to_address: str | None = None,
        deadline: int | None = None,
    ) -> str:
        strategy = self._require_wallet()
        to_address = to_checksum_address(to_address or strategy)
        deadline = int(deadline or self._deadline())

        token_a = to_checksum_address(token_a)
        token_b = to_checksum_address(token_b)

        amount_a_desired = int(amount_a_desired)
        amount_b_desired = int(amount_b_desired)
        if amount_a_desired <= 0 or amount_b_desired <= 0:
            raise ValueError("amount_a_desired and amount_b_desired must be positive")

        await ensure_allowance(
            token_address=token_a,
            owner=strategy,
            spender=self.router,
            amount=amount_a_desired,
            chain_id=self.chain_id,
            signing_callback=self.strategy_wallet_signing_callback,
            approval_amount=MAX_UINT256,
        )
        await ensure_allowance(
            token_address=token_b,
            owner=strategy,
            spender=self.router,
            amount=amount_b_desired,
            chain_id=self.chain_id,
            signing_callback=self.strategy_wallet_signing_callback,
            approval_amount=MAX_UINT256,
        )

        tx = await encode_call(
            target=self.router,
            abi=ROUTER_ABI,
            fn_name="addLiquidity",
            args=[
                token_a,
                token_b,
                bool(stable),
                amount_a_desired,
                amount_b_desired,
                int(amount_a_min),
                int(amount_b_min),
                to_address,
                deadline,
            ],
            from_address=strategy,
            chain_id=self.chain_id,
        )
        return await send_transaction(tx, self.strategy_wallet_signing_callback)

    async def create_lock(
        self,
        *,
        aero_token: str,
        amount: int,
        lock_duration_s: int,
        wait_for_receipt: bool = True,
    ) -> tuple[str, dict[str, Any] | None]:
        """
        Creates a veNFT by locking `amount` of `aero_token` into VotingEscrow.

        Returns (tx_hash, receipt_or_none).
        """
        strategy = self._require_wallet()
        aero_token = to_checksum_address(aero_token)
        amount = int(amount)
        if amount <= 0:
            raise ValueError("amount must be positive")
        lock_duration_s = int(lock_duration_s)
        if lock_duration_s <= 0:
            raise ValueError("lock_duration_s must be positive")

        await ensure_allowance(
            token_address=aero_token,
            owner=strategy,
            spender=self.ve,
            amount=amount,
            chain_id=self.chain_id,
            signing_callback=self.strategy_wallet_signing_callback,
            approval_amount=MAX_UINT256,
        )

        tx = await encode_call(
            target=self.ve,
            abi=VOTING_ESCROW_ABI,
            fn_name="createLock",
            args=[amount, lock_duration_s],
            from_address=strategy,
            chain_id=self.chain_id,
        )
        tx_hash = await send_transaction(
            tx, self.strategy_wallet_signing_callback, wait_for_receipt=False
        )
        if not wait_for_receipt:
            return tx_hash, None
        receipt = await wait_for_transaction_receipt(self.chain_id, tx_hash)
        return tx_hash, receipt

    async def vote(
        self,
        *,
        token_id: int,
        pools: list[str],
        weights: list[int],
    ) -> str:
        strategy = self._require_wallet()
        if not pools or not weights or len(pools) != len(weights):
            raise ValueError("pools and weights must be same non-empty length")
        pools_cs = [to_checksum_address(p) for p in pools]
        weights_u = [int(w) for w in weights]

        tx = await encode_call(
            target=self.voter,
            abi=VOTER_ABI,
            fn_name="vote",
            args=[int(token_id), pools_cs, weights_u],
            from_address=strategy,
            chain_id=self.chain_id,
        )
        return await send_transaction(tx, self.strategy_wallet_signing_callback)

    async def can_vote_now(self, *, token_id: int) -> tuple[bool, int, int, int]:
        """Check whether `token_id` can vote in the current weekly epoch.

        Returns: (can_vote, last_voted_ts, epoch_start_ts, next_epoch_start_ts).
        """
        async with web3_from_chain_id(self.chain_id) as web3:
            latest = await web3.eth.get_block("latest")
            now = int(latest["timestamp"])
            epoch_start = (now // WEEK_S) * WEEK_S
            next_epoch_start = epoch_start + WEEK_S

            c = web3.eth.contract(address=self.voter, abi=VOTER_ABI)
            last_voted = int(await c.functions.lastVoted(int(token_id)).call())

        return (last_voted < epoch_start, last_voted, epoch_start, next_epoch_start)

    async def deposit_gauge(self, *, gauge: str, lp_token: str, amount: int) -> str:
        strategy = self._require_wallet()
        gauge = to_checksum_address(gauge)
        lp_token = to_checksum_address(lp_token)
        amount = int(amount)
        if amount <= 0:
            raise ValueError("amount must be positive")

        await ensure_allowance(
            token_address=lp_token,
            owner=strategy,
            spender=gauge,
            amount=amount,
            chain_id=self.chain_id,
            signing_callback=self.strategy_wallet_signing_callback,
            approval_amount=MAX_UINT256,
        )
        tx = await encode_call(
            target=gauge,
            abi=GAUGE_ABI,
            fn_name="deposit",
            args=[amount],
            from_address=strategy,
            chain_id=self.chain_id,
        )
        return await send_transaction(tx, self.strategy_wallet_signing_callback)

    async def lp_balance(self, lp_token: str) -> int:
        strategy = self._require_wallet()
        return await get_token_balance(
            token_address=to_checksum_address(lp_token),
            chain_id=self.chain_id,
            wallet_address=strategy,
        )

    # -----------------------------
    # ERC721 receipt helpers
    # -----------------------------

    @staticmethod
    def parse_erc721_mint_token_id_from_receipt(
        receipt: dict[str, Any],
        *,
        nft_address: str,
        to_address: str,
    ) -> int:
        nft_address = to_checksum_address(nft_address).lower()
        to_address = to_checksum_address(to_address).lower()
        transfer_topic0 = keccak(text="Transfer(address,address,uint256)").hex().lower()

        logs = receipt.get("logs") or []
        for lg in logs:
            try:
                if str(lg.get("address", "")).lower() != nft_address:
                    continue
                topics = lg.get("topics") or []
                if len(topics) < 4:
                    continue
                topic0 = (
                    topics[0].hex() if hasattr(topics[0], "hex") else str(topics[0])
                )
                topic0 = str(topic0).lower().removeprefix("0x")
                if topic0 != transfer_topic0:
                    continue

                from_topic = (
                    topics[1].hex() if hasattr(topics[1], "hex") else str(topics[1])
                )
                to_topic = (
                    topics[2].hex() if hasattr(topics[2], "hex") else str(topics[2])
                )
                token_id_topic = (
                    topics[3].hex() if hasattr(topics[3], "hex") else str(topics[3])
                )

                from_addr = "0x" + str(from_topic)[-40:]
                to_addr = "0x" + str(to_topic)[-40:]
                if int(from_addr, 16) != 0:
                    continue
                if to_addr.lower() != to_address:
                    continue
                return int(str(token_id_topic), 16)
            except Exception:
                continue
        raise RuntimeError("Unable to parse ERC721 tokenId from receipt logs")

    @staticmethod
    def _parse_erc721_mint_token_id_from_receipt(
        receipt: dict[str, Any],
        *,
        nft_address: str,
        to_address: str,
    ) -> int:
        # Backwards-compatible alias.
        return AerodromeAdapter.parse_erc721_mint_token_id_from_receipt(
            receipt, nft_address=nft_address, to_address=to_address
        )

    def parse_ve_nft_token_id_from_create_lock_receipt(
        self, receipt: dict[str, Any], *, to_address: str
    ) -> int:
        return self.parse_erc721_mint_token_id_from_receipt(
            receipt, nft_address=self.ve, to_address=to_address
        )

    # -----------------------------
    # Slipstream CL tx helpers
    # -----------------------------

    async def slipstream_mint_position(
        self,
        *,
        pool: str,
        tick_lower: int,
        tick_upper: int,
        amount0_desired: int,
        amount1_desired: int,
        amount0_min: int = 0,
        amount1_min: int = 0,
        recipient: str | None = None,
        deadline: int | None = None,
        sqrt_price_x96: int = 0,
        wait_for_receipt: bool = True,
    ) -> tuple[str, int | None, dict[str, Any] | None]:
        """
        Mint a Slipstream CL position (NFPM NFT) for an existing pool.

        Returns (tx_hash, token_id_or_none, receipt_or_none).
        """
        strategy = self._require_wallet()
        pool = to_checksum_address(pool)
        recipient = to_checksum_address(recipient or strategy)
        deadline = int(deadline or self._deadline())

        amount0_desired = int(amount0_desired)
        amount1_desired = int(amount1_desired)
        if amount0_desired <= 0 or amount1_desired <= 0:
            raise ValueError("amount0_desired and amount1_desired must be positive")

        pool_state = await self.slipstream_pool_state(pool=pool)

        await ensure_allowance(
            token_address=pool_state.token0,
            owner=strategy,
            spender=AERODROME_SLIPSTREAM_NFPM,
            amount=amount0_desired,
            chain_id=self.chain_id,
            signing_callback=self.strategy_wallet_signing_callback,
            approval_amount=MAX_UINT256,
        )
        await ensure_allowance(
            token_address=pool_state.token1,
            owner=strategy,
            spender=AERODROME_SLIPSTREAM_NFPM,
            amount=amount1_desired,
            chain_id=self.chain_id,
            signing_callback=self.strategy_wallet_signing_callback,
            approval_amount=MAX_UINT256,
        )

        params = (
            pool_state.token0,
            pool_state.token1,
            int(pool_state.tick_spacing),
            int(tick_lower),
            int(tick_upper),
            int(amount0_desired),
            int(amount1_desired),
            int(amount0_min),
            int(amount1_min),
            recipient,
            int(deadline),
            int(sqrt_price_x96),
        )

        tx = await encode_call(
            target=AERODROME_SLIPSTREAM_NFPM,
            abi=SLIPSTREAM_NFPM_ABI,
            fn_name="mint",
            args=[params],
            from_address=strategy,
            chain_id=self.chain_id,
        )
        tx_hash = await send_transaction(
            tx, self.strategy_wallet_signing_callback, wait_for_receipt=False
        )

        if not wait_for_receipt:
            return tx_hash, None, None

        receipt = await wait_for_transaction_receipt(self.chain_id, tx_hash)
        token_id = self._parse_erc721_mint_token_id_from_receipt(
            receipt or {},
            nft_address=AERODROME_SLIPSTREAM_NFPM,
            to_address=recipient,
        )
        return tx_hash, int(token_id), receipt

    async def slipstream_approve_position(
        self,
        *,
        spender: str,
        token_id: int,
    ) -> str:
        """
        Approve `spender` to transfer the Slipstream NFPM position NFT.
        """
        strategy = self._require_wallet()
        spender = to_checksum_address(spender)
        tx = await encode_call(
            target=AERODROME_SLIPSTREAM_NFPM,
            abi=SLIPSTREAM_NFPM_ABI,
            fn_name="approve",
            args=[spender, int(token_id)],
            from_address=strategy,
            chain_id=self.chain_id,
        )
        return await send_transaction(tx, self.strategy_wallet_signing_callback)

    async def slipstream_gauge_deposit(
        self,
        *,
        gauge: str,
        token_id: int,
        approve: bool = True,
    ) -> str:
        """
        Deposit a Slipstream NFPM position NFT into its gauge (to earn emissions).
        """
        strategy = self._require_wallet()
        gauge = to_checksum_address(gauge)
        if approve:
            await self.slipstream_approve_position(
                spender=gauge, token_id=int(token_id)
            )
        tx = await encode_call(
            target=gauge,
            abi=SLIPSTREAM_GAUGE_ABI,
            fn_name="deposit",
            args=[int(token_id)],
            from_address=strategy,
            chain_id=self.chain_id,
        )
        return await send_transaction(tx, self.strategy_wallet_signing_callback)
