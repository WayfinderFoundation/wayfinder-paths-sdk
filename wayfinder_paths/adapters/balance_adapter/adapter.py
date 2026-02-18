import asyncio
from typing import Any

from wayfinder_paths.adapters.ledger_adapter.adapter import LedgerAdapter
from wayfinder_paths.adapters.multicall_adapter.adapter import MulticallAdapter
from wayfinder_paths.adapters.token_adapter.adapter import TokenAdapter
from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
from wayfinder_paths.core.utils.evm_helpers import resolve_chain_id
from wayfinder_paths.core.utils.token_resolver import TokenResolver
from wayfinder_paths.core.utils.tokens import (
    build_send_transaction,
    get_token_balance,
    get_token_balance_with_decimals,
    is_native_token,
)
from wayfinder_paths.core.utils.transaction import send_transaction
from wayfinder_paths.core.utils.units import from_erc20_raw
from wayfinder_paths.core.utils.web3 import web3_from_chain_id


class BalanceAdapter(BaseAdapter):
    adapter_type = "BALANCE"

    def __init__(
        self,
        config: dict[str, Any],
        main_wallet_signing_callback=None,
        signing_callback=None,
    ):
        super().__init__("balance", config)
        self.main_wallet_signing_callback = main_wallet_signing_callback
        self.signing_callback = signing_callback
        self.token_adapter = TokenAdapter()
        self.ledger_adapter = LedgerAdapter()

    async def get_balance(
        self,
        *,
        wallet_address: str,
        token_id: str | None = None,
        token_address: str | None = None,
        chain_id: int | None = None,
    ) -> tuple[bool, int | str]:
        try:
            if token_address is None:
                if not token_id:
                    raise ValueError("token_id or token_address is required")
                chain_id, token_address = await TokenResolver.resolve_token(
                    token_id, chain_id=chain_id
                )
            if chain_id is None:
                raise ValueError("chain_id is required")
            balance = await get_token_balance(
                token_address,
                int(chain_id),
                wallet_address,
            )
            return True, balance
        except Exception as e:
            return False, str(e)

    async def get_balance_details(
        self,
        *,
        wallet_address: str,
        token_id: str | None = None,
        token_address: str | None = None,
        chain_id: int | None = None,
        default_native_decimals: int = 18,
        balance_block_identifier: str | int = "pending",
    ) -> tuple[bool, dict[str, Any] | str]:
        """Return on-chain balance with decimals (RPC-first; API only for token_id lookup)."""
        try:
            if token_address is None:
                if not token_id:
                    raise ValueError("token_id or token_address is required")
                chain_id, token_address = await TokenResolver.resolve_token(
                    token_id, chain_id=chain_id
                )
            if chain_id is None:
                raise ValueError("chain_id is required")

            balance_raw, decimals = await get_token_balance_with_decimals(
                token_address,
                int(chain_id),
                wallet_address,
                balance_block_identifier=balance_block_identifier,
                default_native_decimals=int(default_native_decimals),
            )

            is_native = is_native_token(token_address)
            token_address_out = None if is_native else token_address

            balance_decimal = (
                from_erc20_raw(balance_raw, int(decimals))
                if int(decimals) >= 0
                else None
            )
            return True, {
                "success": True,
                "token_id": token_id,
                "token_address": token_address_out,
                "chain_id": int(chain_id),
                "wallet_address": str(wallet_address),
                "balance_raw": int(balance_raw),
                "decimals": int(decimals),
                "balance_decimal": float(balance_decimal)
                if balance_decimal is not None
                else None,
            }
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_vault_wallet_balance(self, token_id: str) -> tuple[bool, int | str]:
        addr = self._wallet_address(self.config.get("strategy_wallet", {}))
        if not addr:
            return False, "No strategy_wallet configured"
        return await self.get_balance(wallet_address=addr, token_id=token_id)

    async def move_from_main_wallet_to_strategy_wallet(
        self,
        token_id: str,
        amount: float,
        strategy_name: str = "unknown",
        skip_ledger: bool = False,
    ) -> tuple[bool, str]:
        return await self._move_between_wallets(
            token_id=token_id,
            amount=amount,
            from_wallet=self.config["main_wallet"],
            to_wallet=self.config["strategy_wallet"],
            ledger_method=self.ledger_adapter.record_deposit
            if not skip_ledger
            else None,
            ledger_wallet="to",
            strategy_name=strategy_name,
        )

    async def move_from_strategy_wallet_to_main_wallet(
        self,
        token_id: str,
        amount: float,
        strategy_name: str = "unknown",
        skip_ledger: bool = False,
    ) -> tuple[bool, str]:
        return await self._move_between_wallets(
            token_id=token_id,
            amount=amount,
            from_wallet=self.config["strategy_wallet"],
            to_wallet=self.config["main_wallet"],
            ledger_method=self.ledger_adapter.record_withdrawal
            if not skip_ledger
            else None,
            ledger_wallet="from",
            strategy_name=strategy_name,
        )

    async def send_to_address(
        self,
        token_id: str,
        amount: int,
        from_wallet: dict[str, Any],
        to_address: str,
        signing_callback,
    ) -> tuple[bool, str]:
        token_info = await TOKEN_CLIENT.get_token_details(token_id)
        chain_id = resolve_chain_id(token_info)
        if chain_id is None:
            return False, "Could not resolve chain_id from token"
        tx = await build_send_transaction(
            from_address=from_wallet["address"],
            to_address=to_address,
            token_address=token_info["address"],
            chain_id=chain_id,
            amount=amount,
        )
        tx_hash = await send_transaction(tx, signing_callback)
        return True, tx_hash

    async def _move_between_wallets(
        self,
        *,
        token_id: str,
        amount: float,
        from_wallet: dict[str, Any],
        to_wallet: dict[str, Any],
        ledger_method,
        ledger_wallet: str,
        strategy_name: str,
    ) -> tuple[bool, str]:
        token_info = await TOKEN_CLIENT.get_token_details(token_id)
        chain_id = resolve_chain_id(token_info)
        if chain_id is None:
            return False, "Could not resolve chain_id from token"
        decimals = token_info.get("decimals", 18)
        raw_amount = int(amount * (10**decimals))

        transaction = await build_send_transaction(
            from_address=from_wallet["address"],
            to_address=to_wallet["address"],
            token_address=token_info["address"],
            chain_id=chain_id,
            amount=raw_amount,
        )

        main_address = self.config.get("main_wallet", {}).get("address", "").lower()
        callback = (
            self.main_wallet_signing_callback
            if from_wallet["address"].lower() == main_address
            else self.signing_callback
        )
        tx_hash = await send_transaction(transaction, callback)

        if ledger_method:
            wallet_for_ledger = (
                from_wallet["address"]
                if ledger_wallet == "from"
                else to_wallet["address"]
            )
            await self._record_ledger_entry(
                ledger_method, wallet_for_ledger, token_info, amount, strategy_name
            )

        return True, tx_hash

    async def _record_ledger_entry(
        self,
        ledger_method,
        wallet_address: str,
        token_info: dict[str, Any],
        amount: float,
        strategy_name: str,
    ) -> None:
        try:
            chain_id = resolve_chain_id(token_info)
            token_id = token_info.get("token_id")
            usd_value = (
                await self.token_adapter.get_amount_usd(
                    token_info.get("token_id"), amount, decimals=0
                )
                or 0.0
            )
            await ledger_method(
                wallet_address=wallet_address,
                chain_id=chain_id,
                token_address=token_info.get("address"),
                token_amount=str(amount),
                usd_value=usd_value,
                data={
                    "token_id": token_id,
                    "amount": str(amount),
                    "usd_value": usd_value,
                },
                strategy_name=strategy_name,
            )
        except Exception as exc:
            self.logger.warning(f"Ledger entry failed: {exc}", wallet=wallet_address)

    @staticmethod
    def _wallet_address(wallet: dict[str, Any] | None) -> str | None:
        if wallet and isinstance(wallet, dict):
            return wallet.get("address")
        return None

    async def get_wallet_balances_multicall(
        self,
        *,
        assets: list[dict[str, Any]],
        wallet_address: str | None = None,
        default_native_decimals: int = 18,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        if not assets:
            return True, []

        base_wallet = wallet_address
        if base_wallet is None:
            strategy_wallet = self.config.get("strategy_wallet", {})
            base_wallet = self._wallet_address(strategy_wallet)

        results: list[dict[str, Any]] = [{"success": False} for _ in assets]
        all_success = True

        normalized: list[dict[str, Any]] = []
        for idx, asset in enumerate(assets):
            token_id = asset.get("token_id")
            token_address = asset.get("token_address")
            chain_id = asset.get("chain_id")
            req_wallet = asset.get("wallet_address") or base_wallet

            if not req_wallet:
                results[idx] = {
                    "success": False,
                    "error": "wallet_address not provided and no strategy_wallet configured",
                    "token_id": token_id,
                    "token_address": token_address,
                    "chain_id": chain_id,
                }
                all_success = False
                continue

            if token_id and (token_address is None or chain_id is None):
                try:
                    (
                        resolved_chain_id,
                        resolved_address,
                    ) = await TokenResolver.resolve_token(
                        str(token_id), chain_id=chain_id
                    )
                    chain_id = (
                        int(chain_id) if chain_id is not None else resolved_chain_id
                    )
                    token_address = token_address or resolved_address
                except Exception as exc:  # noqa: BLE001
                    results[idx] = {
                        "success": False,
                        "error": str(exc),
                        "token_id": token_id,
                        "token_address": token_address,
                        "chain_id": chain_id,
                        "wallet_address": req_wallet,
                    }
                    all_success = False
                    continue

            if chain_id is None:
                results[idx] = {
                    "success": False,
                    "error": "chain_id is required",
                    "token_id": token_id,
                    "token_address": token_address,
                    "chain_id": chain_id,
                    "wallet_address": req_wallet,
                }
                all_success = False
                continue

            token_addr_str = (
                str(token_address).strip() if token_address is not None else None
            )
            is_native = is_native_token(token_addr_str)

            normalized.append(
                {
                    "index": idx,
                    "token_id": token_id,
                    "token_address": token_addr_str,
                    "chain_id": int(chain_id),
                    "wallet_address": str(req_wallet),
                    "is_native": bool(is_native),
                }
            )

        by_chain: dict[int, list[dict[str, Any]]] = {}
        for entry in normalized:
            by_chain.setdefault(entry["chain_id"], []).append(entry)

        async def _process_chain(chain_id: int, entries: list[dict[str, Any]]) -> None:
            nonlocal all_success
            try:
                async with web3_from_chain_id(chain_id) as w3:
                    multicall = MulticallAdapter(web3=w3, chain_id=chain_id)

                    token_set: set[str] = {
                        w3.to_checksum_address(e["token_address"])
                        for e in entries
                        if not e["is_native"] and e["token_address"]
                    }
                    sorted_tokens = sorted(token_set)

                    calls: list[Any] = []
                    decimals_call_index: dict[str, int] = {}
                    for token in sorted_tokens:
                        decimals_call_index[token] = len(calls)
                        erc20 = w3.eth.contract(address=token, abi=ERC20_ABI)
                        calls.append(
                            multicall.build_call(token, erc20.encode_abi("decimals"))
                        )

                    balance_call_index: dict[int, int] = {}
                    for entry in entries:
                        if entry["is_native"]:
                            call = multicall.encode_eth_balance(entry["wallet_address"])
                        else:
                            token = w3.to_checksum_address(entry["token_address"])
                            call = multicall.encode_erc20_balance(
                                token, entry["wallet_address"]
                            )
                        balance_call_index[entry["index"]] = len(calls)
                        calls.append(call)

                    mc_res = await multicall.aggregate(calls)

                    decimals_by_token: dict[str, int] = {}
                    for token, call_idx in decimals_call_index.items():
                        raw_decimals = multicall.decode_uint256(
                            mc_res.return_data[call_idx]
                        )
                        decimals_by_token[token] = int(raw_decimals)

                    for entry in entries:
                        out_idx = entry["index"]
                        bal_idx = balance_call_index[out_idx]
                        raw_balance = multicall.decode_uint256(
                            mc_res.return_data[bal_idx]
                        )
                        if entry["is_native"]:
                            decimals = int(default_native_decimals)
                            token_address_out = None
                        else:
                            token = w3.to_checksum_address(entry["token_address"])
                            decimals = int(
                                decimals_by_token.get(token, default_native_decimals)
                            )
                            token_address_out = token

                        balance_decimal = (
                            from_erc20_raw(raw_balance, decimals)
                            if decimals >= 0
                            else None
                        )

                        results[out_idx] = {
                            "success": True,
                            "token_id": entry.get("token_id"),
                            "token_address": token_address_out,
                            "chain_id": chain_id,
                            "wallet_address": entry["wallet_address"],
                            "balance_raw": int(raw_balance),
                            "decimals": int(decimals),
                            "balance_decimal": float(balance_decimal)
                            if balance_decimal is not None
                            else None,
                            "block_number": mc_res.block_number,
                        }

            except Exception as exc:  # noqa: BLE001
                all_success = False
                err = str(exc)
                for entry in entries:
                    out_idx = entry["index"]
                    results[out_idx] = {
                        "success": False,
                        "error": err,
                        "token_id": entry.get("token_id"),
                        "token_address": entry.get("token_address"),
                        "chain_id": chain_id,
                        "wallet_address": entry.get("wallet_address"),
                    }

        await asyncio.gather(
            *[
                _process_chain(chain_id, entries)
                for chain_id, entries in by_chain.items()
            ]
        )

        for idx, out in enumerate(results):
            if out.get("success") is True:
                continue
            if "error" not in out:
                all_success = False
                out.setdefault("error", "Unknown error")
                out.setdefault("token_id", assets[idx].get("token_id"))
                out.setdefault("token_address", assets[idx].get("token_address"))
                out.setdefault("chain_id", assets[idx].get("chain_id"))
                out.setdefault(
                    "wallet_address", assets[idx].get("wallet_address") or base_wallet
                )

        return all_success, results
