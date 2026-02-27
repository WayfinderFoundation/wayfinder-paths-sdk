from __future__ import annotations

import asyncio
from typing import Any

from eth_utils import to_checksum_address
from hexbytes import HexBytes
from web3._utils.events import event_abi_to_log_topic, get_event_data

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.constants.base import MANTISSA, MAX_UINT256
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM
from wayfinder_paths.core.constants.contracts import (
    EIGEN_TOKEN,
    EIGENCLOUD_BEACON_CHAIN_ETH_STRATEGY_SENTINEL,
    EIGENCLOUD_DELEGATION_MANAGER,
    EIGENCLOUD_EIGEN_STRATEGY,
    EIGENCLOUD_REWARDS_COORDINATOR,
    EIGENCLOUD_STRATEGIES,
    EIGENCLOUD_STRATEGY_MANAGER,
    ZERO_ADDRESS,
)
from wayfinder_paths.core.constants.eigencloud_abi import (
    IDELEGATION_MANAGER_ABI,
    IREWARDS_COORDINATOR_ABI,
    ISTRATEGY_ABI,
    ISTRATEGY_MANAGER_ABI,
)
from wayfinder_paths.core.utils.tokens import ensure_allowance, get_erc20_metadata
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

CHAIN_NAME = "ethereum"

_SLASHING_WITHDRAWAL_QUEUED_EVENT_ABI = next(
    i
    for i in IDELEGATION_MANAGER_ABI
    if i.get("type") == "event" and i.get("name") == "SlashingWithdrawalQueued"
)
_SLASHING_WITHDRAWAL_QUEUED_TOPIC0 = HexBytes(
    event_abi_to_log_topic(_SLASHING_WITHDRAWAL_QUEUED_EVENT_ABI)
)


def _as_bytes(data: bytes | str) -> bytes:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    s = str(data).strip()
    if not s:
        return b""
    if s.startswith("0x"):
        return bytes(HexBytes(s))
    # Best-effort: interpret as hex without 0x.
    return bytes(HexBytes("0x" + s))


def _as_bytes32_hex(value: Any) -> str:
    if value is None:
        return "0x" + ("00" * 32)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return "0x" + ("00" * 32)
        hb = HexBytes(s if s.startswith("0x") else "0x" + s)
        if len(hb) > 32:
            raise ValueError(f"bytes32 too long: {len(hb)}")
        return "0x" + hb.rjust(32, b"\x00").hex()
    if isinstance(value, (bytes, bytearray, HexBytes)):
        b = bytes(value)
        if len(b) > 32:
            raise ValueError(f"bytes32 too long: {len(b)}")
        return "0x" + HexBytes(b).rjust(32, b"\x00").hex()
    if isinstance(value, int):
        if value < 0:
            raise ValueError("bytes32 int must be non-negative")
        return "0x" + HexBytes(value.to_bytes(32, "big")).hex()
    raise TypeError("expected bytes32 as hex string, bytes, or int")


class EigenCloudAdapter(BaseAdapter):
    """Adapter for EigenCloud (EigenLayer) restaking on Ethereum mainnet.

    Notes:
    - Restaking is share accounting (strategies), not ERC-4626.
    - Delegation is optional; withdrawals are delayed via a queue.
    - Rewards are claimed via merkle proofs (offchain-prepared claim structs).
    """

    adapter_type = "EIGENCLOUD"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        sign_callback: Any | None = None,
        wallet_address: str | None = None,
    ) -> None:
        super().__init__("eigencloud_adapter", config)
        self.sign_callback = sign_callback

        self.chain_id = CHAIN_ID_ETHEREUM
        self.chain_name = CHAIN_NAME

        self.strategy_manager: str = EIGENCLOUD_STRATEGY_MANAGER
        self.delegation_manager: str = EIGENCLOUD_DELEGATION_MANAGER
        self.rewards_coordinator: str = EIGENCLOUD_REWARDS_COORDINATOR

        self.wallet_address: str | None = (
            to_checksum_address(wallet_address) if wallet_address else None
        )

    async def get_all_markets(
        self,
        *,
        include_total_shares: bool = True,
        include_share_to_underlying: bool = True,
        block_identifier: int | str | None = None,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        block_id = block_identifier if block_identifier is not None else "pending"
        try:
            async with web3_from_chain_id(self.chain_id) as web3:
                sm = web3.eth.contract(
                    address=self.strategy_manager, abi=ISTRATEGY_MANAGER_ABI
                )
                out: list[dict[str, Any]] = []

                for name, strategy in EIGENCLOUD_STRATEGIES.items():
                    strat = to_checksum_address(strategy)
                    strat_contract = web3.eth.contract(address=strat, abi=ISTRATEGY_ABI)

                    whitelisted_coro = sm.functions.strategyIsWhitelistedForDeposit(
                        strat
                    ).call(block_identifier=block_id)

                    # EigenStrategy can accept EIGEN or bEIGEN, so treat underlying token
                    # as "EIGEN" for display purposes by default.
                    underlying_coro = (
                        asyncio.sleep(0, result=EIGEN_TOKEN)
                        if strat.lower() == EIGENCLOUD_EIGEN_STRATEGY.lower()
                        else strat_contract.functions.underlyingToken().call(
                            block_identifier=block_id
                        )
                    )

                    total_shares_coro = (
                        strat_contract.functions.totalShares().call(
                            block_identifier=block_id
                        )
                        if include_total_shares
                        else asyncio.sleep(0, result=0)
                    )

                    shares_to_underlying_coro = (
                        strat_contract.functions.sharesToUnderlyingView(MANTISSA).call(
                            block_identifier=block_id
                        )
                        if include_share_to_underlying
                        else asyncio.sleep(0, result=0)
                    )

                    whitelisted, underlying, total_shares, share_price = (
                        await asyncio.gather(
                            whitelisted_coro,
                            underlying_coro,
                            total_shares_coro,
                            shares_to_underlying_coro,
                        )
                    )

                    underlying_addr = to_checksum_address(str(underlying))
                    symbol, token_name, decimals = await get_erc20_metadata(
                        underlying_addr,
                        self.chain_id,
                        web3=web3,
                        block_identifier=block_id,
                    )

                    out.append(
                        {
                            "chain_id": int(self.chain_id),
                            "strategy": strat,
                            "strategy_name": name,
                            "underlying": underlying_addr,
                            "underlying_symbol": symbol or "",
                            "underlying_name": token_name or "",
                            "underlying_decimals": decimals or 0,
                            "is_whitelisted_for_deposit": whitelisted,
                            "total_shares": total_shares or 0,
                            # underlying units per 1e18 shares
                            "shares_to_underlying_1e18": share_price or 0,
                        }
                    )

                return True, out
        except Exception as exc:
            return False, str(exc)

    async def deposit(
        self,
        *,
        strategy: str,
        amount: int,
        token: str | None = None,
        check_whitelist: bool = True,
        block_identifier: int | str | None = None,
    ) -> tuple[bool, Any]:
        wallet = self.wallet_address
        if not wallet:
            return False, "wallet_address is required"
        if not self.sign_callback:
            return False, "sign_callback is required"

        amt = int(amount)
        if amt <= 0:
            return False, "amount must be positive"

        strat = to_checksum_address(strategy)
        block_id = block_identifier if block_identifier is not None else "pending"

        tok: str | None = None
        try:
            if token is not None:
                tok = to_checksum_address(token)
            elif strat.lower() == EIGENCLOUD_EIGEN_STRATEGY.lower():
                tok = EIGEN_TOKEN

            async with web3_from_chain_id(self.chain_id) as web3:
                if tok is None:
                    s = web3.eth.contract(address=strat, abi=ISTRATEGY_ABI)
                    underlying = await s.functions.underlyingToken().call(
                        block_identifier=block_id
                    )
                    tok = to_checksum_address(str(underlying))

                if check_whitelist:
                    sm = web3.eth.contract(
                        address=self.strategy_manager, abi=ISTRATEGY_MANAGER_ABI
                    )
                    whitelisted = await sm.functions.strategyIsWhitelistedForDeposit(
                        strat
                    ).call(block_identifier=block_id)
                    if not whitelisted:
                        return False, f"strategy not whitelisted for deposit: {strat}"
        except Exception as exc:
            return False, str(exc)

        if tok is None:
            return False, "failed to resolve deposit token"

        try:
            ok_appr, appr = await ensure_allowance(
                token_address=tok,
                owner=wallet,
                spender=self.strategy_manager,
                amount=amt,
                chain_id=self.chain_id,
                signing_callback=self.sign_callback,
                approval_amount=MAX_UINT256,
            )
            if not ok_appr:
                return False, appr
            approve_tx_hash = (
                appr if isinstance(appr, str) and appr.startswith("0x") else None
            )

            tx = await encode_call(
                target=self.strategy_manager,
                abi=ISTRATEGY_MANAGER_ABI,
                fn_name="depositIntoStrategy",
                args=[strat, tok, amt],
                from_address=wallet,
                chain_id=self.chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, {
                "tx_hash": tx_hash,
                "approve_tx_hash": approve_tx_hash,
                "strategy": strat,
                "token": tok,
                "amount": amt,
            }
        except Exception as exc:
            return False, str(exc)

    async def get_delegation_state(
        self,
        *,
        account: str | None = None,
        block_identifier: int | str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        acct = to_checksum_address(account) if account else self.wallet_address
        if not acct:
            return False, "account (or wallet_address) is required"

        block_id = block_identifier if block_identifier is not None else "pending"
        try:
            async with web3_from_chain_id(self.chain_id) as web3:
                dm = web3.eth.contract(
                    address=self.delegation_manager, abi=IDELEGATION_MANAGER_ABI
                )
                is_delegated_coro = dm.functions.isDelegated(acct).call(
                    block_identifier=block_id
                )
                delegated_to_coro = dm.functions.delegatedTo(acct).call(
                    block_identifier=block_id
                )
                is_delegated, delegated_to = await asyncio.gather(
                    is_delegated_coro, delegated_to_coro
                )

                delegated_to_addr = to_checksum_address(str(delegated_to))
                operator_approver: str | None = None
                if is_delegated and delegated_to_addr != ZERO_ADDRESS:
                    try:
                        operator_approver = to_checksum_address(
                            str(
                                await dm.functions.delegationApprover(
                                    delegated_to_addr
                                ).call(block_identifier=block_id)
                            )
                        )
                    except Exception:
                        operator_approver = None

                return True, {
                    "isDelegated": is_delegated,
                    "delegatedTo": delegated_to_addr,
                    "operatorApprover": operator_approver,
                }
        except Exception as exc:
            return False, str(exc)

    async def delegate(
        self,
        *,
        operator: str,
        approver_signature: bytes | str = b"",
        approver_expiry: int = 0,
        approver_salt: Any = None,
    ) -> tuple[bool, Any]:
        wallet = self.wallet_address
        if not wallet:
            return False, "wallet_address is required"
        if not self.sign_callback:
            return False, "sign_callback is required"

        op = to_checksum_address(operator)
        sig = _as_bytes(approver_signature)
        expiry = int(approver_expiry or 0)
        salt = _as_bytes32_hex(approver_salt)

        try:
            tx = await encode_call(
                target=self.delegation_manager,
                abi=IDELEGATION_MANAGER_ABI,
                fn_name="delegateTo",
                args=[op, (sig, expiry), salt],
                from_address=wallet,
                chain_id=self.chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    async def undelegate(
        self,
        *,
        staker: str | None = None,
        include_withdrawal_roots: bool = True,
        block_identifier: int | str | None = None,
    ) -> tuple[bool, Any]:
        wallet = self.wallet_address
        if not wallet:
            return False, "wallet_address is required"
        if not self.sign_callback:
            return False, "sign_callback is required"

        stk = to_checksum_address(staker) if staker else wallet

        try:
            tx = await encode_call(
                target=self.delegation_manager,
                abi=IDELEGATION_MANAGER_ABI,
                fn_name="undelegate",
                args=[stk],
                from_address=wallet,
                chain_id=self.chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            payload: dict[str, Any] = {"tx_hash": tx_hash}
            if include_withdrawal_roots:
                ok, roots = await self.get_withdrawal_roots_from_tx_hash(
                    tx_hash=str(tx_hash)
                )
                if ok and isinstance(roots, list):
                    payload["withdrawal_roots"] = roots
            return True, payload
        except Exception as exc:
            return False, str(exc)

    async def redelegate(
        self,
        *,
        new_operator: str,
        approver_signature: bytes | str = b"",
        approver_expiry: int = 0,
        approver_salt: Any = None,
        include_withdrawal_roots: bool = True,
        block_identifier: int | str | None = None,
    ) -> tuple[bool, Any]:
        wallet = self.wallet_address
        if not wallet:
            return False, "wallet_address is required"
        if not self.sign_callback:
            return False, "sign_callback is required"

        op = to_checksum_address(new_operator)
        sig = _as_bytes(approver_signature)
        expiry = int(approver_expiry or 0)
        salt = _as_bytes32_hex(approver_salt)

        try:
            tx = await encode_call(
                target=self.delegation_manager,
                abi=IDELEGATION_MANAGER_ABI,
                fn_name="redelegate",
                args=[op, (sig, expiry), salt],
                from_address=wallet,
                chain_id=self.chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            payload: dict[str, Any] = {"tx_hash": tx_hash}
            if include_withdrawal_roots:
                ok, roots = await self.get_withdrawal_roots_from_tx_hash(
                    tx_hash=str(tx_hash)
                )
                if ok and isinstance(roots, list):
                    payload["withdrawal_roots"] = roots
            return True, payload
        except Exception as exc:
            return False, str(exc)

    async def queue_withdrawals(
        self,
        *,
        strategies: list[str],
        deposit_shares: list[int],
        include_withdrawal_roots: bool = True,
        block_identifier: int | str | None = None,
    ) -> tuple[bool, Any]:
        wallet = self.wallet_address
        if not wallet:
            return False, "wallet_address is required"
        if not self.sign_callback:
            return False, "sign_callback is required"

        if not strategies:
            return False, "strategies is required"
        if len(strategies) != len(deposit_shares):
            return False, "strategies and deposit_shares must have equal length"

        strats = [to_checksum_address(s) for s in strategies]
        if any(s <= 0 for s in deposit_shares):
            return False, "all deposit_shares must be positive"

        params = [(strats, deposit_shares, wallet)]

        try:
            tx = await encode_call(
                target=self.delegation_manager,
                abi=IDELEGATION_MANAGER_ABI,
                fn_name="queueWithdrawals",
                args=[params],
                from_address=wallet,
                chain_id=self.chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            payload: dict[str, Any] = {"tx_hash": tx_hash}
            if include_withdrawal_roots:
                ok, roots = await self.get_withdrawal_roots_from_tx_hash(
                    tx_hash=str(tx_hash)
                )
                if ok and isinstance(roots, list):
                    payload["withdrawal_roots"] = roots
            return True, payload
        except Exception as exc:
            return False, str(exc)

    def _withdrawal_roots_from_receipt(self, *, web3: Any, receipt: Any) -> list[str]:
        logs = (receipt or {}).get("logs") or []
        roots: list[str] = []

        for log in logs if isinstance(logs, list) else []:
            try:
                if str(log.get("address") or "").lower() != str(
                    self.delegation_manager
                ).lower():
                    continue
                topics = log.get("topics") or []
                if not topics:
                    continue
                if HexBytes(topics[0]) != _SLASHING_WITHDRAWAL_QUEUED_TOPIC0:
                    continue

                evt = get_event_data(
                    web3.codec,
                    _SLASHING_WITHDRAWAL_QUEUED_EVENT_ABI,
                    log,
                )
                root = (evt.get("args") or {}).get("withdrawalRoot")
                if root is None:
                    continue
                roots.append(_as_bytes32_hex(root))
            except Exception:
                continue

        # Dedupe while preserving order.
        return list(dict.fromkeys(roots))

    async def get_withdrawal_roots_from_tx_hash(
        self,
        *,
        tx_hash: str,
    ) -> tuple[bool, list[str] | str]:
        h = str(tx_hash or "").strip()
        if not h:
            return False, "tx_hash is required"
        if not h.startswith("0x"):
            h = f"0x{h}"

        try:
            async with web3_from_chain_id(self.chain_id) as web3:
                receipt = await web3.eth.get_transaction_receipt(h)
                roots = self._withdrawal_roots_from_receipt(web3=web3, receipt=receipt)
                return True, roots
        except Exception as exc:
            return False, str(exc)

    async def get_queued_withdrawal(
        self,
        *,
        withdrawal_root: str | bytes,
        block_identifier: int | str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        root_hex = _as_bytes32_hex(withdrawal_root)
        block_id = block_identifier if block_identifier is not None else "pending"
        try:
            async with web3_from_chain_id(self.chain_id) as web3:
                dm = web3.eth.contract(
                    address=self.delegation_manager, abi=IDELEGATION_MANAGER_ABI
                )
                withdrawal, shares = await dm.functions.getQueuedWithdrawal(
                    root_hex
                ).call(block_identifier=block_id)

                # withdrawal = (staker, delegatedTo, withdrawer, nonce, startBlock, strategies, scaledShares)
                strategies = [to_checksum_address(s) for s in (withdrawal[5] or [])]
                scaled_shares = list(withdrawal[6] or [])

                return True, {
                    "withdrawal_root": root_hex,
                    "withdrawal": {
                        "staker": to_checksum_address(withdrawal[0]),
                        "delegatedTo": to_checksum_address(withdrawal[1]),
                        "withdrawer": to_checksum_address(withdrawal[2]),
                        "nonce": withdrawal[3],
                        "startBlock": withdrawal[4],
                        "strategies": strategies,
                        "scaledShares": scaled_shares,
                    },
                    "shares": list(shares or []),
                }
        except Exception as exc:
            return False, str(exc)

    async def complete_withdrawal(
        self,
        *,
        withdrawal_root: str | bytes,
        receive_as_tokens: bool = True,
        tokens_override: list[str] | None = None,
        block_identifier: int | str | None = None,
    ) -> tuple[bool, Any]:
        wallet = self.wallet_address
        if not wallet:
            return False, "wallet_address is required"
        if not self.sign_callback:
            return False, "sign_callback is required"

        root_hex = _as_bytes32_hex(withdrawal_root)
        block_id = block_identifier if block_identifier is not None else "pending"

        try:
            async with web3_from_chain_id(self.chain_id) as web3:
                dm = web3.eth.contract(
                    address=self.delegation_manager, abi=IDELEGATION_MANAGER_ABI
                )
                withdrawal, shares = await dm.functions.getQueuedWithdrawal(
                    root_hex
                ).call(block_identifier=block_id)

                strategies = [to_checksum_address(s) for s in (withdrawal[5] or [])]
                scaled_shares = list(withdrawal[6] or [])

                if tokens_override is not None:
                    if len(tokens_override) != len(strategies):
                        return (
                            False,
                            "tokens_override length must equal withdrawal strategies length",
                        )
                    tokens = [to_checksum_address(t) for t in tokens_override]
                else:
                    tokens = []
                    for strat in strategies:
                        if (
                            strat.lower()
                            == EIGENCLOUD_BEACON_CHAIN_ETH_STRATEGY_SENTINEL.lower()
                        ):
                            tokens.append(ZERO_ADDRESS)
                            continue
                        if strat.lower() == EIGENCLOUD_EIGEN_STRATEGY.lower():
                            tokens.append(EIGEN_TOKEN)
                            continue
                        s = web3.eth.contract(address=strat, abi=ISTRATEGY_ABI)
                        underlying = await s.functions.underlyingToken().call(
                            block_identifier=block_id
                        )
                        tokens.append(to_checksum_address(str(underlying)))

                # Even when receive_as_tokens=False, tokens length must match strategies.
                if len(tokens) != len(strategies):
                    return False, "tokens length must equal withdrawal strategies length"

                withdrawal_tuple = (
                    to_checksum_address(withdrawal[0]),
                    to_checksum_address(withdrawal[1]),
                    to_checksum_address(withdrawal[2]),
                    withdrawal[3],
                    withdrawal[4],
                    strategies,
                    scaled_shares,
                )

                tx = await encode_call(
                    target=self.delegation_manager,
                    abi=IDELEGATION_MANAGER_ABI,
                    fn_name="completeQueuedWithdrawal",
                    args=[withdrawal_tuple, tokens, receive_as_tokens],
                    from_address=wallet,
                    chain_id=self.chain_id,
                )
                tx_hash = await send_transaction(tx, self.sign_callback)
                return True, {
                    "tx_hash": tx_hash,
                    "withdrawal_root": root_hex,
                    "receive_as_tokens": receive_as_tokens,
                    "tokens": tokens,
                    "shares": list(shares or []),
                }
        except Exception as exc:
            return False, str(exc)

    # ---------------------------
    # Rewards (merkle claims)
    # ---------------------------

    async def get_rewards_metadata(
        self,
        *,
        account: str | None = None,
        block_identifier: int | str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        acct = to_checksum_address(account) if account else self.wallet_address
        if not acct:
            return False, "account (or wallet_address) is required"

        block_id = block_identifier if block_identifier is not None else "pending"
        try:
            async with web3_from_chain_id(self.chain_id) as web3:
                rc = web3.eth.contract(
                    address=self.rewards_coordinator, abi=IREWARDS_COORDINATOR_ABI
                )
                claimer_coro = rc.functions.claimerFor(acct).call(
                    block_identifier=block_id
                )
                roots_len_coro = rc.functions.getDistributionRootsLength().call(
                    block_identifier=block_id
                )
                current_root_coro = rc.functions.getCurrentClaimableDistributionRoot().call(
                    block_identifier=block_id
                )
                claimer, roots_len, current_root = await asyncio.gather(
                    claimer_coro, roots_len_coro, current_root_coro
                )

                root_tuple = current_root or ("0x" + ("00" * 32), 0, 0, False)
                return True, {
                    "earner": acct,
                    "claimerFor": to_checksum_address(claimer),
                    "distributionRootsLength": roots_len or 0,
                    "currentRoot": {
                        "root": _as_bytes32_hex(root_tuple[0]),
                        "rewardsCalculationEndTimestamp": root_tuple[1] or 0,
                        "activatedAt": root_tuple[2] or 0,
                        "disabled": root_tuple[3],
                    },
                }
        except Exception as exc:
            return False, str(exc)

    async def set_rewards_claimer(
        self,
        *,
        claimer: str,
    ) -> tuple[bool, Any]:
        wallet = self.wallet_address
        if not wallet:
            return False, "wallet_address is required"
        if not self.sign_callback:
            return False, "sign_callback is required"

        c = to_checksum_address(claimer)
        try:
            tx = await encode_call(
                target=self.rewards_coordinator,
                abi=IREWARDS_COORDINATOR_ABI,
                fn_name="setClaimerFor",
                args=[c],
                from_address=wallet,
                chain_id=self.chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    async def check_claim(
        self,
        *,
        claim: Any,
        block_identifier: int | str | None = None,
    ) -> tuple[bool, bool | str]:
        block_id = block_identifier if block_identifier is not None else "pending"
        try:
            async with web3_from_chain_id(self.chain_id) as web3:
                rc = web3.eth.contract(
                    address=self.rewards_coordinator, abi=IREWARDS_COORDINATOR_ABI
                )
                ok = await rc.functions.checkClaim(claim).call(
                    block_identifier=block_id
                )
                return True, ok
        except Exception as exc:
            return False, str(exc)

    async def claim_rewards(
        self,
        *,
        claim: Any,
        recipient: str,
    ) -> tuple[bool, Any]:
        wallet = self.wallet_address
        if not wallet:
            return False, "wallet_address is required"
        if not self.sign_callback:
            return False, "sign_callback is required"

        rcpt = to_checksum_address(recipient)
        try:
            tx = await encode_call(
                target=self.rewards_coordinator,
                abi=IREWARDS_COORDINATOR_ABI,
                fn_name="processClaim",
                args=[claim, rcpt],
                from_address=wallet,
                chain_id=self.chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    async def claim_rewards_batch(
        self,
        *,
        claims: list[Any],
        recipient: str,
    ) -> tuple[bool, Any]:
        wallet = self.wallet_address
        if not wallet:
            return False, "wallet_address is required"
        if not self.sign_callback:
            return False, "sign_callback is required"

        if not claims:
            return False, "claims is required"

        rcpt = to_checksum_address(recipient)
        try:
            tx = await encode_call(
                target=self.rewards_coordinator,
                abi=IREWARDS_COORDINATOR_ABI,
                fn_name="processClaims",
                args=[claims, rcpt],
                from_address=wallet,
                chain_id=self.chain_id,
            )
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    async def claim_rewards_calldata(
        self,
        *,
        calldata: str | bytes,
        value: int = 0,
    ) -> tuple[bool, Any]:
        """Raw-calldata fallback for rewards claiming (e.g., from EigenLayer CLI/app)."""
        wallet = self.wallet_address
        if not wallet:
            return False, "wallet_address is required"
        if not self.sign_callback:
            return False, "sign_callback is required"

        try:
            raw_data = (
                _as_bytes(calldata) if isinstance(calldata, str) else bytes(calldata)
            )
            data = HexBytes(raw_data)
        except Exception as exc:
            return False, f"invalid calldata: {exc}"
        if not data:
            return False, "calldata is required"

        try:
            tx: dict[str, Any] = {
                "chainId": int(self.chain_id),
                "from": wallet,
                "to": self.rewards_coordinator,
                "data": "0x" + data.hex(),
                "value": value,
            }
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, tx_hash
        except Exception as exc:
            return False, str(exc)

    # ---------------------------
    # Positions
    # ---------------------------

    async def get_pos(
        self,
        *,
        account: str | None = None,
        include_usd: bool = False,
        block_identifier: int | str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        acct = to_checksum_address(account) if account else self.wallet_address
        if not acct:
            return False, "account (or wallet_address) is required"

        block_id = block_identifier if block_identifier is not None else "pending"
        try:
            async with web3_from_chain_id(self.chain_id) as web3:
                dm = web3.eth.contract(
                    address=self.delegation_manager, abi=IDELEGATION_MANAGER_ABI
                )

                strategies, deposit_shares = await dm.functions.getDepositedShares(
                    acct
                ).call(block_identifier=block_id)
                strategies = [to_checksum_address(s) for s in (strategies or [])]
                deposit_shares = list(deposit_shares or [])

                withdrawable_shares: list[int] = []
                if strategies:
                    withdrawable_raw, _deposit_raw = (
                        await dm.functions.getWithdrawableShares(acct, strategies).call(
                            block_identifier=block_id
                        )
                    )
                    withdrawable_shares = list(withdrawable_raw or [])

                is_delegated, delegated_to = await asyncio.gather(
                    dm.functions.isDelegated(acct).call(block_identifier=block_id),
                    dm.functions.delegatedTo(acct).call(block_identifier=block_id),
                )

                positions: list[dict[str, Any]] = []
                usd_value_total: float | None = 0.0 if include_usd else None

                for i, strat in enumerate(strategies):
                    dep = deposit_shares[i] if i < len(deposit_shares) else 0
                    wdr = withdrawable_shares[i] if i < len(withdrawable_shares) else 0
                    if dep <= 0 and wdr <= 0:
                        continue

                    underlying_addr: str | None = None
                    deposit_underlying = 0
                    withdrawable_underlying = 0

                    if (
                        strat.lower()
                        == EIGENCLOUD_BEACON_CHAIN_ETH_STRATEGY_SENTINEL.lower()
                    ):
                        underlying_addr = None
                    else:
                        s = web3.eth.contract(address=strat, abi=ISTRATEGY_ABI)
                        if strat.lower() == EIGENCLOUD_EIGEN_STRATEGY.lower():
                            underlying_addr = EIGEN_TOKEN
                        else:
                            try:
                                underlying_addr = to_checksum_address(
                                    str(
                                        await s.functions.underlyingToken().call(
                                            block_identifier=block_id
                                        )
                                    )
                                )
                            except Exception:
                                underlying_addr = None

                        if dep > 0:
                            try:
                                deposit_underlying = await s.functions.sharesToUnderlyingView(
                                    dep
                                ).call(block_identifier=block_id)
                            except Exception:
                                deposit_underlying = 0
                        if wdr > 0:
                            try:
                                withdrawable_underlying = (
                                    await s.functions.sharesToUnderlyingView(wdr).call(
                                        block_identifier=block_id
                                    )
                                )
                            except Exception:
                                withdrawable_underlying = 0

                    pos: dict[str, Any] = {
                        "strategy": strat,
                        "deposit_shares": dep,
                        "withdrawable_shares": wdr,
                        "underlying": underlying_addr,
                        "deposit_underlying_estimate": deposit_underlying,
                        "withdrawable_underlying_estimate": withdrawable_underlying,
                    }

                    if include_usd and underlying_addr:
                        usd = await self._usd_value(
                            token_address=underlying_addr,
                            amount_raw=withdrawable_underlying,
                        )
                        pos["usd_value"] = usd
                        if usd_value_total is not None and usd is not None:
                            usd_value_total += usd

                    positions.append(pos)

                out: dict[str, Any] = {
                    "chain_id": int(self.chain_id),
                    "account": acct,
                    "isDelegated": is_delegated,
                    "delegatedTo": to_checksum_address(delegated_to),
                    "positions": positions,
                }
                if include_usd:
                    out["usd_value"] = usd_value_total
                return True, out
        except Exception as exc:
            return False, str(exc)

    async def get_full_user_state(
        self,
        *,
        account: str,
        include_usd: bool = False,
        include_queued_withdrawals: bool = True,
        withdrawal_roots: list[str] | None = None,
        include_rewards_metadata: bool = True,
        block_identifier: int | str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        acct = to_checksum_address(account)
        block_id = block_identifier if block_identifier is not None else "pending"

        ok_pos, pos = await self.get_pos(
            account=acct,
            include_usd=include_usd,
            block_identifier=block_id,
        )
        if not ok_pos:
            return False, str(pos)

        ok_del, delegation = await self.get_delegation_state(
            account=acct, block_identifier=block_id
        )
        if not ok_del:
            return False, str(delegation)

        state: dict[str, Any] = {
            "protocol": "eigencloud",
            "chainId": int(self.chain_id),
            "account": acct,
            "delegation": delegation,
            "positions": (pos or {}).get("positions") if isinstance(pos, dict) else [],
        }
        if include_usd and isinstance(pos, dict):
            state["usd_value"] = pos.get("usd_value")

        if include_queued_withdrawals:
            roots = withdrawal_roots or []
            queued: list[dict[str, Any]] = []
            for r in roots:
                ok_q, q = await self.get_queued_withdrawal(
                    withdrawal_root=r, block_identifier=block_id
                )
                if ok_q and isinstance(q, dict):
                    queued.append(q)
            state["queued_withdrawals"] = queued

        if include_rewards_metadata:
            ok_rm, rm = await self.get_rewards_metadata(
                account=acct, block_identifier=block_id
            )
            if ok_rm and isinstance(rm, dict):
                state["rewards"] = rm

        return True, state

    async def _usd_value(self, *, token_address: str, amount_raw: int) -> float | None:
        try:
            data = await TOKEN_CLIENT.get_token_details(
                token_address, market_data=True, chain_id=self.chain_id
            )
            price = (
                data.get("price_usd") or data.get("price") or data.get("current_price")
            )
            if not price:
                return None
            decimals = int(data.get("decimals", 18))
            return (amount_raw / (10**decimals)) * float(price)
        except Exception:
            return None
