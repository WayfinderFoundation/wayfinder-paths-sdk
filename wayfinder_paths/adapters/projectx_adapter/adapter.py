from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from eth_utils import to_checksum_address
from web3 import AsyncWeb3

from wayfinder_paths.adapters.multicall_adapter.adapter import MulticallAdapter
from wayfinder_paths.adapters.uniswap_adapter.base import UniswapV3BaseAdapter
from wayfinder_paths.core.constants.contracts import PRJX_NPM, PRJX_ROUTER
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
from wayfinder_paths.core.constants.projectx import (
    ADDRESS_TO_TOKEN_ID,
    PRJX_FACTORY,
    PRJX_POINTS_API_URL,
    PROJECTX_CHAIN_ID,
    get_prjx_subgraph_url,
)
from wayfinder_paths.core.constants.projectx_abi import (
    PROJECTX_FACTORY_ABI,
    PROJECTX_POOL_ABI,
    PROJECTX_ROUTER_ABI,
)
from wayfinder_paths.core.constants.uniswap_v3_abi import (
    NONFUNGIBLE_POSITION_MANAGER_ABI,
)
from wayfinder_paths.core.utils.tokens import (
    ensure_allowance,
    get_token_balance,
    is_native_token,
)
from wayfinder_paths.core.utils.transaction import (
    encode_call,
    send_transaction,
    wait_for_transaction_receipt,
)
from wayfinder_paths.core.utils.uniswap_v3_math import (
    MASK_256,
    Q128,
    amounts_for_liq_inrange,
    deadline,
    filter_positions,
    find_pool,
    liq_for_amounts,
    read_all_positions,
    read_position,
    round_tick_to_spacing,
    sqrt_price_x96_from_tick,
    sqrt_price_x96_to_price,
    tick_from_sqrt_price_x96,
)
from wayfinder_paths.core.utils.units import from_erc20_raw
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

MINT_POLL_ATTEMPTS = 6
INITIAL_MINT_DELAY_SECONDS = 2
MINT_POLL_INTERVAL_SECONDS = 10

BALANCE_MAX_SWAPS = 4
BALANCE_SWAP_HAIRCUT = 0.02  # 2% buffer to avoid overshooting the solve trade
MINT_RETRY_SLIPPAGE_BPS = 25  # upper cap when bumping slippage after a revert
BALANCE_MIN_SWAP_TOKEN0 = 0.01  # in token0 units
BALANCE_MIN_SWAP_TOKEN1 = 0.01  # in token1 units


def _resolve_pool_address(config: dict[str, Any] | None) -> str | None:
    if not config or not isinstance(config, dict):
        return None

    for key in (
        "pool_address",
        "pool",
        "projectx_pool_address",
        "projectx_pool",
    ):
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    strategy_cfg = config.get("strategy")
    if isinstance(strategy_cfg, dict):
        for key in (
            "pool_address",
            "pool",
            "projectx_pool_address",
            "projectx_pool",
        ):
            value = strategy_cfg.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return None


def _target_ratio_need0_over_need1(sqrt_p: int, sqrt_pl: int, sqrt_pu: int) -> float:
    need0, need1 = amounts_for_liq_inrange(sqrt_p, sqrt_pl, sqrt_pu, Q128)
    return float(need0) / float(max(1, need1))


@dataclass(frozen=True)
class PositionSnapshot:
    token_id: int
    liquidity: int
    tick_lower: int
    tick_upper: int
    fee: int
    token0: str
    token1: str


POSITIONS_BY_OWNER_QUERY = """
query PositionsByOwner($owner: String!, $first: Int!, $lastId: String!) {
  positions(
    first: $first
    orderBy: id
    orderDirection: asc
    where: { id_gt: $lastId, owner: $owner }
  ) {
    id
    owner
    pool {
      id
      feeTier
      sqrtPrice
      tick
      token0 { id symbol }
      token1 { id symbol }
    }
    tickLower { tickIdx }
    tickUpper { tickIdx }
    liquidity
  }
}
"""

SWAPS_QUERY_VOLUME_TICK = """
query Swaps(
  $pool: String!
  $first: Int!
  $id_gt: String
  $timestamp_gte: BigInt
  $timestamp_lte: BigInt
) {
  swaps(
    first: $first
    orderBy: id
    orderDirection: asc
    where: {
      pool: $pool
      id_gt: $id_gt
      timestamp_gte: $timestamp_gte
      timestamp_lte: $timestamp_lte
    }
  ) {
    id
    timestamp
    tick
    sqrtPriceX96
    amount0
    amount1
    amountUSD
    pool { id }
  }
}
"""

SWAPS_QUERY_SIMPLE = """
query Swaps(
  $pool: String!
  $first: Int!
  $id_gt: String
  $timestamp_gte: BigInt
  $timestamp_lte: BigInt
) {
  swaps(
    first: $first
    orderBy: id
    orderDirection: asc
    where: {
      pool: $pool
      id_gt: $id_gt
      timestamp_gte: $timestamp_gte
      timestamp_lte: $timestamp_lte
    }
  ) {
    id
    timestamp
    sqrtPriceX96
    pool { id }
  }
}
"""


class ProjectXLiquidityAdapter(UniswapV3BaseAdapter):
    adapter_type = "PROJECTX"

    def __init__(
        self,
        config: dict[str, Any],
        *,
        strategy_wallet_signing_callback=None,
    ) -> None:
        wallet = (config or {}).get("strategy_wallet") or {}
        addr = wallet.get("address")
        if not addr:
            raise ValueError("strategy_wallet.address is required for ProjectX adapter")
        owner = to_checksum_address(str(addr))

        pool_address = _resolve_pool_address(config)
        self.pool_address: str | None = (
            to_checksum_address(str(pool_address)) if pool_address else None
        )

        super().__init__(
            "projectx_adapter",
            config,
            chain_id=PROJECTX_CHAIN_ID,
            npm_address=PRJX_NPM,
            factory_address=PRJX_FACTORY,
            owner=owner,
            strategy_wallet_signing_callback=strategy_wallet_signing_callback,
            factory_abi=PROJECTX_FACTORY_ABI,
        )

        self._token_cache: dict[str, dict[str, Any]] = {}
        self._pool_meta_cache: dict[str, Any] | None = None
        self._subgraph_url: str = get_prjx_subgraph_url(config)

    def _require_pool_address(self) -> str:
        if not self.pool_address:
            raise ValueError(
                "pool_address is required for this operation. "
                "Pass pool_address in config or config_overrides."
            )
        return self.pool_address

    async def pool_overview(self) -> tuple[bool, dict[str, Any] | str]:
        try:
            meta = await self._pool_meta()
            async with web3_from_chain_id(PROJECTX_CHAIN_ID) as web3:
                token0_meta, token1_meta = await asyncio.gather(
                    self._token_meta(web3, meta["token0"]),
                    self._token_meta(web3, meta["token1"]),
                )
            return True, {
                "sqrt_price_x96": meta["sqrt_price_x96"],
                "tick": meta["tick"],
                "tick_spacing": meta["tick_spacing"],
                "fee": meta["fee"],
                "liquidity": meta["liquidity"],
                "token0": token0_meta,
                "token1": token1_meta,
            }
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def current_balances(
        self, *, owner: str | None = None
    ) -> tuple[bool, dict[str, int] | str]:
        try:
            meta = await self._pool_meta()
            target_owner = to_checksum_address(owner) if owner else self.owner
            async with web3_from_chain_id(PROJECTX_CHAIN_ID) as web3:
                bal0, bal1 = await self._balances_for_tokens(
                    web3, [meta["token0"], meta["token1"]], owner=target_owner
                )
            return True, {meta["token0"]: int(bal0), meta["token1"]: int(bal1)}
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def list_positions(
        self, *, owner: str | None = None
    ) -> tuple[bool, list[PositionSnapshot] | str]:
        try:
            meta = await self._pool_meta()
            target_owner = to_checksum_address(owner) if owner else self.owner

            async with web3_from_chain_id(PROJECTX_CHAIN_ID) as web3:
                npm = web3.eth.contract(
                    address=self.npm_address,
                    abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
                )
                all_positions = await read_all_positions(npm, target_owner)

            filtered = filter_positions(
                all_positions,
                token0=meta["token0"],
                token1=meta["token1"],
                fee=int(meta["fee"]),
                active_only=True,
            )
            out = [
                PositionSnapshot(
                    token_id=tid,
                    liquidity=pos["liquidity"],
                    tick_lower=pos["tick_lower"],
                    tick_upper=pos["tick_upper"],
                    fee=pos["fee"],
                    token0=pos["token0"],
                    token1=pos["token1"],
                )
                for tid, pos in filtered
            ]
            return True, out
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def _list_all_positions(
        self, *, owner: str | None = None
    ) -> tuple[bool, list[PositionSnapshot] | str]:
        try:
            target_owner = to_checksum_address(owner) if owner else self.owner

            async with web3_from_chain_id(PROJECTX_CHAIN_ID) as web3:
                npm = web3.eth.contract(
                    address=self.npm_address,
                    abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
                )
                all_positions = await read_all_positions(npm, target_owner)

            filtered = filter_positions(all_positions, active_only=True)
            out = [
                PositionSnapshot(
                    token_id=tid,
                    liquidity=pos["liquidity"],
                    tick_lower=pos["tick_lower"],
                    tick_upper=pos["tick_upper"],
                    fee=pos["fee"],
                    token0=pos["token0"],
                    token1=pos["token1"],
                )
                for tid, pos in filtered
            ]
            return True, out
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_full_user_state(
        self,
        *,
        account: str,
        include_overview: bool = True,
        include_balances: bool = True,
        include_positions: bool = True,
        include_points: bool = True,
    ) -> tuple[bool, dict[str, Any] | str]:
        acct = to_checksum_address(account)

        out: dict[str, Any] = {
            "protocol": (self.adapter_type or self.name).lower(),
            "chainId": int(PROJECTX_CHAIN_ID),
            "account": acct,
            "pool": self.pool_address,
            "poolOverview": None,
            "balances": None,
            "positions": None,
            "points": None,
            "errors": {},
        }

        ok_any = False

        if include_overview and self.pool_address:
            ok_over, overview = await self.pool_overview()
            if ok_over:
                ok_any = True
                out["poolOverview"] = overview
            else:
                out["errors"]["poolOverview"] = overview

        if include_balances and self.pool_address:
            ok_bal, balances = await self.current_balances(owner=acct)
            if ok_bal:
                ok_any = True
                out["balances"] = balances
            else:
                out["errors"]["balances"] = balances

        if include_positions:
            ok_pos, positions = await self._list_all_positions(owner=acct)
            if ok_pos and isinstance(positions, list):
                ok_any = True
                out["positions"] = [
                    {
                        "token_id": int(p.token_id),
                        "liquidity": int(p.liquidity),
                        "tick_lower": int(p.tick_lower),
                        "tick_upper": int(p.tick_upper),
                        "fee": int(p.fee),
                        "token0": str(p.token0),
                        "token1": str(p.token1),
                    }
                    for p in positions
                ]
            else:
                out["errors"]["positions"] = positions

        if include_points:
            ok_pts, pts = await self.fetch_prjx_points(acct)
            if ok_pts:
                ok_any = True
                out["points"] = pts
            else:
                out["errors"]["points"] = pts

        return ok_any, out

    _SUBGRAPH_MAX_PAGE = 1000

    async def fetch_swaps(
        self,
        *,
        limit: int = 10,
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        """Return recent swaps for the configured pool via subgraph.

        Paginates via ``id_gt`` cursor so callers can request more than
        the subgraph's per-query cap of 1000 results.
        """
        try:
            self._require_pool_address()
            batch_size = min(max(1, limit), self._SUBGRAPH_MAX_PAGE)
            id_gt: str | None = None
            all_parsed: list[dict[str, Any]] = []

            while len(all_parsed) < limit:
                variables: dict[str, Any] = {
                    "pool": self.pool_address.lower(),
                    "first": batch_size,
                    "id_gt": id_gt,
                    "timestamp_gte": str(start_timestamp)
                    if start_timestamp is not None
                    else None,
                    "timestamp_lte": str(end_timestamp)
                    if end_timestamp is not None
                    else None,
                }

                data, last_err = await self._query_swaps(variables)
                if not data and last_err:
                    return False, str(last_err)

                swaps = data.get("swaps", []) or []
                if not swaps:
                    break

                batch = self._parse_swaps(swaps)
                all_parsed.extend(batch)

                if len(swaps) < batch_size:
                    break

                id_gt = swaps[-1].get("id")

            # Client-side time filter as a safety net
            if start_timestamp is not None:
                all_parsed = [
                    s
                    for s in all_parsed
                    if int(s.get("timestamp", 0)) >= start_timestamp
                ]
            if end_timestamp is not None:
                all_parsed = [
                    s for s in all_parsed if int(s.get("timestamp", 0)) <= end_timestamp
                ]
            return True, all_parsed[:limit]
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def _query_swaps(
        self, variables: dict[str, Any]
    ) -> tuple[dict[str, Any], Exception | None]:
        """Try both swap queries (volume+tick first, then simple fallback)."""

        async def _post(query_str: str) -> dict[str, Any]:
            payload = {"query": query_str, "variables": variables}
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(self._subgraph_url, json=payload)
                resp.raise_for_status()
                body = resp.json()
                if body.get("errors"):
                    raise RuntimeError(str(body.get("errors")))
                return body.get("data", {}) or {}

        data: dict[str, Any] = {}
        last_err: Exception | None = None
        for query_str in (SWAPS_QUERY_VOLUME_TICK, SWAPS_QUERY_SIMPLE):
            try:
                data = await _post(query_str)
                if data.get("swaps") is not None:
                    break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue
        return data, last_err

    @staticmethod
    def _parse_swaps(swaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        parsed: list[dict[str, Any]] = []
        for swap in swaps:
            try:
                ts_raw = swap.get("timestamp")
                sqrt_raw = swap.get("sqrtPriceX96") or swap.get("sqrt_price_x96")
                tick_raw = swap.get("tick")
                amount0_raw = swap.get("amount0")
                amount1_raw = swap.get("amount1")
                amount_usd_raw = swap.get("amountUSD") or swap.get("amount_usd")
                tick_val: int | None = None
                if tick_raw is not None:
                    tick_val = int(tick_raw)
                elif sqrt_raw is not None:
                    tick_val = int(tick_from_sqrt_price_x96(int(sqrt_raw)))
                if tick_val is None:
                    continue

                amount0: float | None = None
                amount1: float | None = None
                amount_usd: float | None = None
                try:
                    if amount0_raw is not None:
                        amount0 = float(amount0_raw)
                    if amount1_raw is not None:
                        amount1 = float(amount1_raw)
                    if amount_usd_raw is not None:
                        amount_usd = float(amount_usd_raw)
                except (TypeError, ValueError):
                    amount0 = amount1 = amount_usd = None
                parsed.append(
                    {
                        "id": swap.get("id"),
                        "timestamp": int(ts_raw or 0),
                        "tick": tick_val,
                        "sqrt_price_x96": int(sqrt_raw or 0),
                        "amount0": amount0,
                        "amount1": amount1,
                        "amount_usd": amount_usd,
                    }
                )
            except (TypeError, ValueError):  # pragma: no cover - defensive
                continue
        return parsed

    async def recent_swaps(
        self, limit: int = 10
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        return await self.fetch_swaps(limit=limit)

    async def mint_from_balances(
        self,
        tick_lower: int,
        tick_upper: int,
        *,
        slippage_bps: int = 30,
    ) -> tuple[bool, dict[str, Any] | str]:
        """Mint a new position, retrying once with higher slippage if price slips."""
        try:
            token_id, tx_hash, spent = await self._mint_from_balances_once(
                tick_lower, tick_upper, slippage_bps=slippage_bps
            )
            return True, {"token_id": token_id, "tx_hash": tx_hash, "spent": spent}
        except Exception as exc:
            msg = str(exc)
            if "Price slippage" in msg or "slippage" in msg:
                bumped = max(slippage_bps + 5, slippage_bps * 2)
                bumped = min(bumped, MINT_RETRY_SLIPPAGE_BPS)
                if bumped > slippage_bps:
                    self.logger.warning(
                        "Mint slippage check hit; retrying with slippage_bps=%s (was %s)",
                        bumped,
                        slippage_bps,
                    )
                    try:
                        token_id, tx_hash, spent = await self._mint_from_balances_once(
                            tick_lower, tick_upper, slippage_bps=bumped
                        )
                        return True, {
                            "token_id": token_id,
                            "tx_hash": tx_hash,
                            "spent": spent,
                        }
                    except Exception as exc2:  # noqa: BLE001
                        return False, str(exc2)
            return False, str(exc)

    async def _mint_from_balances_once(
        self,
        tick_lower: int,
        tick_upper: int,
        *,
        slippage_bps: int = 30,
    ) -> tuple[int | None, str | None, dict[str, int]]:
        meta = await self._sync_pool_meta()
        token0 = meta["token0"]
        token1 = meta["token1"]

        tick_spacing = int(meta["tick_spacing"])
        tick_lower_adj = int(round_tick_to_spacing(int(tick_lower), tick_spacing))
        tick_upper_adj = int(round_tick_to_spacing(int(tick_upper), tick_spacing))
        if tick_upper_adj < int(tick_upper):
            tick_upper_adj += tick_spacing
        if tick_lower_adj >= tick_upper_adj:
            tick_upper_adj = tick_lower_adj + tick_spacing

        await self._balance_for_band(
            tick_lower_adj, tick_upper_adj, slippage_bps=slippage_bps
        )
        # Re-sync after any balance swaps to avoid minting against a stale price.
        meta = await self._sync_pool_meta()

        async with web3_from_chain_id(PROJECTX_CHAIN_ID) as web3:
            await asyncio.gather(
                self._token_meta(web3, token0),
                self._token_meta(web3, token1),
            )

            before_bal0, before_bal1 = await self._balances_for_tokens(
                web3, [token0, token1]
            )

        sqrt_p = int(meta["sqrt_price_x96"])
        sqrt_pl = sqrt_price_x96_from_tick(int(tick_lower_adj))
        sqrt_pu = sqrt_price_x96_from_tick(int(tick_upper_adj))

        use0 = (before_bal0 * 999) // 1000
        use1 = (before_bal1 * 999) // 1000
        liq = liq_for_amounts(sqrt_p, sqrt_pl, sqrt_pu, use0, use1)
        empty_meta = {"token0_spent": 0, "token1_spent": 0}
        if liq <= 0:
            return None, None, empty_meta

        amt0_des, amt1_des = amounts_for_liq_inrange(sqrt_p, sqrt_pl, sqrt_pu, liq)
        amt0_des = min(use0, int(amt0_des))
        amt1_des = min(use1, int(amt1_des))
        if amt0_des <= 0 and amt1_des <= 0:
            return None, None, empty_meta

        ok, tx_hash = await super().add_liquidity(
            token0,
            token1,
            int(meta["fee"]),
            tick_lower_adj,
            tick_upper_adj,
            int(amt0_des),
            int(amt1_des),
            slippage_bps=slippage_bps,
            tick_spacing=tick_spacing,
        )
        if not ok:
            raise RuntimeError(str(tx_hash))

        token_id = await self._extract_token_id_from_receipt(tx_hash)
        if token_id is None:
            # Fallback for providers that don't return logs: poll on-chain enumeration.
            token_id = await self._poll_for_any_position_id()

        async with web3_from_chain_id(PROJECTX_CHAIN_ID) as web3:
            post_bal0, post_bal1 = await self._balances_for_tokens(
                web3, [token0, token1]
            )

        spent0 = max(0, int(before_bal0) - int(post_bal0))
        spent1 = max(0, int(before_bal1) - int(post_bal1))
        return token_id, tx_hash, {"token0_spent": spent0, "token1_spent": spent1}

    async def burn_position(self, token_id: int) -> tuple[bool, Any]:
        return await self.remove_liquidity(int(token_id), collect=True, burn=True)

    async def increase_liquidity_balanced(
        self,
        token_id: int,
        tick_lower: int,
        tick_upper: int,
        *,
        slippage_bps: int = 20,
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            await self._balance_for_band(
                tick_lower=tick_lower,
                tick_upper=tick_upper,
                slippage_bps=slippage_bps,
            )
            meta = await self._pool_meta()
            token0 = meta["token0"]
            token1 = meta["token1"]

            async with web3_from_chain_id(PROJECTX_CHAIN_ID) as web3:
                bal0, bal1 = await self._balances_for_tokens(web3, [token0, token1])
                before0 = int(bal0)
                before1 = int(bal1)

            empty_spend = {"token0_spent": 0, "token1_spent": 0}
            if before0 <= 0 and before1 <= 0:
                return True, {"tx_hash": None, "spent": empty_spend}

            sqrt_p = meta["sqrt_price_x96"]
            sqrt_pl = sqrt_price_x96_from_tick(tick_lower)
            sqrt_pu = sqrt_price_x96_from_tick(tick_upper)

            liq_all = liq_for_amounts(sqrt_p, sqrt_pl, sqrt_pu, before0, before1)
            if liq_all <= 0:
                return True, {"tx_hash": None, "spent": empty_spend}
            need0, need1 = amounts_for_liq_inrange(sqrt_p, sqrt_pl, sqrt_pu, liq_all)
            amt0_des = min(before0, int(need0))
            amt1_des = min(before1, int(need1))
            if amt0_des <= 0 and amt1_des <= 0:
                return True, {"tx_hash": None, "spent": empty_spend}

            ok, tx_hash = await super().increase_liquidity(
                int(token_id),
                int(amt0_des),
                int(amt1_des),
                slippage_bps=slippage_bps,
            )
            if not ok:
                return False, str(tx_hash)

            async with web3_from_chain_id(PROJECTX_CHAIN_ID) as web3:
                after0, after1 = await self._balances_for_tokens(web3, [token0, token1])

            spent0 = max(0, int(before0) - int(after0))
            spent1 = max(0, int(before1) - int(after1))
            return True, {
                "tx_hash": str(tx_hash),
                "spent": {"token0_spent": spent0, "token1_spent": spent1},
            }
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def swap_exact_in(
        self,
        from_token: str,
        to_token: str,
        amount_in: int,
        *,
        slippage_bps: int = 30,
        prefer_fees: Sequence[int] | None = None,
    ) -> tuple[bool, str]:
        try:
            if amount_in <= 0:
                raise ValueError("amount_in must be positive")

            token_in = to_checksum_address(from_token)
            token_out = to_checksum_address(to_token)

            if is_native_token(token_in) or is_native_token(token_out):
                raise ValueError(
                    "ProjectX swap adapter currently supports ERC20 tokens only"
                )

            if self.pool_address and not prefer_fees:
                meta = await self._pool_meta()
                pool_tokens = {meta["token0"].lower(), meta["token1"].lower()}
                if {token_in.lower(), token_out.lower()} == pool_tokens:
                    prefer_fees = [int(meta["fee"])] + [
                        f
                        for f in (100, 500, 1000, 3000, 10000)
                        if f != int(meta["fee"])
                    ]
            selected_fee, pool_address = await self._find_pool_for_pair(
                token_in, token_out, prefer_fees=prefer_fees
            )

            await ensure_allowance(
                token_address=to_checksum_address(token_in),
                owner=self.owner,
                spender=PRJX_ROUTER,
                amount=int(amount_in),
                chain_id=PROJECTX_CHAIN_ID,
                signing_callback=self.strategy_wallet_signing_callback,
                approval_amount=int(amount_in * 2),
            )

            async with web3_from_chain_id(PROJECTX_CHAIN_ID) as web3:
                pool = web3.eth.contract(address=pool_address, abi=PROJECTX_POOL_ABI)
                slot0, token0_raw, token1_raw = await asyncio.gather(
                    pool.functions.slot0().call(block_identifier="latest"),
                    pool.functions.token0().call(block_identifier="latest"),
                    pool.functions.token1().call(block_identifier="latest"),
                )
                sqrt_price_x96 = int(slot0[0])
                token0 = to_checksum_address(token0_raw)
                token1 = to_checksum_address(token1_raw)

                meta_in, meta_out, token0_meta, token1_meta = await asyncio.gather(
                    self._token_meta(web3, token_in),
                    self._token_meta(web3, token_out),
                    self._token_meta(web3, token0),
                    self._token_meta(web3, token1),
                )

                dec_in = int(meta_in.get("decimals", 18))
                dec_out = int(meta_out.get("decimals", 18))

            # Compute a conservative minOut from current mid price.
            price_token1_per_token0 = sqrt_price_x96_to_price(
                sqrt_price_x96,
                int(token0_meta.get("decimals", 18)),
                int(token1_meta.get("decimals", 18)),
            )
            fee_frac = max(0.0, min(0.5, float(selected_fee) / 1_000_000.0))
            slippage = max(1, int(slippage_bps)) / 10_000.0

            amount_in_tokens = from_erc20_raw(amount_in, dec_in)
            if (
                token_in.lower() == token0.lower()
                and token_out.lower() == token1.lower()
            ):
                expected_out_tokens = amount_in_tokens * price_token1_per_token0
            elif (
                token_in.lower() == token1.lower()
                and token_out.lower() == token0.lower()
            ):
                expected_out_tokens = (
                    amount_in_tokens / price_token1_per_token0
                    if price_token1_per_token0 > 0
                    else 0.0
                )
            else:
                raise RuntimeError("Selected pool does not match swap token pair")

            expected_out_tokens *= max(0.0, 1.0 - fee_frac)
            expected_out_raw = int(expected_out_tokens * (10**dec_out))
            amount_out_min = int(max(0, expected_out_raw) * max(0.0, 1.0 - slippage))

            tx = await encode_call(
                target=PRJX_ROUTER,
                abi=PROJECTX_ROUTER_ABI,
                fn_name="exactInputSingle",
                args=[
                    (
                        token_in,
                        token_out,
                        int(selected_fee),
                        self.owner,
                        deadline(900),
                        int(amount_in),
                        int(amount_out_min),
                        0,
                    )
                ],
                from_address=self.owner,
                chain_id=PROJECTX_CHAIN_ID,
            )
            tx_hash = await send_transaction(tx, self.strategy_wallet_signing_callback)
            return True, str(tx_hash)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    @staticmethod
    def classify_range_state(
        ticks: Sequence[int],
        tick_lower: int,
        tick_upper: int,
        fallback_tick: int | None = None,
    ) -> str:
        observations = [int(t) for t in ticks if isinstance(t, (int, float))]
        if not observations and fallback_tick is not None:
            observations = [fallback_tick]
        if not observations:
            return "unknown"
        outside = sum(
            1 for tick in observations if tick <= tick_lower or tick >= tick_upper
        )
        if outside == len(observations):
            return "out_of_range"
        if outside > 0:
            return "entering_out_of_range"
        return "in_range"

    async def price_band_for_ticks(
        self, tick_lower: int, tick_upper: int
    ) -> tuple[bool, dict[str, Any] | None | str]:
        try:
            meta = await self._pool_meta()
            token0 = meta["token0"]
            token1 = meta["token1"]

            lo_tick = min(int(tick_lower), int(tick_upper))
            hi_tick = max(int(tick_lower), int(tick_upper))
            if lo_tick == hi_tick:
                return True, None

            sqrt_lo = sqrt_price_x96_from_tick(lo_tick)
            sqrt_hi = sqrt_price_x96_from_tick(hi_tick)

            async with web3_from_chain_id(PROJECTX_CHAIN_ID) as web3:
                token0_meta, token1_meta = await asyncio.gather(
                    self._token_meta(web3, token0),
                    self._token_meta(web3, token1),
                )
                price_lo = sqrt_price_x96_to_price(
                    sqrt_lo, int(token0_meta["decimals"]), int(token1_meta["decimals"])
                )
                price_hi = sqrt_price_x96_to_price(
                    sqrt_hi, int(token0_meta["decimals"]), int(token1_meta["decimals"])
                )

            prices = [p for p in (price_lo, price_hi) if p > 0]
            if len(prices) < 2:
                return True, None
            token1_per_token0_min, token1_per_token0_max = sorted(prices)

            token0_per_token1_min = 0.0
            token0_per_token1_max = 0.0
            if token1_per_token0_max > 0:
                token0_per_token1_min = 1.0 / token1_per_token0_max
            if token1_per_token0_min > 0:
                token0_per_token1_max = 1.0 / token1_per_token0_min
            if token0_per_token1_min <= 0 or token0_per_token1_max <= 0:
                token0_per_token1_min = token0_per_token1_max = 0.0

            return True, {
                "token0": token0_meta,
                "token1": token1_meta,
                "token1_per_token0": {
                    "min": token1_per_token0_min,
                    "max": token1_per_token0_max,
                },
                "token0_per_token1": {
                    "min": token0_per_token1_min,
                    "max": token0_per_token1_max,
                },
            }
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def find_pool_for_pair(
        self,
        token_a: str,
        token_b: str,
        *,
        prefer_fees: Sequence[int] | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        """Resolve the ProjectX pool address for a token pair (read-only)."""
        try:
            fee, pool = await self._find_pool_for_pair(
                token_a, token_b, prefer_fees=prefer_fees
            )
            return True, {"fee": int(fee), "pool": str(pool)}
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def live_fee_snapshot(
        self, token_id: int
    ) -> tuple[bool, dict[str, float] | str]:
        try:
            pool_addr = self._require_pool_address()
            owed0, owed1 = await self._read_live_claimable_fees(int(token_id))
            position = await self._read_position_struct(int(token_id))
            async with web3_from_chain_id(PROJECTX_CHAIN_ID) as web3:
                pool = web3.eth.contract(address=pool_addr, abi=PROJECTX_POOL_ABI)
                token0_meta, token1_meta, slot0 = await asyncio.gather(
                    self._token_meta(web3, position["token0"]),
                    self._token_meta(web3, position["token1"]),
                    pool.functions.slot0().call(block_identifier="latest"),
                )
                sqrt_price = int(slot0[0])

            usd_value = self._estimate_fees_usd_from_pool(
                owed0,
                owed1,
                int(token0_meta["decimals"]),
                int(token1_meta["decimals"]),
                sqrt_price,
            )
            return True, {
                "owed0": owed0 / (10 ** int(token0_meta["decimals"])),
                "owed1": owed1 / (10 ** int(token1_meta["decimals"])),
                "usd": float(usd_value),
            }
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    @staticmethod
    async def fetch_prjx_points(
        wallet_address: str | None,
    ) -> tuple[bool, dict[str, Any] | str]:
        if not wallet_address:
            return True, {}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    PRJX_POINTS_API_URL, params={"walletAddress": wallet_address}
                )
                if resp.status_code == 404:
                    return True, {"points": 0}
                resp.raise_for_status()
                data = resp.json() or {}
                if data.get("error") == "User not found":
                    return True, {"points": 0}
                return True, data
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def _pool_meta(self) -> dict[str, Any]:
        if self._pool_meta_cache:
            return self._pool_meta_cache

        pool_addr = self._require_pool_address()
        async with web3_from_chain_id(PROJECTX_CHAIN_ID) as web3:
            pool = web3.eth.contract(address=pool_addr, abi=PROJECTX_POOL_ABI)
            slot0, tick_spacing, fee, liquidity, token0, token1 = await asyncio.gather(
                pool.functions.slot0().call(block_identifier="latest"),
                pool.functions.tickSpacing().call(block_identifier="latest"),
                pool.functions.fee().call(block_identifier="latest"),
                pool.functions.liquidity().call(block_identifier="latest"),
                pool.functions.token0().call(block_identifier="latest"),
                pool.functions.token1().call(block_identifier="latest"),
            )

        meta = {
            "sqrt_price_x96": int(slot0[0]),
            "tick": int(slot0[1]),
            "tick_spacing": int(tick_spacing),
            "fee": int(fee),
            "liquidity": int(liquidity),
            "token0": to_checksum_address(token0),
            "token1": to_checksum_address(token1),
        }
        self._pool_meta_cache = meta
        return meta

    async def _sync_pool_meta(self) -> dict[str, Any]:
        self._pool_meta_cache = None
        return await self._pool_meta()

    async def _token_meta(self, web3: AsyncWeb3, address: str) -> dict[str, Any]:
        checksum = to_checksum_address(address)
        cached = self._token_cache.get(checksum.lower())
        if cached:
            return cached
        contract = web3.eth.contract(address=checksum, abi=ERC20_ABI)
        decimals, symbol = await asyncio.gather(
            contract.functions.decimals().call(block_identifier="latest"),
            contract.functions.symbol().call(block_identifier="latest"),
        )
        token_id = ADDRESS_TO_TOKEN_ID.get(checksum)
        meta = {
            "address": checksum,
            "decimals": int(decimals),
            "symbol": symbol,
            "token_id": token_id,
        }
        self._token_cache[checksum.lower()] = meta
        return meta

    async def _balances_for_tokens(
        self,
        web3: AsyncWeb3,
        token_addresses: Sequence[str],
        *,
        owner: str | None = None,
        block_identifier: str = "pending",
    ) -> list[int]:
        if not token_addresses:
            return []

        checksummed = [to_checksum_address(a) for a in token_addresses]
        target_owner = to_checksum_address(owner) if owner else self.owner
        try:
            multicall = MulticallAdapter(web3=web3, chain_id=PROJECTX_CHAIN_ID)
            calls = [
                multicall.encode_erc20_balance(token, target_owner)
                for token in checksummed
            ]
            res = await multicall.aggregate(calls, block_identifier=block_identifier)
            return [int(multicall.decode_uint256(d)) for d in res.return_data]
        except Exception:
            balances = await asyncio.gather(
                *(
                    self._balance(web3, token, owner=target_owner)
                    for token in checksummed
                )
            )
            return [int(b) for b in balances]

    async def _balance(
        self, web3: AsyncWeb3, token_address: str, *, owner: str | None = None
    ) -> int:
        target_owner = to_checksum_address(owner) if owner else self.owner
        return int(
            await get_token_balance(
                token_address,
                PROJECTX_CHAIN_ID,
                target_owner,
                web3=web3,
                block_identifier="pending",
            )
        )

    async def _extract_token_id_from_receipt(self, tx_hash: str) -> int | None:
        try:
            receipt = await wait_for_transaction_receipt(PROJECTX_CHAIN_ID, tx_hash)
        except Exception:
            return None

        transfer_topic = (
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        )
        npm_addr = str(self.npm_address).lower()
        owner_topic = "0x" + self.owner.lower()[2:].rjust(64, "0")
        for log in receipt.get("logs", []) or []:
            try:
                if str(log.get("address", "")).lower() != npm_addr:
                    continue
                topics = log.get("topics") or []
                if len(topics) < 4:
                    continue
                t0 = topics[0].hex() if hasattr(topics[0], "hex") else str(topics[0])
                if t0.lower() != transfer_topic:
                    continue
                to_topic = (
                    topics[2].hex() if hasattr(topics[2], "hex") else str(topics[2])
                )
                if to_topic.lower() != owner_topic:
                    continue
                token_topic = (
                    topics[3].hex() if hasattr(topics[3], "hex") else str(topics[3])
                )
                return int(token_topic, 16)
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _poll_for_any_position_id(self) -> int | None:
        for attempt in range(MINT_POLL_ATTEMPTS):
            await asyncio.sleep(
                INITIAL_MINT_DELAY_SECONDS
                if attempt == 0
                else MINT_POLL_INTERVAL_SECONDS
            )
            ok, positions = await self.list_positions()
            if ok and isinstance(positions, list) and positions:
                return int(positions[0].token_id)
        return None

    async def _balance_for_band(
        self,
        tick_lower: int,
        tick_upper: int,
        *,
        slippage_bps: int = 30,
    ) -> None:
        for _ in range(BALANCE_MAX_SWAPS):
            meta = await self._sync_pool_meta()
            sqrt_p = int(meta["sqrt_price_x96"])
            sqrt_pl = sqrt_price_x96_from_tick(int(tick_lower))
            sqrt_pu = sqrt_price_x96_from_tick(int(tick_upper))
            swapped = await self._swap_once_to_band_ratio(
                sqrt_p, sqrt_pl, sqrt_pu, slippage_bps=slippage_bps
            )
            if not swapped:
                break

    async def _swap_once_to_band_ratio(
        self,
        sqrt_p: int,
        sqrt_pl: int,
        sqrt_pu: int,
        *,
        slippage_bps: int,
    ) -> bool:
        meta = await self._pool_meta()
        token0 = meta["token0"]
        token1 = meta["token1"]

        async with web3_from_chain_id(PROJECTX_CHAIN_ID) as web3:
            b0, b1 = await self._balances_for_tokens(web3, [token0, token1])
            if b0 <= 0 and b1 <= 0:
                return False
            token0_meta, token1_meta = await asyncio.gather(
                self._token_meta(web3, token0),
                self._token_meta(web3, token1),
            )
            dec0 = int(token0_meta["decimals"])
            dec1 = int(token1_meta["decimals"])

        target_ratio = _target_ratio_need0_over_need1(sqrt_p, sqrt_pl, sqrt_pu)
        # Use raw price (token1_raw / token0_raw) — no decimal adjustment —
        # because target_ratio and balances are all in raw units.
        price_raw = (sqrt_p / (1 << 96)) ** 2
        if price_raw <= 0:
            return False

        fee_haircut = max(5, int(slippage_bps)) / 10_000.0
        price_net = max(price_raw * (1.0 - fee_haircut), 1e-18)

        numer = b0 - target_ratio * b1
        if numer > 0:
            denom = 1.0 + target_ratio * price_net
            if denom <= 0:
                return False
            amount0_in = numer / denom
            amount0_in = int(max(0, min(amount0_in, b0)))
            amount0_in = int(amount0_in * (1 - BALANCE_SWAP_HAIRCUT))
            if amount0_in > 0:
                min0 = int(BALANCE_MIN_SWAP_TOKEN0 * (10**dec0))
                if amount0_in < min0:
                    return False
                ok, tx = await self.swap_exact_in(
                    token0,
                    token1,
                    amount0_in,
                    slippage_bps=slippage_bps,
                )
                if not ok:
                    if "No PRJX route" in str(tx):
                        self.logger.warning("Skipping balance swap: %s", tx)
                        return False
                    raise RuntimeError(str(tx))
                return True
        else:
            denom = target_ratio + (1.0 / price_net)
            if denom <= 0:
                return False
            amount1_in = (target_ratio * b1 - b0) / denom
            amount1_in = int(max(0, min(amount1_in, b1)))
            amount1_in = int(amount1_in * (1 - BALANCE_SWAP_HAIRCUT))
            if amount1_in > 0:
                min1 = int(BALANCE_MIN_SWAP_TOKEN1 * (10**dec1))
                if amount1_in < min1:
                    return False
                ok, tx = await self.swap_exact_in(
                    token1,
                    token0,
                    amount1_in,
                    slippage_bps=slippage_bps,
                )
                if not ok:
                    if "No PRJX route" in str(tx):
                        self.logger.warning("Skipping balance swap: %s", tx)
                        return False
                    raise RuntimeError(str(tx))
                return True
        return False

    async def _read_position_struct(self, token_id: int) -> dict[str, Any]:
        async with web3_from_chain_id(PROJECTX_CHAIN_ID) as web3:
            npm = web3.eth.contract(
                address=self.npm_address,
                abi=NONFUNGIBLE_POSITION_MANAGER_ABI,
            )
            return dict(await read_position(npm, int(token_id)))

    async def _fee_growth_inside_now(
        self,
        pool_contract,
        tick_current: int,
        tick_lower: int,
        tick_upper: int,
    ) -> tuple[int, int]:
        (f0_global, f1_global, tl, tu) = await asyncio.gather(
            pool_contract.functions.feeGrowthGlobal0X128().call(
                block_identifier="latest"
            ),
            pool_contract.functions.feeGrowthGlobal1X128().call(
                block_identifier="latest"
            ),
            pool_contract.functions.ticks(int(tick_lower)).call(
                block_identifier="latest"
            ),
            pool_contract.functions.ticks(int(tick_upper)).call(
                block_identifier="latest"
            ),
        )
        f0_global = int(f0_global)
        f1_global = int(f1_global)

        f0_below = (
            int(tl[2])
            if tick_current >= tick_lower
            else (f0_global - int(tl[2])) & MASK_256
        )
        f1_below = (
            int(tl[3])
            if tick_current >= tick_lower
            else (f1_global - int(tl[3])) & MASK_256
        )

        f0_above = (
            int(tu[2])
            if tick_current < tick_upper
            else (f0_global - int(tu[2])) & MASK_256
        )
        f1_above = (
            int(tu[3])
            if tick_current < tick_upper
            else (f1_global - int(tu[3])) & MASK_256
        )

        f0_inside = (f0_global - f0_below - f0_above) & MASK_256
        f1_inside = (f1_global - f1_below - f1_above) & MASK_256
        return f0_inside, f1_inside

    async def _read_live_claimable_fees(self, token_id: int) -> tuple[int, int]:
        pool_addr = self._require_pool_address()
        position = await self._read_position_struct(int(token_id))
        async with web3_from_chain_id(PROJECTX_CHAIN_ID) as web3:
            pool_contract = web3.eth.contract(address=pool_addr, abi=PROJECTX_POOL_ABI)
            slot0 = await pool_contract.functions.slot0().call(
                block_identifier="latest"
            )
            tick_current = int(slot0[1])
            f0_inside, f1_inside = await self._fee_growth_inside_now(
                pool_contract,
                tick_current,
                position["tick_lower"],
                position["tick_upper"],
            )
        delta0 = (f0_inside - position["fee_growth_inside0_last_x128"]) & MASK_256
        delta1 = (f1_inside - position["fee_growth_inside1_last_x128"]) & MASK_256
        extra0 = (position["liquidity"] * delta0) // Q128
        extra1 = (position["liquidity"] * delta1) // Q128
        claim0 = position["tokens_owed0"] + extra0
        claim1 = position["tokens_owed1"] + extra1
        return claim0, claim1

    @staticmethod
    def _estimate_fees_usd_from_pool(
        owed0: int,
        owed1: int,
        decimals0: int,
        decimals1: int,
        sqrt_price_x96: int,
        token1_is_usd_like: bool = True,
    ) -> float:
        if sqrt_price_x96 <= 0:
            return 0.0
        price_adjusted = sqrt_price_x96_to_price(sqrt_price_x96, decimals0, decimals1)
        f0 = from_erc20_raw(owed0, decimals0)
        f1 = from_erc20_raw(owed1, decimals1)
        if token1_is_usd_like:
            usd_value = f1 + f0 * price_adjusted
        elif price_adjusted != 0:
            usd_value = f0 + f1 / price_adjusted
        else:
            usd_value = 0.0
        return float(usd_value)

    async def _find_pool_for_pair(
        self, token_a: str, token_b: str, *, prefer_fees: Sequence[int] | None = None
    ) -> tuple[int, str]:
        fees = list(prefer_fees or [100, 500, 1000, 3000, 10000])
        async with web3_from_chain_id(PROJECTX_CHAIN_ID) as web3:
            factory = web3.eth.contract(
                address=self.factory_address,
                abi=PROJECTX_FACTORY_ABI,
            )
            results = await asyncio.gather(
                *(find_pool(factory, token_a, token_b, fee) for fee in fees)
            )
            fallback: tuple[int, str] | None = None
            for fee, pool_addr in zip(fees, results, strict=True):
                if not pool_addr:
                    continue
                if fallback is None:
                    fallback = (fee, pool_addr)
                pool_contract = web3.eth.contract(
                    address=pool_addr, abi=PROJECTX_POOL_ABI
                )
                liq = await pool_contract.functions.liquidity().call(
                    block_identifier="latest"
                )
                if int(liq) > 0:
                    return fee, pool_addr
            if fallback:
                return fallback
        raise RuntimeError(
            f"No PRJX route for pair {token_a}->{token_b} (fees tried: {fees})"
        )
