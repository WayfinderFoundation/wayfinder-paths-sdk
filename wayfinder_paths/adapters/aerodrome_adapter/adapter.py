from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from eth_utils import to_checksum_address
from hexbytes import HexBytes
from web3._utils.events import event_abi_to_log_topic, get_event_data

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter, require_wallet
from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.aerodrome_abi import (
    AERODROME_GAUGE_ABI,
    AERODROME_POOL_ABI,
    AERODROME_POOL_FACTORY_ABI,
    AERODROME_REWARD_ABI,
    AERODROME_REWARDS_DISTRIBUTOR_ABI,
    AERODROME_ROUTER_ABI,
    AERODROME_VOTER_ABI,
    AERODROME_VOTING_ESCROW_ABI,
)
from wayfinder_paths.core.constants.aerodrome_contracts import AERODROME_BY_CHAIN
from wayfinder_paths.core.constants.base import MAX_UINT256
from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
from wayfinder_paths.core.constants.contracts import BASE_WETH
from wayfinder_paths.core.constants.erc721_abi import ERC721_TRANSFER_EVENT_ABI
from wayfinder_paths.core.utils.multicall import (
    Call,
    read_only_calls_multicall_or_gather,
)
from wayfinder_paths.core.utils.tokens import ensure_allowance, is_native_token
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.uniswap_v3_math import deadline as default_deadline
from wayfinder_paths.core.utils.uniswap_v3_math import slippage_min
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

WEEK_SECONDS = 7 * 24 * 60 * 60
EPOCH_SPECIAL_WINDOW_SECONDS = 60 * 60  # first/last hour restrictions

_ERC721_TRANSFER_TOPIC0 = HexBytes(event_abi_to_log_topic(ERC721_TRANSFER_EVENT_ABI))


class AerodromeAdapter(BaseAdapter):
    """
    Aerodrome classic pool/gauge/veAERO adapter (Base mainnet only).

    Mental model:
    - LP positions live at Pool (ERC20 LP token) level; fees can be claimed by unstaked LPs.
    - Staking LP in a Gauge earns emissions; pool fees are redirected to ve voters.
    - veAERO positions are VotingEscrow NFTs; voters earn fees/bribes/rebases.
    """

    adapter_type = "AERODROME"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        sign_callback: Callable | None = None,
        wallet_address: str | None = None,
    ) -> None:
        super().__init__("aerodrome_adapter", config or {})
        self.sign_callback = sign_callback
        self.chain_id = CHAIN_ID_BASE

        entry = AERODROME_BY_CHAIN.get(CHAIN_ID_BASE)
        if not entry:
            raise ValueError("Aerodrome Base deployment constants missing")

        self.aero = entry["aero"]
        self.router = entry["router"]
        self.pool_factory = entry["pool_factory"]
        self.voter = entry["voter"]
        self.voting_escrow = entry["voting_escrow"]
        self.rewards_distributor = entry["rewards_distributor"]
        self.weth = BASE_WETH

        self.wallet_address: str | None = (
            to_checksum_address(wallet_address) if wallet_address else None
        )

    def _require_chain(self, chain_id: int | None) -> int:
        cid = chain_id or self.chain_id
        if cid != CHAIN_ID_BASE:
            raise ValueError(
                f"Aerodrome adapter v1 supports Base only (chain_id={CHAIN_ID_BASE}), got {cid}"
            )
        return cid

    async def _minted_erc721_token_id(
        self,
        *,
        chain_id: int,
        nft_contract: str,
        tx_hash: str,
        expected_to: str,
    ) -> int | None:
        nft_l = to_checksum_address(nft_contract).lower()
        expected_to_l = to_checksum_address(expected_to).lower()
        async with web3_from_chain_id(chain_id) as web3:
            receipt = await web3.eth.get_transaction_receipt(tx_hash)
            logs = (receipt or {}).get("logs") or []
            for log in logs if isinstance(logs, list) else []:
                try:
                    if (log.get("address") or "").lower() != nft_l:
                        continue
                    topics = log.get("topics") or []
                    if not topics:
                        continue
                    if HexBytes(topics[0]) != _ERC721_TRANSFER_TOPIC0:
                        continue
                    evt = get_event_data(web3.codec, ERC721_TRANSFER_EVENT_ABI, log)
                    args = evt.get("args") or {}
                    from_addr = args.get("from")
                    to_addr = args.get("to")
                    token_id = args.get("tokenId")
                    if not from_addr or not to_addr or token_id is None:
                        continue
                    if to_checksum_address(from_addr).lower() != ZERO_ADDRESS:
                        continue
                    if to_checksum_address(to_addr).lower() != expected_to_l:
                        continue
                    return token_id
                except Exception:
                    continue
        return None

    async def _can_vote_now(
        self, *, chain_id: int, token_id: int | None = None
    ) -> tuple[bool, str]:
        async with web3_from_chain_id(chain_id) as web3:
            latest = await web3.eth.get_block("latest")
            ts = latest.get("timestamp") or 0

            epoch_start = (ts // WEEK_SECONDS) * WEEK_SECONDS
            epoch_end = epoch_start + WEEK_SECONDS

            if ts < epoch_start + EPOCH_SPECIAL_WINDOW_SECONDS:
                return False, "Voting restricted in the first hour of the epoch"

            if ts >= epoch_end - EPOCH_SPECIAL_WINDOW_SECONDS:
                if token_id is None:
                    return (
                        False,
                        "Voting restricted in the last hour of the epoch (token_id required to check whitelist)",
                    )
                voter = web3.eth.contract(
                    address=to_checksum_address(self.voter), abi=AERODROME_VOTER_ABI
                )
                whitelisted = await voter.functions.isWhitelistedNFT(token_id).call(
                    block_identifier="latest"
                )
                if not whitelisted:
                    return (
                        False,
                        "Voting restricted in the last hour of the epoch unless tokenId is whitelisted",
                    )

        return True, ""

    async def get_pool(
        self,
        *,
        tokenA: str,
        tokenB: str,
        stable: bool,
        chain_id: int = CHAIN_ID_BASE,
    ) -> tuple[bool, Any]:
        try:
            cid = self._require_chain(chain_id)
            tA = to_checksum_address(tokenA)
            tB = to_checksum_address(tokenB)
            async with web3_from_chain_id(cid) as web3:
                factory = web3.eth.contract(
                    address=to_checksum_address(self.pool_factory),
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
        chain_id: int = CHAIN_ID_BASE,
    ) -> tuple[bool, Any]:
        try:
            cid = self._require_chain(chain_id)
            pool = to_checksum_address(pool)
            async with web3_from_chain_id(cid) as web3:
                voter = web3.eth.contract(
                    address=to_checksum_address(self.voter), abi=AERODROME_VOTER_ABI
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

    async def get_reward_contracts(
        self,
        *,
        gauge: str,
        chain_id: int = CHAIN_ID_BASE,
    ) -> tuple[bool, Any]:
        """Return fee/bribe reward contracts for a gauge via Voter discovery."""
        try:
            cid = self._require_chain(chain_id)
            gauge = to_checksum_address(gauge)
            async with web3_from_chain_id(cid) as web3:
                voter = web3.eth.contract(
                    address=to_checksum_address(self.voter), abi=AERODROME_VOTER_ABI
                )
                fees, bribes = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=cid,
                    calls=[
                        Call(
                            voter,
                            "gaugeToFees",
                            args=(gauge,),
                            postprocess=to_checksum_address,
                        ),
                        Call(
                            voter,
                            "gaugeToBribe",
                            args=(gauge,),
                            postprocess=to_checksum_address,
                        ),
                    ],
                    block_identifier="latest",
                )
            return True, {
                "fees": fees,
                "bribes": bribes,
            }
        except Exception as exc:
            return False, str(exc)

    async def get_all_markets(
        self,
        *,
        chain_id: int = CHAIN_ID_BASE,
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
            cid = self._require_chain(chain_id)
            start_i = max(0, start)

            async with web3_from_chain_id(cid) as web3:
                voter = web3.eth.contract(
                    address=to_checksum_address(self.voter), abi=AERODROME_VOTER_ABI
                )
                total = await voter.functions.length().call(
                    block_identifier=block_identifier
                )

                if total == 0 or start_i >= total:
                    return True, {
                        "protocol": "aerodrome",
                        "chain_id": cid,
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
                    chain_id=cid,
                    calls=pool_calls,
                    block_identifier=block_identifier,
                    chunk_size=100,
                )

                pool_contracts = [
                    web3.eth.contract(address=p, abi=AERODROME_POOL_ABI) for p in pools
                ]

                md_calls = [
                    Call(pc, "metadata")
                    for pc in pool_contracts  # (dec0,dec1,r0,r1,st,t0,t1)
                ]
                gauge_calls = [
                    Call(
                        voter,
                        "gauges",
                        args=(p,),
                        postprocess=lambda a: to_checksum_address(a),
                    )
                    for p in pools
                ]

                metadata_list, gauges = await asyncio.gather(
                    read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=cid,
                        calls=md_calls,
                        block_identifier=block_identifier,
                        chunk_size=50,
                    ),
                    read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=cid,
                        calls=gauge_calls,
                        block_identifier=block_identifier,
                        chunk_size=100,
                    ),
                )

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

                    # voter mappings for each gauge
                    fee_calls = [
                        Call(
                            voter,
                            "gaugeToFees",
                            args=(g,),
                            postprocess=lambda a: to_checksum_address(a),
                        )
                        for g in gauges_nonzero
                    ]
                    bribe_calls = [
                        Call(
                            voter,
                            "gaugeToBribe",
                            args=(g,),
                            postprocess=lambda a: to_checksum_address(a),
                        )
                        for g in gauges_nonzero
                    ]
                    reward_token_calls = [
                        Call(
                            gc,
                            "rewardToken",
                            postprocess=lambda a: to_checksum_address(a),
                        )
                        for gc in gauge_contracts
                    ]
                    reward_rate_calls = [
                        Call(gc, "rewardRate", postprocess=int)
                        for gc in gauge_contracts
                    ]
                    total_supply_calls = [
                        Call(gc, "totalSupply", postprocess=int)
                        for gc in gauge_contracts
                    ]
                    period_finish_calls = [
                        Call(gc, "periodFinish", postprocess=int)
                        for gc in gauge_contracts
                    ]

                    (
                        fee_res,
                        bribe_res,
                        reward_token_res,
                        reward_rate_res,
                        total_supply_res,
                        period_finish_res,
                    ) = await asyncio.gather(
                        read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=cid,
                            calls=fee_calls,
                            block_identifier=block_identifier,
                            chunk_size=100,
                        ),
                        read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=cid,
                            calls=bribe_calls,
                            block_identifier=block_identifier,
                            chunk_size=100,
                        ),
                        read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=cid,
                            calls=reward_token_calls,
                            block_identifier=block_identifier,
                            chunk_size=100,
                        ),
                        read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=cid,
                            calls=reward_rate_calls,
                            block_identifier=block_identifier,
                            chunk_size=100,
                        ),
                        read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=cid,
                            calls=total_supply_calls,
                            block_identifier=block_identifier,
                            chunk_size=100,
                        ),
                        read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=cid,
                            calls=period_finish_calls,
                            block_identifier=block_identifier,
                            chunk_size=100,
                        ),
                    )

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
                "chain_id": cid,
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
        chain_id: int = CHAIN_ID_BASE,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        try:
            cid = self._require_chain(chain_id)
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
                tokenA_q, tokenB_q = token, self.weth
                amtA_q, amtB_q = token_amt, eth_amt
            elif tB_native:
                token = to_checksum_address(tokenA)
                token_amt = amountA_desired
                eth_amt = amountB_desired
                tokenA_q, tokenB_q = token, self.weth
                amtA_q, amtB_q = token_amt, eth_amt
            else:
                tokenA_q, tokenB_q = (
                    to_checksum_address(tokenA),
                    to_checksum_address(tokenB),
                )
                amtA_q, amtB_q = amountA_desired, amountB_desired

            async with web3_from_chain_id(cid) as web3:
                router = web3.eth.contract(
                    address=to_checksum_address(self.router), abi=AERODROME_ROUTER_ABI
                )
                a, b, liq = await router.functions.quoteAddLiquidity(
                    tokenA_q,
                    tokenB_q,
                    stable,
                    to_checksum_address(self.pool_factory),
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
        chain_id: int = CHAIN_ID_BASE,
    ) -> tuple[bool, Any]:
        """
        Add liquidity (ERC20-ERC20) or (ERC20-ETH) when either token is native.
        """
        if amountA_desired <= 0 or amountB_desired <= 0:
            return False, "amounts must be positive"

        if self.sign_callback is None:
            return False, "sign_callback is required"

        try:
            cid = self._require_chain(chain_id)
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
                    tokenB=self.weth,
                    stable=stable,
                    amountA_desired=token_amt,
                    amountB_desired=eth_amt,
                    chain_id=cid,
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
                    spender=to_checksum_address(self.router),
                    amount=token_amt,
                    chain_id=cid,
                    signing_callback=self.sign_callback,
                    approval_amount=MAX_UINT256,
                )
                if not approved[0]:
                    return approved

                tx = await encode_call(
                    target=self.router,
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
                    chain_id=cid,
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
                chain_id=cid,
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
                spender=to_checksum_address(self.router),
                amount=amountA_desired,
                chain_id=cid,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approvedA[0]:
                return approvedA

            approvedB = await ensure_allowance(
                token_address=tB,
                owner=to_checksum_address(self.wallet_address),
                spender=to_checksum_address(self.router),
                amount=amountB_desired,
                chain_id=cid,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approvedB[0]:
                return approvedB

            tx = await encode_call(
                target=self.router,
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
                chain_id=cid,
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
        chain_id: int = CHAIN_ID_BASE,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        try:
            cid = self._require_chain(chain_id)
            if liquidity <= 0:
                return False, "liquidity must be positive"

            tA_native = is_native_token(tokenA)
            tB_native = is_native_token(tokenB)
            if tA_native and tB_native:
                return False, "tokenA and tokenB cannot both be native"

            if tA_native:
                token = to_checksum_address(tokenB)
                tokenA_q, tokenB_q = token, self.weth
            elif tB_native:
                token = to_checksum_address(tokenA)
                tokenA_q, tokenB_q = token, self.weth
            else:
                tokenA_q, tokenB_q = (
                    to_checksum_address(tokenA),
                    to_checksum_address(tokenB),
                )

            async with web3_from_chain_id(cid) as web3:
                router = web3.eth.contract(
                    address=to_checksum_address(self.router), abi=AERODROME_ROUTER_ABI
                )
                a, b = await router.functions.quoteRemoveLiquidity(
                    tokenA_q,
                    tokenB_q,
                    stable,
                    to_checksum_address(self.pool_factory),
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
        chain_id: int = CHAIN_ID_BASE,
    ) -> tuple[bool, Any]:
        """Remove liquidity (wallet-held LP only)."""
        if liquidity <= 0:
            return False, "liquidity must be positive"
        if self.sign_callback is None:
            return False, "sign_callback is required"

        try:
            cid = self._require_chain(chain_id)
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

            async with web3_from_chain_id(cid) as web3:
                factory = web3.eth.contract(
                    address=to_checksum_address(self.pool_factory),
                    abi=AERODROME_POOL_FACTORY_ABI,
                )

                # Determine pool (LP token) to approve.
                if tA_native or tB_native:
                    token = to_checksum_address(tokenB if tA_native else tokenA)
                    pool = await factory.functions.getPool(
                        token, self.weth, stable
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
                spender=to_checksum_address(self.router),
                amount=liquidity,
                chain_id=cid,
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
                    tokenB=self.weth,
                    stable=stable,
                    liquidity=liquidity,
                    chain_id=cid,
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
                    target=self.router,
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
                    chain_id=cid,
                )
                tx_hash = await send_transaction(tx, self.sign_callback)
                return True, tx_hash

            ok_q, q = await self.quote_remove_liquidity(
                tokenA=to_checksum_address(tokenA),
                tokenB=to_checksum_address(tokenB),
                stable=stable,
                liquidity=liquidity,
                chain_id=cid,
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
                target=self.router,
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
                chain_id=cid,
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
        chain_id: int = CHAIN_ID_BASE,
    ) -> tuple[bool, Any]:
        """Claim Pool fees for wallet-held LP (unstaked)."""
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            cid = self._require_chain(chain_id)
            pool = to_checksum_address(pool)
            acct = to_checksum_address(self.wallet_address)

            async with web3_from_chain_id(cid) as web3:
                pc = web3.eth.contract(address=pool, abi=AERODROME_POOL_ABI)
                c0, c1 = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=cid,
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
                chain_id=cid,
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
        chain_id: int = CHAIN_ID_BASE,
    ) -> tuple[bool, Any]:
        if amount <= 0:
            return False, "amount must be positive"
        if self.sign_callback is None:
            return False, "sign_callback is required"

        try:
            cid = self._require_chain(chain_id)
            gauge = to_checksum_address(gauge)
            recipient_addr = to_checksum_address(recipient) if recipient else None

            async with web3_from_chain_id(cid) as web3:
                voter = web3.eth.contract(
                    address=to_checksum_address(self.voter), abi=AERODROME_VOTER_ABI
                )
                g = web3.eth.contract(address=gauge, abi=AERODROME_GAUGE_ABI)
                alive, staking_token = await asyncio.gather(
                    voter.functions.isAlive(gauge).call(block_identifier="latest"),
                    g.functions.stakingToken().call(block_identifier="latest"),
                )
                if not alive:
                    return False, "Gauge is not alive (killed/dead)"

            approved = await ensure_allowance(
                token_address=to_checksum_address(staking_token),
                owner=to_checksum_address(self.wallet_address),
                spender=gauge,
                amount=amount,
                chain_id=cid,
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
                chain_id=cid,
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
        chain_id: int = CHAIN_ID_BASE,
    ) -> tuple[bool, Any]:
        if amount <= 0:
            return False, "amount must be positive"
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            cid = self._require_chain(chain_id)
            gauge = to_checksum_address(gauge)
            tx = await encode_call(
                target=gauge,
                abi=AERODROME_GAUGE_ABI,
                fn_name="withdraw",
                args=[amount],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=cid,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def claim_gauge_rewards(
        self,
        *,
        gauges: Sequence[str],
        chain_id: int = CHAIN_ID_BASE,
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            cid = self._require_chain(chain_id)
            gs = [to_checksum_address(g) for g in list(gauges)]
            if not gs:
                return False, "gauges cannot be empty"
            tx = await encode_call(
                target=self.voter,
                abi=AERODROME_VOTER_ABI,
                fn_name="claimRewards",
                args=[gs],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=cid,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    async def get_user_ve_nfts(
        self,
        *,
        owner: str | None = None,
        chain_id: int = CHAIN_ID_BASE,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        try:
            cid = self._require_chain(chain_id)
            if owner:
                owner_addr = to_checksum_address(owner)
            elif self.wallet_address:
                owner_addr = to_checksum_address(self.wallet_address)
            else:
                raise ValueError("address is required")

            async with web3_from_chain_id(cid) as web3:
                ve = web3.eth.contract(
                    address=to_checksum_address(self.voting_escrow),
                    abi=AERODROME_VOTING_ESCROW_ABI,
                )
                bal = await ve.functions.balanceOf(owner_addr).call(
                    block_identifier=block_identifier
                )
                if bal <= 0:
                    return True, []

                id_calls = [
                    Call(
                        ve,
                        "ownerToNFTokenIdList",
                        args=(owner_addr, i),
                        postprocess=int,
                    )
                    for i in range(bal)
                ]
                token_ids = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=cid,
                    calls=id_calls,
                    block_identifier=block_identifier,
                    chunk_size=100,
                )
            return True, token_ids
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def create_lock(
        self,
        *,
        amount: int,
        lock_duration: int,
        chain_id: int = CHAIN_ID_BASE,
    ) -> tuple[bool, Any]:
        if amount <= 0:
            return False, "amount must be positive"
        if lock_duration <= 0:
            return False, "lock_duration must be positive"
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            cid = self._require_chain(chain_id)
            owner = to_checksum_address(self.wallet_address)

            approved = await ensure_allowance(
                token_address=self.aero,
                owner=owner,
                spender=to_checksum_address(self.voting_escrow),
                amount=amount,
                chain_id=cid,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=self.voting_escrow,
                abi=AERODROME_VOTING_ESCROW_ABI,
                fn_name="createLock",
                args=[amount, lock_duration],
                from_address=owner,
                chain_id=cid,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            token_id = await self._minted_erc721_token_id(
                chain_id=cid,
                nft_contract=self.voting_escrow,
                tx_hash=tx_hash,
                expected_to=owner,
            )
            return True, {"tx": tx_hash, "token_id": token_id}
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def create_lock_for(
        self,
        *,
        amount: int,
        lock_duration: int,
        receiver: str,
        chain_id: int = CHAIN_ID_BASE,
    ) -> tuple[bool, Any]:
        if amount <= 0:
            return False, "amount must be positive"
        if lock_duration <= 0:
            return False, "lock_duration must be positive"
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            cid = self._require_chain(chain_id)
            owner = to_checksum_address(self.wallet_address)
            recv = to_checksum_address(receiver)

            approved = await ensure_allowance(
                token_address=self.aero,
                owner=owner,
                spender=to_checksum_address(self.voting_escrow),
                amount=amount,
                chain_id=cid,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=self.voting_escrow,
                abi=AERODROME_VOTING_ESCROW_ABI,
                fn_name="createLockFor",
                args=[amount, lock_duration, recv],
                from_address=owner,
                chain_id=cid,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            token_id = await self._minted_erc721_token_id(
                chain_id=cid,
                nft_contract=self.voting_escrow,
                tx_hash=tx_hash,
                expected_to=recv,
            )
            return True, {"tx": tx_hash, "token_id": token_id}
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def increase_lock_amount(
        self,
        *,
        token_id: int,
        amount: int,
        chain_id: int = CHAIN_ID_BASE,
    ) -> tuple[bool, Any]:
        if amount <= 0:
            return False, "amount must be positive"
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            cid = self._require_chain(chain_id)
            owner = to_checksum_address(self.wallet_address)

            approved = await ensure_allowance(
                token_address=self.aero,
                owner=owner,
                spender=to_checksum_address(self.voting_escrow),
                amount=amount,
                chain_id=cid,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=self.voting_escrow,
                abi=AERODROME_VOTING_ESCROW_ABI,
                fn_name="increaseAmount",
                args=[token_id, amount],
                from_address=owner,
                chain_id=cid,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def increase_unlock_time(
        self,
        *,
        token_id: int,
        lock_duration: int,
        chain_id: int = CHAIN_ID_BASE,
    ) -> tuple[bool, Any]:
        if lock_duration <= 0:
            return False, "lock_duration must be positive"
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            cid = self._require_chain(chain_id)
            tx = await encode_call(
                target=self.voting_escrow,
                abi=AERODROME_VOTING_ESCROW_ABI,
                fn_name="increaseUnlockTime",
                args=[token_id, lock_duration],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=cid,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def withdraw_lock(
        self,
        *,
        token_id: int,
        chain_id: int = CHAIN_ID_BASE,
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            cid = self._require_chain(chain_id)
            tx = await encode_call(
                target=self.voting_escrow,
                abi=AERODROME_VOTING_ESCROW_ABI,
                fn_name="withdraw",
                args=[token_id],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=cid,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def lock_permanent(
        self,
        *,
        token_id: int,
        chain_id: int = CHAIN_ID_BASE,
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            cid = self._require_chain(chain_id)
            tx = await encode_call(
                target=self.voting_escrow,
                abi=AERODROME_VOTING_ESCROW_ABI,
                fn_name="lockPermanent",
                args=[token_id],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=cid,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def unlock_permanent(
        self,
        *,
        token_id: int,
        chain_id: int = CHAIN_ID_BASE,
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            cid = self._require_chain(chain_id)
            tx = await encode_call(
                target=self.voting_escrow,
                abi=AERODROME_VOTING_ESCROW_ABI,
                fn_name="unlockPermanent",
                args=[token_id],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=cid,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def vote(
        self,
        *,
        token_id: int,
        pools: Sequence[str],
        weights: Sequence[int],
        chain_id: int = CHAIN_ID_BASE,
        check_window: bool = True,
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        if not pools:
            return False, "pools cannot be empty"
        if len(pools) != len(weights):
            return False, "pools and weights length mismatch"
        try:
            cid = self._require_chain(chain_id)

            if check_window:
                ok, reason = await self._can_vote_now(chain_id=cid, token_id=token_id)
                if not ok:
                    return False, reason

            pools_cs = [to_checksum_address(p) for p in list(pools)]
            weights_u = list(weights)

            tx = await encode_call(
                target=self.voter,
                abi=AERODROME_VOTER_ABI,
                fn_name="vote",
                args=[token_id, pools_cs, weights_u],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=cid,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def reset_vote(
        self,
        *,
        token_id: int,
        chain_id: int = CHAIN_ID_BASE,
        check_window: bool = True,
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            cid = self._require_chain(chain_id)
            if check_window:
                ok, reason = await self._can_vote_now(chain_id=cid, token_id=token_id)
                if not ok:
                    return False, reason

            tx = await encode_call(
                target=self.voter,
                abi=AERODROME_VOTER_ABI,
                fn_name="reset",
                args=[token_id],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=cid,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    async def _reward_tokens(
        self,
        *,
        reward_contract: str,
        chain_id: int,
        web3: Any,
        block_identifier: str | int = "latest",
    ) -> list[str]:
        rc = to_checksum_address(reward_contract)

        reward = web3.eth.contract(address=rc, abi=AERODROME_REWARD_ABI)
        n = await reward.functions.rewardsListLength().call(
            block_identifier=block_identifier
        )
        if n <= 0:
            return []
        calls = [
            Call(
                reward,
                "rewards",
                args=(i,),
                postprocess=lambda a: to_checksum_address(a),
            )
            for i in range(n)
        ]
        tokens = await read_only_calls_multicall_or_gather(
            web3=web3,
            chain_id=chain_id,
            calls=calls,
            block_identifier=block_identifier,
            chunk_size=100,
        )
        return [to_checksum_address(t) for t in tokens]

    @require_wallet
    async def claim_fees(
        self,
        *,
        token_id: int,
        fee_reward_contracts: Sequence[str],
        token_lists: Sequence[Sequence[str]] | None = None,
        chain_id: int = CHAIN_ID_BASE,
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        if not fee_reward_contracts:
            return False, "fee_reward_contracts cannot be empty"
        try:
            cid = self._require_chain(chain_id)
            fees = [to_checksum_address(a) for a in list(fee_reward_contracts)]

            tokens_nested: list[list[str]]
            if token_lists is not None:
                if len(token_lists) != len(fees):
                    return False, "token_lists length mismatch"
                tokens_nested = [
                    [to_checksum_address(t) for t in list(tokens)]
                    for tokens in list(token_lists)
                ]
            else:
                async with web3_from_chain_id(cid) as web3:
                    toks = await asyncio.gather(
                        *[
                            self._reward_tokens(
                                reward_contract=a,
                                chain_id=cid,
                                web3=web3,
                            )
                            for a in fees
                        ]
                    )
                    tokens_nested = [list(x) for x in toks]

            tx = await encode_call(
                target=self.voter,
                abi=AERODROME_VOTER_ABI,
                fn_name="claimFees",
                args=[fees, tokens_nested, token_id],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=cid,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def claim_bribes(
        self,
        *,
        token_id: int,
        bribe_reward_contracts: Sequence[str],
        token_lists: Sequence[Sequence[str]] | None = None,
        chain_id: int = CHAIN_ID_BASE,
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        if not bribe_reward_contracts:
            return False, "bribe_reward_contracts cannot be empty"
        try:
            cid = self._require_chain(chain_id)
            bribes = [to_checksum_address(a) for a in list(bribe_reward_contracts)]

            tokens_nested: list[list[str]]
            if token_lists is not None:
                if len(token_lists) != len(bribes):
                    return False, "token_lists length mismatch"
                tokens_nested = [
                    [to_checksum_address(t) for t in list(tokens)]
                    for tokens in list(token_lists)
                ]
            else:
                async with web3_from_chain_id(cid) as web3:
                    toks = await asyncio.gather(
                        *[
                            self._reward_tokens(
                                reward_contract=a,
                                chain_id=cid,
                                web3=web3,
                            )
                            for a in bribes
                        ]
                    )
                    tokens_nested = [list(x) for x in toks]

            tx = await encode_call(
                target=self.voter,
                abi=AERODROME_VOTER_ABI,
                fn_name="claimBribes",
                args=[bribes, tokens_nested, token_id],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=cid,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    async def get_rebase_claimable(
        self,
        *,
        token_id: int,
        chain_id: int = CHAIN_ID_BASE,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        try:
            cid = self._require_chain(chain_id)
            async with web3_from_chain_id(cid) as web3:
                rd = web3.eth.contract(
                    address=to_checksum_address(self.rewards_distributor),
                    abi=AERODROME_REWARDS_DISTRIBUTOR_ABI,
                )
                claimable = await rd.functions.claimable(token_id).call(
                    block_identifier=block_identifier
                )
            return True, claimable
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def claim_rebases(
        self,
        *,
        token_id: int,
        chain_id: int = CHAIN_ID_BASE,
        skip_if_zero: bool = True,
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            cid = self._require_chain(chain_id)
            if skip_if_zero:
                ok, claimable = await self.get_rebase_claimable(
                    token_id=token_id, chain_id=cid, block_identifier="latest"
                )
                if not ok:
                    return False, claimable
                if claimable <= 0:
                    return True, {"tx": None, "claimable": claimable}

            tx = await encode_call(
                target=self.rewards_distributor,
                abi=AERODROME_REWARDS_DISTRIBUTOR_ABI,
                fn_name="claim",
                args=[token_id],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=cid,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def claim_rebases_many(
        self,
        *,
        token_ids: Sequence[int],
        chain_id: int = CHAIN_ID_BASE,
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        if not token_ids:
            return False, "token_ids cannot be empty"
        try:
            cid = self._require_chain(chain_id)
            ids = list(token_ids)
            tx = await encode_call(
                target=self.rewards_distributor,
                abi=AERODROME_REWARDS_DISTRIBUTOR_ABI,
                fn_name="claimMany",
                args=[ids],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=cid,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    async def get_full_user_state(
        self,
        *,
        account: str,
        chain_id: int = CHAIN_ID_BASE,
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
            cid = self._require_chain(chain_id)
            acct = to_checksum_address(account)
            start_i = max(0, start)

            async with web3_from_chain_id(cid) as web3:
                voter = web3.eth.contract(
                    address=to_checksum_address(self.voter), abi=AERODROME_VOTER_ABI
                )
                ve = web3.eth.contract(
                    address=to_checksum_address(self.voting_escrow),
                    abi=AERODROME_VOTING_ESCROW_ABI,
                )
                rd = web3.eth.contract(
                    address=to_checksum_address(self.rewards_distributor),
                    abi=AERODROME_REWARDS_DISTRIBUTOR_ABI,
                )

                total = await voter.functions.length().call(
                    block_identifier=block_identifier
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
                    chain_id=cid,
                    calls=pool_calls,
                    block_identifier=block_identifier,
                    chunk_size=100,
                )

                pool_contracts = [
                    web3.eth.contract(address=p, abi=AERODROME_POOL_ABI) for p in pools
                ]
                pool_bal_calls = [
                    Call(pc, "balanceOf", args=(acct,), postprocess=int)
                    for pc in pool_contracts
                ]
                gauge_calls = [
                    Call(voter, "gauges", args=(p,), postprocess=to_checksum_address)
                    for p in pools
                ]
                pool_balances, gauges = await asyncio.gather(
                    read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=cid,
                        calls=pool_bal_calls,
                        block_identifier=block_identifier,
                        chunk_size=100,
                    ),
                    read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=cid,
                        calls=gauge_calls,
                        block_identifier=block_identifier,
                        chunk_size=100,
                    ),
                )

                gauge_contracts: dict[str, Any] = {}
                for g in gauges:
                    if g.lower() == ZERO_ADDRESS:
                        continue
                    if g.lower() not in gauge_contracts:
                        gauge_contracts[g.lower()] = web3.eth.contract(
                            address=to_checksum_address(g), abi=AERODROME_GAUGE_ABI
                        )

                gauge_bal_calls = [
                    Call(g, "balanceOf", args=(acct,), postprocess=int)
                    for g in gauge_contracts.values()
                ]
                gauge_earned_calls = [
                    Call(g, "earned", args=(acct,), postprocess=int)
                    for g in gauge_contracts.values()
                ]
                (g_bal, g_earned) = await asyncio.gather(
                    read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=cid,
                        calls=gauge_bal_calls,
                        block_identifier=block_identifier,
                        chunk_size=100,
                    ),
                    read_only_calls_multicall_or_gather(
                        web3=web3,
                        chain_id=cid,
                        calls=gauge_earned_calls,
                        block_identifier=block_identifier,
                        chunk_size=100,
                    ),
                )

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

                ok_ids, token_ids_any = await self.get_user_ve_nfts(
                    owner=acct, chain_id=cid, block_identifier=block_identifier
                )
                if not ok_ids:
                    return False, token_ids_any
                token_ids = token_ids_any

                ve_items: list[dict[str, Any]] = []
                if token_ids:
                    power_calls = [
                        Call(ve, "balanceOfNFT", args=(tid,), postprocess=int)
                        for tid in token_ids
                    ]
                    voted_calls = [
                        Call(ve, "voted", args=(tid,), postprocess=bool)
                        for tid in token_ids
                    ]
                    claimable_calls = [
                        Call(rd, "claimable", args=(tid,), postprocess=int)
                        for tid in token_ids
                    ]
                    (powers, voted_flags, claimables) = await asyncio.gather(
                        read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=cid,
                            calls=power_calls,
                            block_identifier=block_identifier,
                            chunk_size=100,
                        ),
                        read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=cid,
                            calls=voted_calls,
                            block_identifier=block_identifier,
                            chunk_size=100,
                        ),
                        read_only_calls_multicall_or_gather(
                            web3=web3,
                            chain_id=cid,
                            calls=claimable_calls,
                            block_identifier=block_identifier,
                            chunk_size=100,
                        ),
                    )

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
                            chain_id=cid,
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
                "chain_id": cid,
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
