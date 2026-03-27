from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from typing import Any

from eth_utils import to_checksum_address
from hexbytes import HexBytes
from web3._utils.events import event_abi_to_log_topic, get_event_data

from wayfinder_paths.core.adapters.BaseAdapter import require_wallet
from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.aerodrome_abi import (
    AERODROME_REWARD_ABI,
    AERODROME_REWARDS_DISTRIBUTOR_ABI,
    AERODROME_VOTER_ABI,
    AERODROME_VOTING_ESCROW_ABI,
)
from wayfinder_paths.core.constants.base import MAX_UINT256
from wayfinder_paths.core.constants.erc721_abi import ERC721_TRANSFER_EVENT_ABI
from wayfinder_paths.core.utils.multicall import (
    Call,
    read_only_calls_multicall_or_gather,
)
from wayfinder_paths.core.utils.tokens import (
    ensure_allowance,
    get_erc20_metadata,
    get_token_decimals,
)
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

WEEK_SECONDS = 7 * 24 * 60 * 60
EPOCH_SPECIAL_WINDOW_SECONDS = 60 * 60
AERODROME_TOKEN_PRICE_USDC_TTL_SECONDS = 20.0
VE_MAXTIME_SECONDS = 4 * 365 * 24 * 60 * 60

_ERC721_TRANSFER_TOPIC0 = HexBytes(event_abi_to_log_topic(ERC721_TRANSFER_EVENT_ABI))


class AerodromeTokenHelpersMixin:
    chain_id: int
    _token_decimals_cache: dict[str, int]
    _token_symbol_cache: dict[str, str]

    async def _fetch_token_decimals(self, token_addr: str) -> int:
        return await get_token_decimals(token_addr, self.chain_id)

    async def _fetch_token_symbol(self, token_addr: str) -> str:
        async with web3_from_chain_id(self.chain_id) as web3:
            symbol, _name, _decimals = await get_erc20_metadata(token_addr, web3=web3)
        return symbol

    async def _resolve_token_price_usdc(
        self,
        token: str,
        *,
        price_usdc: float | None = None,
    ) -> float | None:
        raise NotImplementedError

    async def token_decimals(self, token: str) -> int:
        token_addr = to_checksum_address(token)
        cached = self._token_decimals_cache.get(token_addr)
        if cached is not None:
            return cached

        decimals = await self._fetch_token_decimals(token_addr)
        self._token_decimals_cache[token_addr] = decimals
        return decimals

    async def token_amount_usdc(
        self,
        *,
        token: str,
        amount_raw: int,
        price_usdc: float | None = None,
    ) -> float | None:
        if amount_raw == 0:
            return 0.0
        if amount_raw < 0:
            return None

        decimals = await self.token_decimals(token)
        resolved_price = await self._resolve_token_price_usdc(
            token,
            price_usdc=price_usdc,
        )
        if resolved_price is None:
            return None

        if not math.isfinite(resolved_price) or resolved_price <= 0:
            return None
        return (amount_raw / (10**decimals)) * resolved_price

    async def token_price_usdc(self, token: str) -> float | None:
        return await self._resolve_token_price_usdc(token)

    async def token_symbol(self, token: str) -> str:
        token_addr = to_checksum_address(token)
        cached = self._token_symbol_cache.get(token_addr)
        if cached is not None:
            return cached

        symbol = await self._fetch_token_symbol(token_addr)
        self._token_symbol_cache[token_addr] = symbol
        return symbol


class AerodromeVotingRewardsMixin:
    chain_id: int
    core_contracts: dict[str, str]

    async def _minted_erc721_token_id(
        self,
        *,
        nft_contract: str,
        tx_hash: str,
        expected_to: str,
    ) -> int | None:
        nft_l = to_checksum_address(nft_contract).lower()
        expected_to_l = to_checksum_address(expected_to).lower()
        async with web3_from_chain_id(self.chain_id) as web3:
            receipt = await web3.eth.get_transaction_receipt(tx_hash)
            logs = (receipt or {}).get("logs") or []
            for log in logs if isinstance(logs, list) else []:
                try:
                    if (log.get("address") or "").lower() != nft_l:
                        continue
                    topics = log.get("topics") or []
                    if not topics or HexBytes(topics[0]) != _ERC721_TRANSFER_TOPIC0:
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

    async def _can_vote_now(self, *, token_id: int | None = None) -> tuple[bool, str]:
        async with web3_from_chain_id(self.chain_id) as web3:
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
                    address=to_checksum_address(self.core_contracts["voter"]),
                    abi=AERODROME_VOTER_ABI,
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

    async def ve_balance_of_nft(
        self,
        *,
        token_id: int,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        try:
            async with web3_from_chain_id(self.chain_id) as web3:
                ve = web3.eth.contract(
                    address=to_checksum_address(self.core_contracts["voting_escrow"]),
                    abi=AERODROME_VOTING_ESCROW_ABI,
                )
                balance = await ve.functions.balanceOfNFT(token_id).call(
                    block_identifier=block_identifier
                )
            return True, balance
        except Exception as exc:
            return False, str(exc)

    async def ve_locked(
        self,
        *,
        token_id: int,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        try:
            async with web3_from_chain_id(self.chain_id) as web3:
                ve = web3.eth.contract(
                    address=to_checksum_address(self.core_contracts["voting_escrow"]),
                    abi=AERODROME_VOTING_ESCROW_ABI,
                )
                locked = await ve.functions.locked(token_id).call(
                    block_identifier=block_identifier
                )
            if (
                isinstance(locked, (list, tuple))
                and len(locked) == 1
                and isinstance(locked[0], (list, tuple))
            ):
                locked = locked[0]
            amount, end, is_permanent = locked
            return True, {
                "amount": amount,
                "end": end,
                "is_permanent": is_permanent,
            }
        except Exception as exc:
            return False, str(exc)

    async def can_vote_now(
        self,
        *,
        token_id: int,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        try:
            async with web3_from_chain_id(self.chain_id) as web3:
                latest = await web3.eth.get_block(block_identifier)
                now = latest.get("timestamp") or 0
                epoch_start = (now // WEEK_SECONDS) * WEEK_SECONDS
                next_epoch_start = epoch_start + WEEK_SECONDS

                voter = web3.eth.contract(
                    address=to_checksum_address(self.core_contracts["voter"]),
                    abi=AERODROME_VOTER_ABI,
                )
                last_voted = await voter.functions.lastVoted(token_id).call(
                    block_identifier=block_identifier
                )

            return True, {
                "can_vote": last_voted < epoch_start,
                "last_voted": last_voted,
                "epoch_start": epoch_start,
                "next_epoch_start": next_epoch_start,
            }
        except Exception as exc:
            return False, str(exc)

    async def estimate_votes_for_lock(
        self,
        *,
        aero_amount_raw: int,
        lock_duration: int,
    ) -> tuple[bool, Any]:
        if aero_amount_raw <= 0 or lock_duration <= 0:
            return True, 0
        effective_duration = min(lock_duration, VE_MAXTIME_SECONDS)
        return True, aero_amount_raw * effective_duration // VE_MAXTIME_SECONDS

    async def estimate_ve_apr_percent(
        self,
        *,
        usdc_per_ve: float,
        votes_raw: int,
        aero_locked_raw: int,
    ) -> tuple[bool, Any]:
        if votes_raw <= 0 or aero_locked_raw <= 0:
            return True, None

        aero_price = await self.token_price_usdc(self.core_contracts["aero"])
        if aero_price is None or not math.isfinite(aero_price) or aero_price <= 0:
            return True, None

        aero_decimals = await self.token_decimals(self.core_contracts["aero"])
        locked_value_usdc = (aero_locked_raw / (10**aero_decimals)) * aero_price
        if locked_value_usdc <= 0:
            return True, None

        weekly_reward_usdc = (votes_raw / 1e18) * usdc_per_ve
        return True, (weekly_reward_usdc * 52.0 / locked_value_usdc) * 100.0

    async def get_reward_contracts(
        self,
        *,
        gauge: str,
    ) -> tuple[bool, Any]:
        try:
            gauge_addr = to_checksum_address(gauge)
            async with web3_from_chain_id(self.chain_id) as web3:
                voter = web3.eth.contract(
                    address=to_checksum_address(self.core_contracts["voter"]),
                    abi=AERODROME_VOTER_ABI,
                )
                fees, bribes = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=self.chain_id,
                    calls=[
                        Call(
                            voter,
                            "gaugeToFees",
                            args=(gauge_addr,),
                            postprocess=to_checksum_address,
                        ),
                        Call(
                            voter,
                            "gaugeToBribe",
                            args=(gauge_addr,),
                            postprocess=to_checksum_address,
                        ),
                    ],
                    block_identifier="latest",
                )
            return True, {"fees": fees, "bribes": bribes}
        except Exception as exc:
            return False, str(exc)

    @require_wallet
    async def claim_gauge_rewards(
        self,
        *,
        gauges: Sequence[str],
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        if not gauges:
            return False, "gauges cannot be empty"
        try:
            gauge_addrs = [to_checksum_address(g) for g in gauges]
            tx = await encode_call(
                target=self.core_contracts["voter"],
                abi=AERODROME_VOTER_ABI,
                fn_name="claimRewards",
                args=[gauge_addrs],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=self.chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    async def get_user_ve_nfts(
        self,
        *,
        owner: str | None = None,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        try:
            if owner:
                owner_addr = to_checksum_address(owner)
            elif self.wallet_address:
                owner_addr = to_checksum_address(self.wallet_address)
            else:
                raise ValueError("address is required")

            async with web3_from_chain_id(self.chain_id) as web3:
                ve = web3.eth.contract(
                    address=to_checksum_address(self.core_contracts["voting_escrow"]),
                    abi=AERODROME_VOTING_ESCROW_ABI,
                )
                bal = await ve.functions.balanceOf(owner_addr).call(
                    block_identifier=block_identifier
                )
                if bal <= 0:
                    return True, []

                token_ids = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=self.chain_id,
                    calls=[
                        Call(
                            ve,
                            "ownerToNFTokenIdList",
                            args=(owner_addr, i),
                        )
                        for i in range(bal)
                    ],
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
    ) -> tuple[bool, Any]:
        if amount <= 0:
            return False, "amount must be positive"
        if lock_duration <= 0:
            return False, "lock_duration must be positive"
        if self.sign_callback is None:
            return False, "sign_callback is required"

        try:
            owner = to_checksum_address(self.wallet_address)
            approved = await ensure_allowance(
                token_address=self.core_contracts["aero"],
                owner=owner,
                spender=self.core_contracts["voting_escrow"],
                amount=amount,
                chain_id=self.chain_id,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=self.core_contracts["voting_escrow"],
                abi=AERODROME_VOTING_ESCROW_ABI,
                fn_name="createLock",
                args=[amount, lock_duration],
                from_address=owner,
                chain_id=self.chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            token_id = await self._minted_erc721_token_id(
                nft_contract=self.core_contracts["voting_escrow"],
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
    ) -> tuple[bool, Any]:
        if amount <= 0:
            return False, "amount must be positive"
        if lock_duration <= 0:
            return False, "lock_duration must be positive"
        if self.sign_callback is None:
            return False, "sign_callback is required"

        try:
            owner = to_checksum_address(self.wallet_address)
            receiver_addr = to_checksum_address(receiver)
            approved = await ensure_allowance(
                token_address=self.core_contracts["aero"],
                owner=owner,
                spender=self.core_contracts["voting_escrow"],
                amount=amount,
                chain_id=self.chain_id,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=self.core_contracts["voting_escrow"],
                abi=AERODROME_VOTING_ESCROW_ABI,
                fn_name="createLockFor",
                args=[amount, lock_duration, receiver_addr],
                from_address=owner,
                chain_id=self.chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            token_id = await self._minted_erc721_token_id(
                nft_contract=self.core_contracts["voting_escrow"],
                tx_hash=tx_hash,
                expected_to=receiver_addr,
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
    ) -> tuple[bool, Any]:
        if amount <= 0:
            return False, "amount must be positive"
        if self.sign_callback is None:
            return False, "sign_callback is required"

        try:
            owner = to_checksum_address(self.wallet_address)
            approved = await ensure_allowance(
                token_address=self.core_contracts["aero"],
                owner=owner,
                spender=self.core_contracts["voting_escrow"],
                amount=amount,
                chain_id=self.chain_id,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            tx = await encode_call(
                target=self.core_contracts["voting_escrow"],
                abi=AERODROME_VOTING_ESCROW_ABI,
                fn_name="increaseAmount",
                args=[token_id, amount],
                from_address=owner,
                chain_id=self.chain_id,
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
    ) -> tuple[bool, Any]:
        if lock_duration <= 0:
            return False, "lock_duration must be positive"
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            tx = await encode_call(
                target=self.core_contracts["voting_escrow"],
                abi=AERODROME_VOTING_ESCROW_ABI,
                fn_name="increaseUnlockTime",
                args=[token_id, lock_duration],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=self.chain_id,
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
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            tx = await encode_call(
                target=self.core_contracts["voting_escrow"],
                abi=AERODROME_VOTING_ESCROW_ABI,
                fn_name="withdraw",
                args=[token_id],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=self.chain_id,
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
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            tx = await encode_call(
                target=self.core_contracts["voting_escrow"],
                abi=AERODROME_VOTING_ESCROW_ABI,
                fn_name="lockPermanent",
                args=[token_id],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=self.chain_id,
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
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            tx = await encode_call(
                target=self.core_contracts["voting_escrow"],
                abi=AERODROME_VOTING_ESCROW_ABI,
                fn_name="unlockPermanent",
                args=[token_id],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=self.chain_id,
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
        check_window: bool = True,
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        if not pools:
            return False, "pools cannot be empty"
        if len(pools) != len(weights):
            return False, "pools and weights length mismatch"

        try:
            if check_window:
                ok, reason = await self._can_vote_now(token_id=token_id)
                if not ok:
                    return False, reason

            tx = await encode_call(
                target=self.core_contracts["voter"],
                abi=AERODROME_VOTER_ABI,
                fn_name="vote",
                args=[
                    token_id,
                    [to_checksum_address(p) for p in pools],
                    weights,
                ],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=self.chain_id,
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
        check_window: bool = True,
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        try:
            if check_window:
                ok, reason = await self._can_vote_now(token_id=token_id)
                if not ok:
                    return False, reason

            tx = await encode_call(
                target=self.core_contracts["voter"],
                abi=AERODROME_VOTER_ABI,
                fn_name="reset",
                args=[token_id],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=self.chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    async def _reward_tokens(
        self,
        *,
        reward_contract: str,
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
            chain_id=self.chain_id,
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
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        if not fee_reward_contracts:
            return False, "fee_reward_contracts cannot be empty"
        try:
            fees = [to_checksum_address(a) for a in fee_reward_contracts]

            tokens_nested: list[list[str]]
            if token_lists is not None:
                if len(token_lists) != len(fees):
                    return False, "token_lists length mismatch"
                tokens_nested = [
                    [to_checksum_address(t) for t in tokens] for tokens in token_lists
                ]
            else:
                async with web3_from_chain_id(self.chain_id) as web3:
                    nested = await asyncio.gather(
                        *[
                            self._reward_tokens(
                                reward_contract=contract,
                                web3=web3,
                            )
                            for contract in fees
                        ]
                    )
                tokens_nested = nested

            tx = await encode_call(
                target=self.core_contracts["voter"],
                abi=AERODROME_VOTER_ABI,
                fn_name="claimFees",
                args=[fees, tokens_nested, token_id],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=self.chain_id,
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
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        if not bribe_reward_contracts:
            return False, "bribe_reward_contracts cannot be empty"
        try:
            bribes = [to_checksum_address(a) for a in bribe_reward_contracts]

            tokens_nested: list[list[str]]
            if token_lists is not None:
                if len(token_lists) != len(bribes):
                    return False, "token_lists length mismatch"
                tokens_nested = [
                    [to_checksum_address(t) for t in tokens] for tokens in token_lists
                ]
            else:
                async with web3_from_chain_id(self.chain_id) as web3:
                    nested = await asyncio.gather(
                        *[
                            self._reward_tokens(
                                reward_contract=contract,
                                web3=web3,
                            )
                            for contract in bribes
                        ]
                    )
                tokens_nested = nested

            tx = await encode_call(
                target=self.core_contracts["voter"],
                abi=AERODROME_VOTER_ABI,
                fn_name="claimBribes",
                args=[bribes, tokens_nested, token_id],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=self.chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    async def get_rebase_claimable(
        self,
        *,
        token_id: int,
        block_identifier: str | int = "latest",
    ) -> tuple[bool, Any]:
        try:
            async with web3_from_chain_id(self.chain_id) as web3:
                rd = web3.eth.contract(
                    address=to_checksum_address(
                        self.core_contracts["rewards_distributor"]
                    ),
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
        skip_if_zero: bool = True,
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"

        try:
            if skip_if_zero:
                ok, claimable = await self.get_rebase_claimable(
                    token_id=token_id,
                    block_identifier="latest",
                )
                if not ok:
                    return False, claimable
                if claimable <= 0:
                    return True, {"tx": None, "claimable": claimable}

            tx = await encode_call(
                target=self.core_contracts["rewards_distributor"],
                abi=AERODROME_REWARDS_DISTRIBUTOR_ABI,
                fn_name="claim",
                args=[token_id],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=self.chain_id,
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
    ) -> tuple[bool, Any]:
        if self.sign_callback is None:
            return False, "sign_callback is required"
        if not token_ids:
            return False, "token_ids cannot be empty"
        try:
            tx = await encode_call(
                target=self.core_contracts["rewards_distributor"],
                abi=AERODROME_REWARDS_DISTRIBUTOR_ABI,
                fn_name="claimMany",
                args=[token_ids],
                from_address=to_checksum_address(self.wallet_address),
                chain_id=self.chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)
