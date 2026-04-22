from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from eth_abi import decode, encode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address
from loguru import logger

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.constants.boros_abi import (
    BOROS_MARKET_HUB_VIEW_ABI,
    BOROS_MERKLE_DISTRIBUTOR_ABI,
    BOROS_ROUTER_VIEW_ABI,
    BOROS_VAULT_BALANCE_ABI,
)
from wayfinder_paths.core.constants.chains import CHAIN_ID_HYPEREVM
from wayfinder_paths.core.constants.contracts import (
    BOROS_MARKET_HUB,
    BOROS_MERKLE_DISTRIBUTOR,
    BOROS_ROUTER,
    HYPE_OFT_ADDRESS,
)
from wayfinder_paths.core.constants.hype_oft_abi import HYPE_OFT_ABI
from wayfinder_paths.core.utils.multicall import (
    Call,
    read_only_calls_multicall_or_gather,
)
from wayfinder_paths.core.utils.tokens import (
    build_approve_transaction,
    get_token_balance,
)
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.units import from_erc20_raw
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

from .client import BorosClient
from .parsers import (
    extract_collateral,
    extract_maturity_ts,
    extract_symbol,
    extract_underlying,
    parse_market_position,
    time_to_maturity_days,
)
from .types import (
    BorosLimitOrder,
    BorosMarketQuote,
    BorosTenorQuote,
    BorosVault,
)
from .utils import (
    BOROS_TICK_BASE,
    cash_wei_to_float,
    market_id_from_market_acc,
)
from .utils import (
    normalize_apr as _normalize_apr,
)
from .utils import (
    rate_from_tick as _rate_from_tick,
)
from .utils import (
    tick_from_rate as _tick_from_rate,
)


class BorosAdapter(BaseAdapter):
    adapter_type = "BOROS"
    SIDE_LONG = 0
    SIDE_SHORT = 1
    MAX_INT128 = (1 << 127) - 1
    MIN_INT128 = -(1 << 127)
    CROSS_MARGIN_MARKET_ID = 0xFFFFFF

    # LayerZero endpoint IDs for the HYPE OFT deployment.
    # These are needed to bridge between:
    # - HyperEVM (native HYPE) <-> Arbitrum (ERC20 OFT HYPE)
    #
    # Verified on 2026-02-02 by checking `peers(eid)` on-chain:
    # - On HyperEVM (chainId=999), `peers(30110)` points at the Arbitrum OFT contract.
    # - On Arbitrum (chainId=42161), `peers(30367)` points at the HyperEVM OFT contract.
    LZ_EID_ARBITRUM: int = 30110
    LZ_EID_HYPEREVM: int = 30367

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        sign_callback: Callable | None = None,
        wallet_address: str | None = None,
        account_id: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__("boros_adapter", config)
        self.sign_callback = sign_callback
        self._scaling_factor_cache: dict[int, int] = {}
        self._amm_address_cache: dict[int, str | None] = {}
        self._amm_id_by_market_cache: dict[int, int] = {}
        self._lp_balance_cache: dict[tuple[int, int, str], int] = {}
        self._assets_by_id_cache: dict[int, dict[str, Any]] | None = None
        self._vault_context_cache: dict[int, dict[str, Any]] = {}

        boros_cfg = (config or {}).get("boros_adapter", {})
        self.chain_id = int(boros_cfg.get("chain_id", 42161))

        self.wallet_address = (
            to_checksum_address(wallet_address) if wallet_address else None
        )
        self.account_id = boros_cfg.get("account_id", account_id)

        self.boros_client = BorosClient(
            base_url=boros_cfg.get("base_url", "https://api.boros.finance"),
            endpoints=boros_cfg.get("endpoints"),
            user_address=wallet_address,
            account_id=self.account_id,
        )

    @staticmethod
    def _pad_address_bytes32(address: str) -> bytes:
        checksum = to_checksum_address(address)
        return bytes.fromhex(checksum[2:]).rjust(32, b"\x00")

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    @staticmethod
    def _maybe_float(value: Any) -> float | None:
        try:
            if value is None or value == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _maybe_int(value: Any) -> int | None:
        try:
            if value is None or value == "":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _wei_amount_to_tokens(value: Any) -> float | None:
        try:
            if value is None or value == "":
                return None
            return float(value) / 1e18
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _tokens_to_usd(
        token_amount: float | None,
        price_usd: float | None,
    ) -> float | None:
        try:
            if token_amount is None or price_usd is None:
                return None
            return float(token_amount) * float(price_usd)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _maturity_ts_to_expiry(maturity_ts: int | None) -> str | None:
        try:
            if maturity_ts is None:
                return None
            return datetime.fromtimestamp(int(maturity_ts), UTC).date().isoformat()
        except (OSError, OverflowError, TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_account_param(account: str | None) -> str | None:
        if not account:
            return None
        account_param = str(account).lower()
        if not account_param.endswith("00"):
            account_param = f"{account_param}00"
        return account_param

    @staticmethod
    def _time_frame_seconds(time_frame: str) -> int | None:
        return {
            "5m": 5 * 60,
            "1h": 60 * 60,
            "1d": 24 * 60 * 60,
            "1w": 7 * 24 * 60 * 60,
        }.get(str(time_frame).lower())

    @staticmethod
    def _average_defined(values: list[float | None]) -> float | None:
        nums = [float(value) for value in values if value is not None]
        if not nums:
            return None
        return sum(nums) / len(nums)

    @staticmethod
    def _last_defined(values: list[float | None]) -> float | None:
        for value in reversed(values):
            if value is not None:
                return float(value)
        return None

    @staticmethod
    def _split_symbol(symbol: str) -> tuple[str | None, str | None]:
        sym = str(symbol or "").strip()
        if not sym:
            return None, None
        for sep in ("-", "/"):
            parts = [p.strip() for p in sym.split(sep) if p.strip()]
            if len(parts) >= 2:
                return parts[0], parts[1]
        return sym, None

    @staticmethod
    def build_market_acc(
        *,
        address: str,
        account_id: int,
        token_id: int,
        market_id: int,
    ) -> bytes:
        addr = bytes.fromhex(to_checksum_address(address)[2:])
        return (
            addr
            + int(account_id).to_bytes(1, "big")
            + int(token_id).to_bytes(2, "big")
            + int(market_id).to_bytes(3, "big")
        )

    async def get_scaling_factor(self, token_id: int) -> int:
        cached = self._scaling_factor_cache.get(int(token_id))
        if cached:
            return cached

        async with web3_from_chain_id(self.chain_id) as web3:
            contract = web3.eth.contract(
                address=to_checksum_address(BOROS_MARKET_HUB),
                abi=BOROS_MARKET_HUB_VIEW_ABI,
            )
            token_addr, scaling = await contract.functions.tokenData(
                int(token_id)
            ).call()

            scaling_i = int(scaling or 0)
            if scaling_i <= 0:
                raise ValueError(
                    f"Invalid Boros scaling factor for token_id={int(token_id)} "
                    f"(token={token_addr}, scaling={scaling})"
                )

            self._scaling_factor_cache[int(token_id)] = scaling_i
            return scaling_i

    async def scaled_cash_wei_to_unscaled(
        self, token_id: int, scaled_cash_wei: int
    ) -> int:
        scaling = await self.get_scaling_factor(int(token_id))
        return int(scaled_cash_wei) // int(scaling)

    async def unscaled_to_scaled_cash_wei(
        self, token_id: int, unscaled_amount: int
    ) -> int:
        scaling = await self.get_scaling_factor(int(token_id))
        return int(unscaled_amount) * int(scaling)

    async def get_user_withdrawal_status(
        self,
        *,
        token_id: int,
        user_address: str | None = None,
    ) -> tuple[bool, dict[str, int] | str]:
        try:
            user = to_checksum_address(user_address or self.wallet_address or "")
            async with web3_from_chain_id(self.chain_id) as web3:
                contract = web3.eth.contract(
                    address=to_checksum_address(BOROS_MARKET_HUB),
                    abi=BOROS_MARKET_HUB_VIEW_ABI,
                )
                start, unscaled = await contract.functions.getUserWithdrawalStatus(
                    user, int(token_id)
                ).call()
            return True, {"start": int(start), "unscaled": int(unscaled)}
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_cash_fee_data(self, *, token_id: int) -> tuple[bool, dict[str, Any]]:
        """Read MarketHub.getCashFeeData(tokenId) from chain.

        This is useful for guarding Boros actions that require a minimum amount
        of cross cash (e.g., MMInsufficientMinCash()).
        """
        try:
            selector = function_signature_to_4byte_selector("getCashFeeData(uint16)")
            params = encode(["uint16"], [int(token_id)])
            data = "0x" + selector.hex() + params.hex()

            async with web3_from_chain_id(self.chain_id) as web3:
                raw: bytes = await web3.eth.call(
                    {
                        "to": to_checksum_address(BOROS_MARKET_HUB),
                        "data": data,
                    }
                )

            if len(raw) % 32 != 0:
                return False, {"error": f"Unexpected getCashFeeData() size: {len(raw)}"}

            n_words = len(raw) // 32
            values = decode(["uint256"] * n_words, raw)
            if len(values) < 4:
                return False, {
                    "error": f"Unexpected getCashFeeData() words: {len(values)}"
                }

            # Empirically (2026-02-01 on Arbitrum MarketHub), the return is 4 uint256s.
            # We expose all 4, and provide float conversions for the commonly used ones.
            scaling_factor_wei = int(values[0])
            fee_rate_wei = int(values[1])
            min_cash_cross_wei = int(values[2])
            min_cash_isolated_wei = int(values[3])

            return True, {
                "token_id": int(token_id),
                "scaling_factor_wei": scaling_factor_wei,
                "fee_rate_wei": fee_rate_wei,
                "min_cash_cross_wei": min_cash_cross_wei,
                "min_cash_isolated_wei": min_cash_isolated_wei,
                "fee_rate": fee_rate_wei / 1e18,
                "min_cash_cross": min_cash_cross_wei / 1e18,
                "min_cash_isolated": min_cash_isolated_wei / 1e18,
            }
        except Exception as exc:  # noqa: BLE001
            return False, {"error": str(exc)}

    async def sweep_isolated_to_cross(
        self,
        *,
        token_id: int,
        market_id: int | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Sweep isolated cash -> cross cash for a given token (optionally per-market).

        Boros deposits can sometimes show up as isolated cash for the target market;
        this helper moves that isolated cash back to cross margin using cash_transfer.

        Notes:
        - Uses `get_account_balances()` (collaterals endpoint) to find isolated cash.
        - cash_transfer uses 1e18 internal cash units, not token native decimals.
        - This does NOT touch isolated positions for other markets unless market_id is None.
        """
        ok_bal, balances = await self.get_account_balances(token_id=int(token_id))
        if not ok_bal or not isinstance(balances, dict):
            return False, {"error": f"Failed to read Boros balances: {balances}"}

        isolated_positions = balances.get("isolated_positions") or []
        if not isinstance(isolated_positions, list):
            isolated_positions = []

        moved: list[dict[str, Any]] = []
        for iso in isolated_positions:
            try:
                iso_market_id = int(iso.get("market_id"))
                if market_id is not None and iso_market_id != int(market_id):
                    continue
                balance_wei = int(iso.get("balance_wei") or 0)
                if balance_wei <= 0:
                    continue

                tx_ok, tx_res = await self.cash_transfer(
                    market_id=iso_market_id,
                    amount_wei=balance_wei,
                    is_deposit=False,  # isolated -> cross
                )
                moved.append(
                    {
                        "market_id": iso_market_id,
                        "balance_wei": balance_wei,
                        "ok": tx_ok,
                        "tx": tx_res,
                    }
                )
                if not tx_ok:
                    return False, {
                        "error": f"Failed sweep isolated->cross for market {iso_market_id}",
                        "moved": moved,
                    }
            except Exception as exc:  # noqa: BLE001
                return False, {"error": f"Failed sweep isolated->cross: {exc}"}

        return True, {"status": "ok", "moved": moved}

    @staticmethod
    def _unwrap_tx_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Best-effort unwrap of API payloads that may nest the tx dict."""
        tx_src: Any = payload
        for key in ("data", "calldata", "transaction", "tx", "result"):
            if isinstance(tx_src, dict) and isinstance(tx_src.get(key), dict):
                tx_src = tx_src[key]
        return tx_src if isinstance(tx_src, dict) else payload

    def _build_tx_from_calldata(
        self,
        calldata: dict[str, Any],
        *,
        from_address: str,
    ) -> dict[str, Any]:
        """Build a transaction dict from Boros API calldata.

        NOTE: We intentionally do NOT copy 'gas' from the API response.
        The Boros API sometimes returns incorrect gas values (e.g., 1234).
        Instead, we let the transaction service estimate gas properly.
        """
        tx_src = self._unwrap_tx_payload(calldata)

        to_addr = tx_src.get("to") or calldata.get("to")

        # Handle v3 API format that returns {'calldatas': ['0x...']} without 'to' address
        data_val = tx_src.get("data") or calldata.get("data")
        if not data_val:
            calldatas = calldata.get("calldatas") or tx_src.get("calldatas")
            if isinstance(calldatas, list) and len(calldatas) > 0:
                data_val = calldatas[0]
                # Use Router address when calldatas format is used (for calldata execution)
                if not to_addr:
                    to_addr = BOROS_ROUTER
                    logger.debug(
                        f"Using Boros Router address for calldatas format: {to_addr}"
                    )

        if not isinstance(to_addr, str) or not to_addr:
            raise ValueError("Boros calldata missing 'to' address")

        if not data_val:
            data_val = "0x"
        if not isinstance(data_val, str):
            raise ValueError("Boros calldata missing 'data' field")

        chain_id_val = (
            tx_src.get("chainId")
            or tx_src.get("chain_id")
            or calldata.get("chainId")
            or calldata.get("chain_id")
        )
        try:
            chain_id_int = (
                int(chain_id_val) if chain_id_val is not None else int(self.chain_id)
            )
        except (TypeError, ValueError):
            chain_id_int = int(self.chain_id)

        value_val = (
            tx_src.get("value") if "value" in tx_src else calldata.get("value", 0)
        )
        try:
            value_int = int(value_val) if value_val is not None else 0
        except (TypeError, ValueError):
            value_int = 0

        return {
            "chainId": int(chain_id_int),
            "from": to_checksum_address(from_address),
            "to": to_checksum_address(to_addr),
            "data": data_val if data_val.startswith("0x") else f"0x{data_val}",
            "value": int(value_int),
        }

    async def _broadcast_calldata(
        self,
        calldata: dict[str, Any],
        *,
        max_retries: int = 2,
    ) -> tuple[bool, dict[str, Any]]:
        """Broadcast calldata from Boros API with retry logic.

        Handles multiple formats:
        - {"calldatas": ["0x...", "0x..."]} - execute each sequentially to Router
        - {"data": "0x...", "to": "0x..."} - standard format

        Args:
            calldata: Transaction calldata from Boros API.
            timeout: Transaction timeout in seconds.
            max_retries: Number of retry attempts for failed transactions.
        """
        if not self.sign_callback:
            return False, {
                "error": "sign_callback not configured",
                "calldata": calldata,
            }
        if not self.wallet_address:
            return False, {"error": "user_address not configured", "calldata": calldata}

        # Check for calldatas array format (multiple transactions)
        calldatas = calldata.get("calldatas")
        if isinstance(calldatas, list) and len(calldatas) > 1:
            results = []
            for i, data in enumerate(calldatas):
                single_calldata = {"data": data, "to": BOROS_ROUTER}
                tx = self._build_tx_from_calldata(
                    single_calldata, from_address=self.wallet_address
                )
                logger.debug(
                    f"Broadcasting calldata {i + 1}/{len(calldatas)} to {tx.get('to')}"
                )
                try:
                    tx_hash = await send_transaction(
                        tx, self.sign_callback, wait_for_receipt=True
                    )
                    results.append({"ok": True, "res": {"tx_hash": tx_hash}})
                except Exception as e:
                    results.append({"ok": False, "res": {"error": str(e)}})
                    return False, {
                        "status": "error",
                        "error": f"Failed on calldata {i + 1}/{len(calldatas)}: {e}",
                        "tx": {"error": str(e)},
                        "calldata": calldata,
                        "partial_results": results,
                    }
            return True, {
                "status": "ok",
                "tx": results[-1]["res"],
                "calldata": calldata,
                "all_results": results,
            }

        # Single calldata (standard format) with retry logic
        last_error = None
        for attempt in range(max_retries + 1):
            tx = self._build_tx_from_calldata(
                calldata, from_address=self.wallet_address
            )
            try:
                tx_hash = await send_transaction(
                    tx, self.sign_callback, wait_for_receipt=True
                )
                return True, {
                    "status": "ok",
                    "tx": {"tx_hash": tx_hash},
                    "calldata": calldata,
                }
            except Exception as e:
                last_error = str(e)
                error_str = str(e).lower()
                # Check if it's a revert (not worth retrying) vs transient error
                if "revert" in error_str:
                    logger.warning(
                        f"Boros transaction reverted on attempt {attempt + 1}/{max_retries + 1}: {e}"
                    )
                    # For reverts, wait a bit and retry in case it's a timing issue
                    if attempt < max_retries:
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                else:
                    # Non-revert error, log and retry
                    logger.warning(
                        f"Boros transaction failed on attempt {attempt + 1}/{max_retries + 1}: {e}"
                    )
                    if attempt < max_retries:
                        await asyncio.sleep(1)

        return False, {
            "status": "error",
            "error": str(last_error),
            "tx": last_error,
            "calldata": calldata,
            "attempts": max_retries + 1,
        }

    # ------------------------------------------------------------------ #
    # Tick Math Utilities                                                  #
    # ------------------------------------------------------------------ #

    @classmethod
    def tick_from_rate(cls, rate: float, tick_step: int, *, round_down: bool) -> int:
        """Convert APR rate to Boros limitTick.

        Args:
            rate: APR as decimal (e.g., 0.11 = 11%).
            tick_step: Market's tickStep from metadata.
            round_down: If True, round toward zero (for shorts).

        Returns:
            limitTick value for Boros API.
        """
        return _tick_from_rate(
            rate,
            tick_step,
            round_down=round_down,
            base=BOROS_TICK_BASE,
        )

    @classmethod
    def rate_from_tick(cls, tick: int, tick_step: int) -> float:
        """Convert Boros limitTick to APR rate.

        Args:
            tick: Boros tick value.
            tick_step: Market's tickStep.

        Returns:
            APR as decimal (e.g., 0.11 = 11%).
        """
        return _rate_from_tick(tick, tick_step, base=BOROS_TICK_BASE)

    @staticmethod
    def normalize_apr(value: Any) -> float | None:
        """Normalize various APR encodings to decimal.

        Handles: decimal (0.1115), percent (11.15), bps (1115), 1e18-scaled.
        """
        return _normalize_apr(value)

    @staticmethod
    def _to_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ #
    # Market Data                                                          #
    # ------------------------------------------------------------------ #

    async def list_markets(
        self,
        *,
        is_whitelisted: bool | None = True,
        skip: int = 0,
        limit: int = 100,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        try:
            markets = await self.boros_client.list_markets(
                is_whitelisted=is_whitelisted, skip=skip, limit=limit
            )
            return True, markets
        except Exception as e:
            logger.error(f"Failed to list markets: {e}")
            return False, str(e)

    async def list_markets_all(
        self,
        *,
        is_whitelisted: bool | None = True,
        page_size: int = 100,
        max_pages: int | None = None,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        """List all markets, automatically paginating `skip/limit`.

        Boros enforces `limit <= 100`. This helper keeps requesting pages until:
        - an empty page is returned
        - a short page is returned (< page_size)
        - max_pages is reached (if provided)
        """
        try:
            try:
                page_size = int(page_size)
            except (TypeError, ValueError):
                page_size = 100
            if page_size <= 0:
                page_size = 100
            if page_size > 100:
                page_size = 100

            if max_pages is not None:
                try:
                    max_pages = int(max_pages)
                except (TypeError, ValueError):
                    max_pages = None
                if max_pages is not None and max_pages <= 0:
                    max_pages = None

            markets: list[dict[str, Any]] = []
            skip = 0
            pages = 0
            while True:
                batch = await self.boros_client.list_markets(
                    is_whitelisted=is_whitelisted,
                    skip=skip,
                    limit=page_size,
                )
                if not batch:
                    break
                markets.extend(batch)
                pages += 1
                if max_pages is not None and pages >= max_pages:
                    break
                if len(batch) < page_size:
                    break
                skip += page_size

            # De-dup by marketId while preserving first-seen order.
            seen: set[int] = set()
            unique: list[dict[str, Any]] = []
            for m in markets:
                try:
                    mid = int(m.get("marketId") or m.get("id") or 0)
                except (TypeError, ValueError):
                    mid = 0
                if mid and mid in seen:
                    continue
                if mid:
                    seen.add(mid)
                unique.append(m)

            return True, unique
        except Exception as e:
            logger.error(f"Failed to list all markets: {e}")
            return False, str(e)

    async def get_market(self, market_id: int) -> tuple[bool, dict[str, Any] | str]:
        try:
            market = await self.boros_client.get_market(market_id)
            return True, market
        except Exception as e:
            logger.error(f"Failed to get market {market_id}: {e}")
            return False, str(e)

    async def quote_market_by_id(
        self,
        market_id: int,
        *,
        tick_size: float = 0.001,
        prefer_market_data: bool = True,
    ) -> tuple[bool, BorosMarketQuote | str]:
        """Convenience helper: get_market() + quote_market()."""
        ok, market = await self.get_market(int(market_id))
        if not ok:
            return False, str(market)
        return await self.quote_market(
            market,
            tick_size=tick_size,
            prefer_market_data=prefer_market_data,
        )

    async def get_orderbook(
        self, market_id: int, *, tick_size: float = 0.001
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            book = await self.boros_client.get_order_book(
                market_id, tick_size=tick_size
            )
            return True, book
        except Exception as e:
            logger.error(f"Failed to get orderbook for market {market_id}: {e}")
            return False, str(e)

    async def quote_market(
        self,
        market: dict[str, Any],
        *,
        tick_size: float = 0.001,
        prefer_market_data: bool = True,
    ) -> tuple[bool, BorosMarketQuote | str]:
        try:
            market_id = int(market.get("marketId") or market.get("id") or 0)
            market_address = market.get("address") or market.get("marketAddress") or ""
            if not market_id:
                raise ValueError("Market missing marketId/id")

            # Prefer the /markets endpoint embedded snapshot (fast, avoids /order-books call).
            data = market.get("data") or {}
            if not isinstance(data, dict):
                data = {}

            data_bid_apr = self.normalize_apr(data.get("bestBid"))
            data_ask_apr = self.normalize_apr(data.get("bestAsk"))
            data_mid_apr = self.normalize_apr(data.get("midApr"))

            bid_apr = data_bid_apr
            ask_apr = data_ask_apr
            mid_apr = data_mid_apr

            # Only skip the orderbook call if we have both sides.
            if not (
                prefer_market_data
                and data_bid_apr is not None
                and data_ask_apr is not None
            ):
                orderbook = await self.boros_client.get_order_book(
                    market_id, tick_size=tick_size
                )

                long_side = orderbook.get("long") or {}
                short_side = orderbook.get("short") or {}

                long_ticks = long_side.get("ia") or []
                short_ticks = short_side.get("ia") or []

                bid_apr = None
                ask_apr = None

                # Best bid = highest rate long side is willing to pay
                if long_ticks:
                    best_bid_tick = max(long_ticks)
                    bid_apr = float(best_bid_tick) * tick_size

                # Best ask = lowest rate short side willing to receive
                if short_ticks:
                    best_ask_tick = min(short_ticks)
                    ask_apr = float(best_ask_tick) * tick_size

                if bid_apr is not None and ask_apr is not None:
                    mid_apr = (bid_apr + ask_apr) / 2.0
                else:
                    mid_apr = bid_apr if bid_apr is not None else ask_apr
                if mid_apr is None:
                    mid_apr = data_mid_apr

            if mid_apr is None and bid_apr is not None and ask_apr is not None:
                mid_apr = (bid_apr + ask_apr) / 2.0

            maturity_ts = self._extract_maturity_ts(market)
            tenor_days = (
                self._time_to_maturity_days(maturity_ts) if maturity_ts else 0.0
            )

            quote = BorosMarketQuote(
                market_id=market_id,
                market_address=market_address,
                symbol=self._extract_symbol(market),
                underlying=self._extract_underlying(market),
                tenor_days=tenor_days,
                maturity_ts=maturity_ts or 0,
                collateral_address=self._extract_collateral(market),
                collateral_token_id=market.get("tokenId"),
                tick_step=(market.get("imData") or {}).get("tickStep"),
                mid_apr=mid_apr,
                best_bid_apr=bid_apr,
                best_ask_apr=ask_apr,
                mark_apr=self.normalize_apr(data.get("markApr")),
                floating_apr=self.normalize_apr(data.get("floatingApr")),
                long_yield_apr=self.normalize_apr(data.get("longYieldApr")),
                funding_7d_ma_apr=self.normalize_apr(data.get("b7dmafr")),
                funding_30d_ma_apr=self.normalize_apr(data.get("b30dmafr")),
                volume_24h=self._to_float(data.get("volume24h")),
                notional_oi=self._to_float(data.get("notionalOI")),
                asset_mark_price=self._to_float(data.get("assetMarkPrice")),
                next_settlement_time=self._to_int(data.get("nextSettlementTime")),
                last_traded_apr=self.normalize_apr(data.get("lastTradedApr")),
                amm_implied_apr=self.normalize_apr(data.get("ammImpliedApr")),
            )
            return True, quote
        except Exception as e:
            logger.error(f"Failed to quote market: {e}")
            return False, str(e)

    async def quote_markets_for_underlying(
        self,
        underlying_symbol: str,
        *,
        platform: str | None = None,
        max_markets: int = 50,
        page_size: int = 100,
        tick_size: float = 0.001,
        prefer_market_data: bool = True,
    ) -> tuple[bool, list[BorosMarketQuote] | str]:
        try:
            ok, markets = await self.list_markets_all(
                is_whitelisted=True, page_size=page_size
            )
            if not ok:
                return False, str(markets)
            target = underlying_symbol.upper()
            platform_filter = platform.upper() if platform else None

            def _matches(mkt: dict[str, Any]) -> bool:
                under = self._extract_underlying(mkt).upper()
                sym = self._extract_symbol(mkt).upper()
                under_match = target == under
                sym_parts = sym.replace("_", "-").split("-")
                sym_match = target in sym_parts
                if not under_match and not sym_match:
                    return False
                if platform_filter:
                    metadata = mkt.get("metadata") or {}
                    plat = mkt.get("platform") or {}
                    plat_name = (
                        metadata.get("platformName") or plat.get("name") or ""
                    ).upper()
                    if platform_filter not in plat_name and not sym.startswith(
                        platform_filter
                    ):
                        return False
                return True

            filtered = [m for m in markets if _matches(m)]
            if max_markets is not None and len(filtered) > int(max_markets):
                filtered = filtered[: int(max_markets)]
            quotes: list[BorosMarketQuote] = []

            async def _quote_one(market: dict[str, Any]) -> BorosMarketQuote | None:
                try:
                    success, quote = await self.quote_market(
                        market,
                        tick_size=tick_size,
                        prefer_market_data=prefer_market_data,
                    )
                    return quote if success else None
                except Exception as e:
                    market_id = market.get("marketId") or market.get("id")
                    logger.warning(f"quote_market failed for {market_id}: {e}")
                    return None

            results = await asyncio.gather(*[_quote_one(m) for m in filtered])
            quotes = [q for q in results if q is not None]

            quotes.sort(key=lambda q: q.maturity_ts)
            return True, quotes
        except Exception as e:
            logger.error(f"Failed to quote markets for {underlying_symbol}: {e}")
            return False, str(e)

    async def list_tenor_quotes(
        self,
        *,
        underlying_symbol: str | None = None,
        platform: str | None = None,
        is_whitelisted: bool | None = True,
        page_size: int = 100,
        max_pages: int | None = None,
    ) -> tuple[bool, list[BorosTenorQuote] | str]:
        """Fast market+rate snapshot using only the `/markets` endpoint (no orderbooks).

        Useful for quickly answering questions like:
        - "What Boros markets exist for HYPE?"
        - "What are the fixed (mark/mid) and floating APRs?"
        """
        ok, markets = await self.list_markets_all(
            is_whitelisted=is_whitelisted, page_size=page_size, max_pages=max_pages
        )
        if not ok:
            return False, str(markets)

        target = underlying_symbol.upper() if underlying_symbol else None
        platform_filter = platform.upper() if platform else None

        quotes: list[BorosTenorQuote] = []
        for m in markets:
            under = self._extract_underlying(m).upper()
            if target and target != under:
                continue

            if platform_filter:
                metadata = m.get("metadata") or {}
                plat = m.get("platform") or {}
                plat_name = (
                    metadata.get("platformName") or plat.get("name") or ""
                ).upper()
                if platform_filter not in plat_name:
                    continue

            mid_raw = m.get("marketId") or m.get("id") or 0
            market_id = self._to_int(mid_raw) or 0
            address = m.get("address") or m.get("marketAddress") or ""
            symbol = self._extract_symbol(m)
            maturity_ts = self._extract_maturity_ts(m) or 0
            tenor_days = (
                self._time_to_maturity_days(maturity_ts) if maturity_ts else 0.0
            )

            data = m.get("data") or {}
            if not isinstance(data, dict):
                data = {}

            quotes.append(
                BorosTenorQuote(
                    market_id=market_id,
                    address=address,
                    symbol=symbol,
                    underlying_symbol=self._extract_underlying(m),
                    maturity=maturity_ts,
                    tenor_days=tenor_days,
                    mid_apr=self.normalize_apr(data.get("midApr")),
                    mark_apr=self.normalize_apr(data.get("markApr")),
                    floating_apr=self.normalize_apr(data.get("floatingApr")),
                    long_yield_apr=self.normalize_apr(data.get("longYieldApr")),
                    volume_24h=self._to_float(data.get("volume24h")),
                    notional_oi=self._to_float(data.get("notionalOI")),
                )
            )

        quotes.sort(key=lambda q: q.maturity)
        return True, quotes

    # ------------------------------------------------------------------ #
    # Account Data                                                         #
    # ------------------------------------------------------------------ #

    async def get_assets(self) -> tuple[bool, list[dict[str, Any]]]:
        """Get all Boros assets (collateral tokens with addresses).

        Returns:
            Tuple of (success, assets) where assets is a list of dicts with:
            - tokenId: int (e.g., 3=USDT, 5=HYPE)
            - address: str (Arbitrum address)
            - symbol: str
            - decimals: int
            - isCollateral: bool
        """
        try:
            assets = await self.boros_client.get_assets()
            return True, assets
        except Exception as e:
            logger.error(f"Failed to get assets: {e}")
            return False, []

    async def get_asset_by_token_id(
        self, token_id: int
    ) -> tuple[bool, dict[str, Any] | None]:
        """Get a specific asset by its token ID.

        Args:
            token_id: Boros token ID (e.g., 3=USDT, 5=HYPE)

        Returns:
            Tuple of (success, asset_dict or None)
        """
        success, assets = await self.get_assets()
        if not success:
            return False, None
        for asset in assets:
            if asset.get("tokenId") == token_id:
                return True, asset
        return True, None  # Not found but no error

    async def get_market_history(
        self,
        market_id: int,
        *,
        time_frame: str = "1h",
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Get historical rate data for a market.

        Args:
            market_id: Boros market ID.
            time_frame: Time frame for candles (5m, 1h, 1d, 1w).
            start_ts: Start timestamp (Unix seconds).
            end_ts: End timestamp (Unix seconds).

        Returns:
            Tuple of (success, list of OHLCV + rate data dicts).
        """
        try:
            history = await self.boros_client.get_market_history(
                market_id,
                time_frame=time_frame,
                start_ts=start_ts,
                end_ts=end_ts,
            )
            return True, history
        except Exception as e:
            logger.error(f"Failed to get market history for {market_id}: {e}")
            return False, []

    async def list_available_underlyings(
        self,
        *,
        active_only: bool = True,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """List unique underlying symbols available on Boros.

        Args:
            active_only: If True, only include markets that are active (state=Normal, not expired).

        Returns:
            Tuple of (success, list of dicts with symbol, markets_count, platforms).
        """
        try:
            ok, markets = await self.list_markets_all(is_whitelisted=True)
            if not ok:
                return False, []

            now_ts = int(time.time())
            underlyings: dict[str, dict[str, Any]] = {}

            for m in markets:
                if active_only:
                    state = m.get("state") or ""
                    maturity_ts = self._extract_maturity_ts(m) or 0
                    is_active = state.lower() == "normal" and maturity_ts > now_ts
                    if not is_active:
                        continue

                symbol = self._extract_underlying(m).upper()
                if not symbol:
                    continue

                metadata = m.get("metadata") or {}
                plat = m.get("platform") or {}
                platform_name = (
                    metadata.get("platformName") or plat.get("name") or "Unknown"
                )

                if symbol not in underlyings:
                    underlyings[symbol] = {
                        "symbol": symbol,
                        "markets_count": 0,
                        "platforms": set(),
                    }

                underlyings[symbol]["markets_count"] += 1
                underlyings[symbol]["platforms"].add(platform_name)

            result = []
            for u in sorted(underlyings.values(), key=lambda x: x["symbol"]):
                result.append(
                    {
                        "symbol": u["symbol"],
                        "markets_count": u["markets_count"],
                        "platforms": sorted(u["platforms"]),
                    }
                )

            return True, result
        except Exception as e:
            logger.error(f"Failed to list available underlyings: {e}")
            return False, []

    async def list_available_platforms(
        self,
        *,
        active_only: bool = True,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """List available trading platforms on Boros.

        Args:
            active_only: If True, only count markets that are active (state=Normal, not expired).

        Returns:
            Tuple of (success, list of dicts with platform name and market count).
            Example: [{"platform": "Hyperliquid", "markets_count": 12}, ...]
        """
        try:
            ok, markets = await self.list_markets_all(is_whitelisted=True)
            if not ok:
                return False, []

            now_ts = int(time.time())
            platforms: dict[str, int] = {}

            for m in markets:
                if active_only:
                    state = m.get("state") or ""
                    maturity_ts = self._extract_maturity_ts(m) or 0
                    is_active = state.lower() == "normal" and maturity_ts > now_ts
                    if not is_active:
                        continue

                metadata = m.get("metadata") or {}
                plat = m.get("platform") or {}
                platform_name = (
                    metadata.get("platformName") or plat.get("name") or "Unknown"
                )

                platforms[platform_name] = platforms.get(platform_name, 0) + 1

            result = [
                {"platform": name, "markets_count": count}
                for name, count in sorted(platforms.items())
            ]

            return True, result
        except Exception as e:
            logger.error(f"Failed to list available platforms: {e}")
            return False, []

    async def list_markets_by_collateral(
        self,
        token_id: int,
        *,
        active_only: bool = True,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """List markets filtered by Boros collateral token ID.

        This is a convenience wrapper around `search_markets(collateral=...)`.
        """
        return await self.search_markets(
            collateral=int(token_id),
            active_only=active_only,
        )

    async def search_markets(
        self,
        *,
        collateral: int | None = None,
        asset: str | None = None,
        platform: str | None = None,
        active_only: bool = True,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Search markets with optional filters.

        Args:
            collateral: Filter by collateral token ID (e.g., 3=USDT, 5=HYPE).
                Use get_assets() to discover valid token IDs.
            asset: Filter by underlying asset symbol being hedged (e.g., "ETH", "BTC").
                Use list_available_underlyings() to discover valid symbols.
            platform: Filter by trading platform (e.g., "Hyperliquid", "Binance").
                Use list_available_platforms() to discover valid platforms.
            active_only: Only return active markets (state=Normal, not expired).

        Returns:
            Tuple of (success, list of enriched market dicts).
        """
        try:
            ok, markets = await self.list_markets_all(is_whitelisted=True)
            if not ok:
                return False, []

            assets_by_id = await self._fetch_assets_by_id()

            now_ts = int(time.time())
            filtered = []

            for m in markets:
                if collateral is not None:
                    market_token_id = m.get("tokenId")
                    if market_token_id != collateral:
                        continue

                if asset is not None:
                    underlying = self._extract_underlying(m).upper()
                    if underlying != asset.upper():
                        continue

                if platform is not None:
                    metadata = m.get("metadata") or {}
                    plat = m.get("platform") or {}
                    platform_name = (
                        metadata.get("platformName") or plat.get("name") or ""
                    )
                    if platform_name.lower() != platform.lower():
                        continue

                if active_only:
                    state = m.get("state") or ""
                    maturity_ts = self._extract_maturity_ts(m) or 0
                    is_active = state.lower() == "normal" and maturity_ts > now_ts
                    if not is_active:
                        continue

                enriched = self._enrich_market(m, assets_by_id)
                filtered.append(enriched)

            filtered.sort(key=lambda x: x.get("maturity_ts", 0))
            return True, filtered
        except Exception as e:
            logger.error(f"Failed to search markets: {e}")
            return False, []

    async def get_all_markets(
        self,
        *,
        is_whitelisted: bool | None = True,
        active_only: bool = False,
        include_vault_summary: bool = True,
        include_history_summary: bool = True,
        history_time_frame: str = "1h",
        history_points: int = 24,
        account: str | None = None,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        """Return normalized Boros markets with nested rate, vault, and history info."""
        try:
            account_param = self._normalize_account_param(account)
            raw_summary_task = (
                self.boros_client.get_amm_summary(account=account_param)
                if include_vault_summary
                else asyncio.sleep(0, result=None)
            )
            markets_result, assets_by_id, raw_summary = await asyncio.gather(
                self.list_markets_all(is_whitelisted=is_whitelisted),
                self._fetch_assets_by_id(),
                raw_summary_task,
            )

            ok_markets, markets = markets_result
            if not ok_markets or not isinstance(markets, list):
                return False, markets

            markets_by_id = {
                int(market.get("marketId") or market.get("id") or 0): market
                for market in markets
                if isinstance(market, dict)
            }
            rows: list[dict[str, Any]] = []
            rows_by_market_id: dict[int, dict[str, Any]] = {}

            for market in markets:
                if not isinstance(market, dict):
                    continue
                row = dict(self._enrich_market(market, assets_by_id))
                if active_only and not bool(row.get("is_active")):
                    continue

                market_id = int(row.get("market_id") or 0)
                row["market_address"] = (
                    market.get("address") or market.get("marketAddress") or ""
                )
                row.pop("mid_apr", None)
                row.pop("floating_apr", None)
                row.pop("mark_apr", None)
                row["rates"] = self._market_rates_from_market(market)
                row["vault"] = None
                row["history"] = None
                rows.append(row)
                if market_id > 0:
                    rows_by_market_id[market_id] = row

            if include_vault_summary:
                vaults = await self._build_vaults_from_raw_summary(
                    raw_summary=raw_summary,
                    markets_by_id=markets_by_id,
                    assets_by_id=assets_by_id,
                    account=account,
                    use_direct_lp_query=True,
                    include_expired=True,
                )
                for vault in vaults:
                    row = rows_by_market_id.get(int(vault.market_id))
                    if row is None:
                        continue
                    row["vault"] = self._vault_summary_payload(
                        vault,
                        include_user=account is not None,
                    )
                    row["rates"]["vault_apy"] = (
                        float(vault.apy) if vault.apy is not None else None
                    )

                if account is not None:
                    for vault in vaults:
                        market_id = int(vault.market_id)
                        if market_id <= 0 or market_id in rows_by_market_id:
                            continue
                        if not self._vault_has_user_position(vault):
                            continue
                        row = self._market_row_from_vault_only(
                            vault,
                            assets_by_id=assets_by_id,
                            include_user=True,
                        )
                        rows.append(row)
                        rows_by_market_id[market_id] = row

            if include_history_summary and rows:
                try:
                    requested_points = max(int(history_points), 0)
                except (TypeError, ValueError):
                    requested_points = 24
                step_seconds = self._time_frame_seconds(history_time_frame)
                end_ts = (
                    int(time.time()) if step_seconds and requested_points > 0 else None
                )
                start_ts = (
                    end_ts - (step_seconds * requested_points)
                    if end_ts is not None
                    else None
                )
                sem = asyncio.Semaphore(10)

                async def _history_for_market(
                    market_id: int,
                ) -> tuple[int, dict[str, Any] | None]:
                    async with sem:
                        try:
                            ok_history, history = await self.get_market_history(
                                market_id,
                                time_frame=history_time_frame,
                                start_ts=start_ts,
                                end_ts=end_ts,
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                f"Failed to fetch Boros history for market {market_id}: {exc}"
                            )
                            return market_id, None

                    if not ok_history or not isinstance(history, list):
                        return market_id, None

                    return market_id, self._summarize_market_history(
                        history,
                        time_frame=history_time_frame,
                        requested_points=requested_points,
                    )

                history_results = await asyncio.gather(
                    *[
                        _history_for_market(int(row.get("market_id") or 0))
                        for row in rows
                        if int(row.get("market_id") or 0) > 0
                    ]
                )
                for market_id, summary in history_results:
                    row = rows_by_market_id.get(int(market_id))
                    if row is not None:
                        row["history"] = summary

            rows.sort(key=lambda item: item.get("maturity_ts") or 0)
            return True, rows
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Failed to get Boros markets: {exc}")
            return False, str(exc)

    async def get_enriched_market(
        self,
        market_id: int,
    ) -> tuple[bool, dict[str, Any]]:
        """Get a single market with all metadata joined.

        Returns the market plus collateral asset info, margin type, and status.

        Args:
            market_id: Boros market ID.

        Returns:
            Tuple of (success, enriched market dict).
        """
        try:
            ok, market = await self.get_market(market_id)
            if not ok:
                return False, {"error": f"Market {market_id} not found"}

            assets_by_id = await self._fetch_assets_by_id()

            enriched = self._enrich_market(market, assets_by_id)
            return True, enriched
        except Exception as e:
            logger.error(f"Failed to get enriched market {market_id}: {e}")
            return False, {"error": str(e)}

    async def _fetch_assets_by_id(self) -> dict[int, dict[str, Any]]:
        if self._assets_by_id_cache is not None:
            return self._assets_by_id_cache
        ok_assets, assets = await self.get_assets()
        assets_by_id: dict[int, dict[str, Any]] = {}
        if ok_assets and assets:
            for a in assets:
                tid = a.get("tokenId")
                if tid is not None:
                    assets_by_id[tid] = a
        self._assets_by_id_cache = assets_by_id
        return assets_by_id

    @staticmethod
    def _collateral_payload(
        *,
        token_id: int | None,
        asset: dict[str, Any] | None = None,
        symbol: str | None = None,
        address: str | None = None,
        decimals: int = 18,
    ) -> dict[str, Any] | None:
        if isinstance(asset, dict):
            return {
                "token_id": token_id if token_id is not None else asset.get("tokenId"),
                "symbol": asset.get("symbol") or symbol or "",
                "address": asset.get("address") or address or "",
                "decimals": asset.get("decimals") or decimals,
            }
        if token_id is None and not symbol and not address:
            return None
        return {
            "token_id": token_id,
            "symbol": symbol or "",
            "address": address or "",
            "decimals": decimals,
        }

    def _enrich_market(
        self,
        market: dict[str, Any],
        assets_by_id: dict[int, dict[str, Any]],
    ) -> dict[str, Any]:
        """Enrich a market dict with joined asset metadata.

        Adds collateral info, margin type, and status fields.
        """
        market_id = int(market.get("marketId") or market.get("id") or 0)
        symbol = self._extract_symbol(market)
        underlying_symbol = self._extract_underlying(market)
        maturity_ts = self._extract_maturity_ts(market) or 0
        now_ts = int(time.time())
        tenor_days = self._time_to_maturity_days(maturity_ts) if maturity_ts else 0.0

        # Platform
        metadata = market.get("metadata") or {}
        plat = market.get("platform") or {}
        platform = metadata.get("platformName") or plat.get("name") or "Unknown"

        # Collateral info
        token_id = market.get("tokenId")
        collateral = self._collateral_payload(
            token_id=token_id,
            asset=assets_by_id.get(token_id) if token_id is not None else None,
        )

        # Margin type
        im_data = market.get("imData") or {}
        is_isolated_only = bool(im_data.get("isIsolatedOnly", False))
        max_leverage = im_data.get("maxLeverage") or im_data.get("leverage")

        # Status
        state = market.get("state") or ""
        is_active = state.lower() == "normal" and maturity_ts > now_ts

        # Current rates from data field
        data = market.get("data") or {}
        mid_apr = self.normalize_apr(data.get("midApr"))
        floating_apr = self.normalize_apr(data.get("floatingApr"))
        mark_apr = self.normalize_apr(data.get("markApr"))

        return {
            "market_id": market_id,
            "symbol": symbol,
            "underlying_symbol": underlying_symbol,
            "platform": platform,
            "collateral": collateral,
            "is_isolated_only": is_isolated_only,
            "max_leverage": max_leverage,
            "state": state,
            "is_active": is_active,
            "maturity_ts": maturity_ts,
            "tenor_days": tenor_days,
            "mid_apr": mid_apr,
            "floating_apr": floating_apr,
            "mark_apr": mark_apr,
        }

    def _market_rates_from_market(
        self,
        market: dict[str, Any],
        *,
        vault: BorosVault | None = None,
    ) -> dict[str, Any]:
        data = market.get("data") or {}
        if not isinstance(data, dict):
            data = {}

        best_bid_apr = self.normalize_apr(data.get("bestBid"))
        best_ask_apr = self.normalize_apr(data.get("bestAsk"))
        mid_apr = self.normalize_apr(data.get("midApr"))
        if mid_apr is None and best_bid_apr is not None and best_ask_apr is not None:
            mid_apr = (best_bid_apr + best_ask_apr) / 2.0

        return {
            "floating_apr": self.normalize_apr(data.get("floatingApr")),
            "mark_apr": self.normalize_apr(data.get("markApr")),
            "vault_apy": float(vault.apy) if vault and vault.apy is not None else None,
            "mid_apr": mid_apr,
            "best_bid_apr": best_bid_apr,
            "best_ask_apr": best_ask_apr,
            "long_yield_apr": self.normalize_apr(data.get("longYieldApr")),
            "funding_7d_ma_apr": self.normalize_apr(data.get("b7dmafr")),
            "funding_30d_ma_apr": self.normalize_apr(data.get("b30dmafr")),
            "volume_24h": self._to_float(data.get("volume24h")),
            "notional_oi": self._to_float(data.get("notionalOI")),
            "asset_mark_price": self._to_float(data.get("assetMarkPrice")),
            "next_settlement_time": self._to_int(data.get("nextSettlementTime")),
            "last_traded_apr": self.normalize_apr(data.get("lastTradedApr")),
            "amm_implied_apr": self.normalize_apr(data.get("ammImpliedApr")),
        }

    def _summarize_market_history(
        self,
        candles: list[dict[str, Any]],
        *,
        time_frame: str,
        requested_points: int,
    ) -> dict[str, Any] | None:
        window: list[tuple[int, dict[str, Any]]] = []
        for candle in candles:
            if not isinstance(candle, dict):
                continue
            ts = self._maybe_int(candle.get("ts"))
            if ts is None:
                ts = self._maybe_int(candle.get("t"))
            if ts is None:
                continue
            window.append((ts, candle))

        if not window:
            return None

        window.sort(key=lambda item: item[0])
        if requested_points > 0:
            window = window[-requested_points:]

        mark_rates = [self.normalize_apr(candle.get("mr")) for _, candle in window]
        floating_rates = [self.normalize_apr(candle.get("ofr")) for _, candle in window]
        funding_7d = [self.normalize_apr(candle.get("b7dmafr")) for _, candle in window]
        funding_30d = [
            self.normalize_apr(candle.get("b30dmafr")) for _, candle in window
        ]

        return {
            "time_frame": str(time_frame),
            "points": len(window),
            "start_ts": window[0][0],
            "end_ts": window[-1][0],
            "latest_mark_rate": self._last_defined(mark_rates),
            "avg_mark_rate": self._average_defined(mark_rates),
            "latest_floating_rate": self._last_defined(floating_rates),
            "avg_floating_rate": self._average_defined(floating_rates),
            "latest_funding_7d_ma_apr": self._last_defined(funding_7d),
            "latest_funding_30d_ma_apr": self._last_defined(funding_30d),
        }

    def _vault_summary_payload(
        self,
        vault: BorosVault,
        *,
        include_user: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "amm_id": int(vault.amm_id),
            "apy": float(vault.apy) if vault.apy is not None else None,
            "expiry": vault.expiry,
            "collateral_symbol": vault.collateral_symbol,
            "collateral_address": vault.collateral_address,
            "collateral_price_usd": vault.collateral_price_usd,
            "tvl": vault.tvl,
            "tvl_usd": vault.tvl_usd,
            "available_tokens": vault.available_tokens,
            "available_usd": vault.available_usd,
            "remaining_supply_pct": vault.remaining_supply_pct,
            "is_expired": bool(vault.is_expired),
            "is_isolated_only": bool(vault.is_isolated_only),
            "market_state": vault.market_state,
            "is_open_for_deposit": self.is_vault_open_for_deposit(
                vault,
                allow_isolated_only=True,
            ),
        }
        if include_user:
            payload["user"] = {
                "deposited_tokens": vault.user_deposit_tokens,
                "deposited_usd": vault.user_deposit_usd,
                "available_tokens": vault.user_available_tokens,
                "available_usd": vault.user_available_usd,
                "total_lp_wei": vault.user_total_lp_wei,
            }
        return payload

    @staticmethod
    def _vault_has_user_position(vault: BorosVault) -> bool:
        return (vault.user_total_lp_wei or 0) > 0 or (
            vault.user_deposit_tokens or 0.0
        ) > 0.0

    def _market_row_from_vault_only(
        self,
        vault: BorosVault,
        *,
        assets_by_id: dict[int, dict[str, Any]],
        include_user: bool,
    ) -> dict[str, Any]:
        collateral_asset = (
            assets_by_id.get(int(vault.collateral_token_id))
            if vault.collateral_token_id is not None
            else None
        )
        collateral = self._collateral_payload(
            token_id=vault.collateral_token_id,
            asset=collateral_asset,
            symbol=vault.collateral_symbol,
            address=vault.collateral_address,
        )

        return {
            "market_id": int(vault.market_id),
            "market_address": "",
            "symbol": vault.market_symbol
            or vault.symbol
            or f"BOROS-MARKET-{int(vault.market_id)}",
            "underlying_symbol": vault.base_symbol or "",
            "platform": "Unknown",
            "collateral": collateral,
            "is_isolated_only": bool(vault.is_isolated_only),
            "max_leverage": None,
            "state": vault.market_state
            or ("Expired" if vault.is_expired else "Unknown"),
            "is_active": False,
            "maturity_ts": vault.maturity_ts,
            "tenor_days": vault.tenor_days,
            "rates": self._market_rates_from_market({}, vault=vault),
            "vault": self._vault_summary_payload(vault, include_user=include_user),
            "history": None,
        }

    @staticmethod
    def _coerce_vault_list(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [entry for entry in data if isinstance(entry, dict)]
        if not isinstance(data, dict):
            return []

        collaterals = data.get("collaterals")
        if isinstance(collaterals, list):
            flattened: list[dict[str, Any]] = []
            for coll in collaterals:
                if not isinstance(coll, dict):
                    continue
                coll_token_id = coll.get("tokenId")
                coll_addr = coll.get("collateralAddress")
                for vault in coll.get("vaults") or []:
                    if not isinstance(vault, dict):
                        continue
                    enriched = dict(vault)
                    enriched.setdefault("collateralTokenId", coll_token_id)
                    enriched.setdefault("collateralAddress", coll_addr)
                    flattened.append(enriched)
            if flattened:
                return flattened

        for key in ("results", "vaults", "data", "amm", "ammSummary", "ammStates"):
            value = data.get(key)
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
        return []

    async def _build_vaults_from_raw_summary(
        self,
        *,
        raw_summary: Any,
        markets_by_id: dict[int, dict[str, Any]],
        assets_by_id: dict[int, dict[str, Any]],
        account: str | None = None,
        use_direct_lp_query: bool = True,
        include_expired: bool = True,
    ) -> list[BorosVault]:
        raw_vaults = self._coerce_vault_list(raw_summary)
        assets_by_address = {
            str(asset.get("address") or "").lower(): asset
            for asset in assets_by_id.values()
            if isinstance(asset, dict) and asset.get("address")
        }

        vaults: list[BorosVault] = []
        for raw_vault in raw_vaults:
            market_id = self._to_int(raw_vault.get("marketId"))
            market_meta = markets_by_id.get(market_id)
            raw_entry = dict(raw_vault)
            if isinstance(market_meta, dict):
                if raw_entry.get("tokenId") is None:
                    raw_entry["tokenId"] = market_meta.get("tokenId")
                if raw_entry.get("collateralTokenId") is None:
                    raw_entry["collateralTokenId"] = market_meta.get(
                        "collateralTokenId"
                    ) or ((market_meta.get("market") or {}).get("tokenId"))
            collateral_token_id = self._maybe_int(
                raw_entry.get("collateralTokenId") or raw_entry.get("tokenId")
            )
            collateral_address = str(raw_entry.get("collateralAddress") or "").lower()
            collateral_asset = (
                assets_by_id.get(collateral_token_id)
                if collateral_token_id is not None
                else None
            ) or (
                assets_by_address.get(collateral_address)
                if collateral_address
                else None
            )
            raw_entry["_market_meta"] = market_meta
            raw_entry["_collateral_asset"] = collateral_asset
            raw_entry["_is_expired"] = market_id > 0 and market_meta is None
            vault = self._vault_from_raw(raw_entry)
            vaults.append(vault)
            if vault.amm_id > 0:
                self._vault_context_cache[vault.amm_id] = {
                    "amm_id": int(vault.amm_id),
                    "market_id": int(vault.market_id),
                    "token_id": int(vault.collateral_token_id or 0),
                    "is_isolated_only": bool(vault.is_isolated_only),
                }
            if vault.market_id > 0 and vault.amm_id > 0:
                self._amm_id_by_market_cache[vault.market_id] = vault.amm_id

        if account and use_direct_lp_query:
            vaults = await self._augment_vault_lp_balances(vaults, account=account)

        if not include_expired:
            vaults = [vault for vault in vaults if not vault.is_expired]

        return vaults

    @staticmethod
    def estimate_user_lp_balance_wei(vault: BorosVault) -> int | None:
        if vault.user_total_lp_wei is not None:
            return int(vault.user_total_lp_wei)
        raw = vault.raw or {}
        try:
            candidate = raw.get("userTotalLp") or (
                (raw.get("user") or {}).get("totalLp")
            )
            return int(candidate) if candidate is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def estimate_user_vault_value_tokens(
        vault: BorosVault,
        *,
        prefer_lp_balance: bool = False,
    ) -> float:
        if vault.user_deposit_tokens is not None and not prefer_lp_balance:
            return float(vault.user_deposit_tokens)
        lp_wei = BorosAdapter.estimate_user_lp_balance_wei(vault)
        lp_price = vault.lp_price
        if lp_price is None:
            lp_price = (vault.raw or {}).get("lpPrice")
        try:
            if lp_wei is None or lp_price is None:
                return 0.0
            return (float(lp_wei) * float(lp_price)) / 1e18
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def estimate_vault_capacity_tokens(vault: BorosVault) -> float | None:
        if vault.available_tokens is not None:
            return float(vault.available_tokens)
        if vault.remaining_supply_lp is None:
            return None
        lp_price = vault.lp_price
        if lp_price is None:
            lp_price = (vault.raw or {}).get("lpPrice")
        try:
            if lp_price is None:
                return None
            return (float(vault.remaining_supply_lp) * float(lp_price)) / 1e18
        except (TypeError, ValueError):
            return None

    @staticmethod
    def estimate_vault_capacity_usd(vault: BorosVault) -> float | None:
        if vault.available_usd is not None:
            return float(vault.available_usd)
        capacity_tokens = BorosAdapter.estimate_vault_capacity_tokens(vault)
        return BorosAdapter._tokens_to_usd(capacity_tokens, vault.collateral_price_usd)

    @staticmethod
    def estimate_user_vault_value_usd(
        vault: BorosVault,
        *,
        prefer_lp_balance: bool = False,
    ) -> float | None:
        if vault.user_deposit_usd is not None and not prefer_lp_balance:
            return float(vault.user_deposit_usd)
        tokens = BorosAdapter.estimate_user_vault_value_tokens(
            vault,
            prefer_lp_balance=prefer_lp_balance,
        )
        return BorosAdapter._tokens_to_usd(tokens, vault.collateral_price_usd)

    @staticmethod
    def is_vault_open_for_deposit(
        vault: BorosVault,
        *,
        min_tenor_days: float = 0.0,
        allow_isolated_only: bool = False,
    ) -> bool:
        if vault.is_expired:
            return False
        if vault.is_isolated_only and not allow_isolated_only:
            return False
        if str(vault.market_state or "").lower() == "paused":
            return False
        if vault.tenor_days is not None and float(vault.tenor_days) < float(
            min_tenor_days
        ):
            return False
        return True

    def _vault_from_raw(self, entry: dict[str, Any]) -> BorosVault:
        market = entry.get("market") or {}
        market_meta = entry.get("_market_meta") if isinstance(entry, dict) else None
        collateral_asset = (
            entry.get("_collateral_asset") if isinstance(entry, dict) else None
        )
        is_expired = bool(entry.get("_is_expired", False))

        symbol = (
            entry.get("ammSymbol")
            or entry.get("marketSymbol")
            or entry.get("symbol")
            or market.get("symbol")
            or entry.get("name")
            or ""
        )
        base_symbol, quote_symbol = self._split_symbol(symbol)
        lp_price = self._maybe_float(entry.get("lpPrice"))
        total_value_tokens = self._wei_amount_to_tokens(entry.get("totalValue"))
        if total_value_tokens is None:
            total_value_tokens = self._maybe_float(entry.get("tvl"))
        apy = self._maybe_float(
            entry.get("lpApy")
            or entry.get("apy")
            or entry.get("apy24h")
            or entry.get("apyPct")
        )

        collateral_token_id = self._maybe_int(
            entry.get("collateralTokenId")
            or entry.get("tokenId")
            or (
                (market_meta or {}).get("tokenId")
                if isinstance(market_meta, dict)
                else None
            )
        )
        collateral_address = (
            entry.get("collateralAddress")
            or (
                (collateral_asset or {}).get("address")
                if isinstance(collateral_asset, dict)
                else None
            )
            or (market.get("collateralAddress") if isinstance(market, dict) else None)
        )
        collateral_symbol = (
            ((collateral_asset or {}).get("metadata") or {}).get("proSymbol")
            if isinstance(collateral_asset, dict)
            else None
        ) or (
            (collateral_asset or {}).get("symbol")
            if isinstance(collateral_asset, dict)
            else None
        )
        collateral_price_usd = self._maybe_float(
            (collateral_asset or {}).get("usdPrice")
            if isinstance(collateral_asset, dict)
            else None
        )

        supply_cap = self._maybe_int(entry.get("totalSupplyCap"))
        total_lp = self._maybe_int(entry.get("totalLp"))
        remaining_lp = (
            max(int(supply_cap) - int(total_lp), 0)
            if supply_cap is not None and total_lp is not None
            else None
        )
        remaining_pct = (
            (remaining_lp / supply_cap)
            if supply_cap not in (None, 0) and remaining_lp is not None
            else None
        )
        available_tokens = (
            (float(remaining_lp) * float(lp_price)) / 1e18
            if remaining_lp is not None and lp_price is not None
            else None
        )
        tvl_usd = self._maybe_float(
            entry.get("tvlUsd") or entry.get("tvlUSD") or entry.get("tvl_usd")
        )
        if tvl_usd is None:
            tvl_usd = self._tokens_to_usd(total_value_tokens, collateral_price_usd)
        available_usd = self._tokens_to_usd(available_tokens, collateral_price_usd)

        maturity_ts: int | None = None
        tenor_days: float | None = None
        expiry: str | None = None
        if isinstance(market_meta, dict):
            symbol = (
                (market_meta.get("imData") or {}).get("symbol")
                or (market_meta.get("metadata") or {}).get("name")
                or symbol
            )
            if symbol:
                base_symbol, quote_symbol = self._split_symbol(symbol)
            maturity_ts = self._extract_maturity_ts(market_meta)
            tenor_days = (
                time_to_maturity_days(maturity_ts) if maturity_ts is not None else None
            )
            expiry = self._maturity_ts_to_expiry(maturity_ts)
            underlying_symbol = self._extract_underlying(market_meta)
            if underlying_symbol:
                base_symbol = underlying_symbol
            if collateral_symbol:
                quote_symbol = collateral_symbol
        im_data = (
            (market_meta or {}).get("imData") if isinstance(market_meta, dict) else {}
        )
        is_isolated_only = bool((im_data or {}).get("isIsolatedOnly"))
        market_state = (
            str((market_meta or {}).get("state"))
            if isinstance(market_meta, dict)
            and (market_meta or {}).get("state") is not None
            else None
        )

        user = entry.get("user") or {}
        user_deposit_tokens = self._wei_amount_to_tokens(user.get("depositValue"))
        user_available_tokens = self._wei_amount_to_tokens(
            user.get("availableBalanceToDeposit")
        )
        user_deposit_usd = self._tokens_to_usd(
            user_deposit_tokens,
            collateral_price_usd,
        )
        user_available_usd = self._tokens_to_usd(
            user_available_tokens,
            collateral_price_usd,
        )
        user_total_lp_wei = None
        try:
            total_lp_raw = user.get("totalLp")
            if total_lp_raw is not None:
                user_total_lp_wei = int(total_lp_raw)
        except (TypeError, ValueError):
            user_total_lp_wei = None

        return BorosVault(
            amm_id=self._to_int(
                entry.get("ammId") or entry.get("id") or entry.get("amm_id")
            ),
            market_id=self._to_int(
                entry.get("marketId")
                or entry.get("market_id")
                or market.get("marketId")
            ),
            symbol=symbol,
            market_symbol=entry.get("marketSymbol")
            or market.get("symbol")
            or (
                (market_meta.get("metadata") or {}).get("name") if market_meta else None
            ),
            base_symbol=base_symbol,
            quote_symbol=quote_symbol,
            collateral_token_id=collateral_token_id,
            collateral_symbol=collateral_symbol,
            collateral_address=collateral_address,
            collateral_price_usd=collateral_price_usd,
            apy=apy,
            tvl=total_value_tokens,
            tvl_usd=tvl_usd,
            lp_token_address=entry.get("lpToken")
            or entry.get("lpTokenAddress")
            or entry.get("ammAddress"),
            lp_price=lp_price,
            total_lp_wei=total_lp,
            total_supply_cap_lp=supply_cap,
            remaining_supply_lp=remaining_lp,
            remaining_supply_pct=remaining_pct,
            available_tokens=available_tokens,
            available_usd=available_usd,
            maturity_ts=maturity_ts,
            expiry=expiry,
            tenor_days=tenor_days,
            is_expired=is_expired,
            is_isolated_only=is_isolated_only,
            market_state=market_state,
            user_deposit_tokens=user_deposit_tokens,
            user_deposit_usd=user_deposit_usd,
            user_available_tokens=user_available_tokens,
            user_available_usd=user_available_usd,
            user_total_lp_wei=user_total_lp_wei,
            raw=entry,
        )

    async def _get_amm_address_from_router(self, amm_id: int) -> str | None:
        if amm_id in self._amm_address_cache:
            return self._amm_address_cache[amm_id]
        try:
            async with web3_from_chain_id(self.chain_id) as web3:
                contract = web3.eth.contract(
                    address=to_checksum_address(BOROS_ROUTER),
                    abi=BOROS_ROUTER_VIEW_ABI,
                )
                market_acc = await contract.functions.ammIdToAcc(int(amm_id)).call()
            result: str | None = None
            if market_acc and len(market_acc) >= 20:
                result = to_checksum_address("0x" + market_acc[:20].hex())
            self._amm_address_cache[amm_id] = result
            return result
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to resolve Boros AMM address {amm_id}: {exc}")
        return None

    async def get_vault_lp_balance(
        self,
        *,
        amm_id: int,
        token_id: int,
        account: str | None = None,
        account_id: int = 0,
        market_id: int | None = None,
        is_isolated_only: bool | None = None,
    ) -> tuple[bool, int | str]:
        address = account or self.wallet_address
        if not address:
            return False, "wallet_address is required"

        amm_address = await self._get_amm_address_from_router(int(amm_id))
        if not amm_address:
            return False, f"Could not resolve amm address for amm_id={int(amm_id)}"

        resolved_market_id = (
            int(market_id) if market_id is not None else self.CROSS_MARGIN_MARKET_ID
        )
        if market_id is None or is_isolated_only is None:
            context = await self._get_vault_context_for_amm(int(amm_id))
            if context:
                if market_id is None and bool(context.get("is_isolated_only")):
                    resolved_market_id = self._to_int(
                        context.get("market_id"),
                        default=self.CROSS_MARGIN_MARKET_ID,
                    )
                if is_isolated_only is None:
                    is_isolated_only = bool(context.get("is_isolated_only"))

        if not bool(is_isolated_only):
            resolved_market_id = self.CROSS_MARGIN_MARKET_ID

        market_acc = self.build_market_acc(
            address=address,
            account_id=int(account_id),
            token_id=int(token_id),
            market_id=int(resolved_market_id),
        )

        try:
            async with web3_from_chain_id(self.chain_id) as web3:
                contract = web3.eth.contract(
                    address=to_checksum_address(amm_address),
                    abi=BOROS_VAULT_BALANCE_ABI,
                )
                balance = await contract.functions.balanceOf(market_acc).call()
            return True, int(balance)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def _invalidate_lp_cache(self) -> None:
        self._lp_balance_cache = {}

    async def _augment_vault_lp_balances(
        self,
        vaults: list[BorosVault],
        *,
        account: str,
    ) -> list[BorosVault]:
        # Build list of (index, vault, token_id) for vaults that need LP queries
        queryable: list[tuple[int, BorosVault, int]] = []
        for i, vault in enumerate(vaults):
            token_id = int(vault.collateral_token_id or 0)
            if token_id > 0 and vault.amm_id > 0:
                queryable.append((i, vault, token_id))

        if not queryable:
            return vaults

        # Check LP balance cache — resolve what we can, track what needs fetching
        cached_balances: dict[int, int] = {}  # queryable index → balance
        needs_fetch: list[int] = []  # indices into queryable
        for qi, (_, vault, token_id) in enumerate(queryable):
            market_id = (
                int(vault.market_id)
                if bool(vault.is_isolated_only)
                else self.CROSS_MARGIN_MARKET_ID
            )
            market_acc = self.build_market_acc(
                address=account,
                account_id=0,
                token_id=int(token_id),
                market_id=market_id,
            )
            cache_key = (vault.amm_id, token_id, market_acc.hex())
            if cache_key in self._lp_balance_cache:
                cached_balances[qi] = self._lp_balance_cache[cache_key]
            else:
                needs_fetch.append(qi)

        if needs_fetch:
            await self._fetch_lp_balances_multicall(
                queryable,
                needs_fetch,
                account,
                cached_balances,
            )

        # Apply all balances (cached + freshly fetched)
        for qi, (_, vault, _) in enumerate(queryable):
            balance_i = cached_balances.get(qi, 0)
            if balance_i <= 0:
                continue
            vault.user_total_lp_wei = balance_i
            vault.user_deposit_tokens = self.estimate_user_vault_value_tokens(
                vault,
                prefer_lp_balance=True,
            )
            vault.user_deposit_usd = self.estimate_user_vault_value_usd(
                vault,
                prefer_lp_balance=True,
            )
        return vaults

    async def _fetch_lp_balances_multicall(
        self,
        queryable: list[tuple[int, BorosVault, int]],
        needs_fetch: list[int],
        account: str,
        out: dict[int, int],
    ) -> None:
        """Batch-fetch LP balances via multicall, populating `out` and caches."""
        async with web3_from_chain_id(self.chain_id) as web3:
            router_contract = web3.eth.contract(
                address=to_checksum_address(BOROS_ROUTER),
                abi=BOROS_ROUTER_VIEW_ABI,
            )

            # --- Round 1: resolve uncached AMM addresses via multicall ---
            amm_resolve_indices: list[int] = []  # indices into needs_fetch
            amm_resolve_calls: list[Call] = []
            for nfi in needs_fetch:
                _, vault, _ = queryable[nfi]
                if vault.amm_id not in self._amm_address_cache:
                    amm_resolve_indices.append(nfi)
                    amm_resolve_calls.append(
                        Call(
                            contract=router_contract,
                            fn_name="ammIdToAcc",
                            args=(int(vault.amm_id),),
                        )
                    )

            if amm_resolve_calls:
                amm_results = await read_only_calls_multicall_or_gather(
                    web3=web3,
                    chain_id=self.chain_id,
                    calls=amm_resolve_calls,
                )
                for nfi, raw_acc in zip(amm_resolve_indices, amm_results, strict=False):
                    _, vault, _ = queryable[nfi]
                    resolved: str | None = None
                    try:
                        if raw_acc and len(raw_acc) >= 20:
                            resolved = to_checksum_address("0x" + raw_acc[:20].hex())
                    except Exception:  # noqa: BLE001
                        pass
                    self._amm_address_cache[vault.amm_id] = resolved

            # --- Round 2: balanceOf calls on resolved AMM contracts ---
            balance_calls: list[Call] = []
            balance_indices: list[int] = []  # indices into needs_fetch
            market_accs: list[bytes] = []
            for nfi in needs_fetch:
                _, vault, token_id = queryable[nfi]
                amm_addr = self._amm_address_cache.get(vault.amm_id)
                if not amm_addr:
                    continue
                market_id = (
                    int(vault.market_id)
                    if bool(vault.is_isolated_only)
                    else self.CROSS_MARGIN_MARKET_ID
                )
                market_acc = self.build_market_acc(
                    address=account,
                    account_id=0,
                    token_id=int(token_id),
                    market_id=market_id,
                )
                amm_contract = web3.eth.contract(
                    address=to_checksum_address(amm_addr),
                    abi=BOROS_VAULT_BALANCE_ABI,
                )
                balance_calls.append(
                    Call(
                        contract=amm_contract,
                        fn_name="balanceOf",
                        args=(market_acc,),
                    )
                )
                balance_indices.append(nfi)
                market_accs.append(market_acc)

            if not balance_calls:
                return

            bal_results = await read_only_calls_multicall_or_gather(
                web3=web3,
                chain_id=self.chain_id,
                calls=balance_calls,
            )
            for nfi, balance_raw, market_acc in zip(
                balance_indices, bal_results, market_accs, strict=False
            ):
                _, vault, token_id = queryable[nfi]
                try:
                    balance_i = int(balance_raw)
                except (TypeError, ValueError):
                    balance_i = 0
                cache_key = (vault.amm_id, token_id, market_acc.hex())
                self._lp_balance_cache[cache_key] = balance_i
                out[nfi] = balance_i

    async def get_vaults_summary(
        self,
        *,
        account: str | None = None,
        use_direct_lp_query: bool = True,
        include_expired: bool = True,
    ) -> tuple[bool, list[BorosVault] | str]:
        try:
            account_param = self._normalize_account_param(account)

            markets_result, assets_by_id, raw_summary = await asyncio.gather(
                self.list_markets_all(is_whitelisted=None),
                self._fetch_assets_by_id(),
                self.boros_client.get_amm_summary(account=account_param),
            )

            ok_markets, markets = markets_result
            markets_by_id = (
                {
                    int(market.get("marketId") or 0): market
                    for market in markets
                    if isinstance(market, dict)
                }
                if ok_markets and isinstance(markets, list)
                else {}
            )
            vaults = await self._build_vaults_from_raw_summary(
                raw_summary=raw_summary,
                markets_by_id=markets_by_id,
                assets_by_id=assets_by_id,
                account=account,
                use_direct_lp_query=use_direct_lp_query,
                include_expired=include_expired,
            )

            return True, vaults
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Failed to get Boros vaults summary: {exc}")
            return False, str(exc)

    async def search_vaults(
        self,
        *,
        asset: str | None = None,
        token_id: int | None = None,
        limit: int = 20,
        account: str | None = None,
        include_expired: bool = True,
    ) -> tuple[bool, list[BorosVault] | str]:
        ok, vaults = await self.get_vaults_summary(
            account=account,
            include_expired=include_expired,
        )
        if not ok or not isinstance(vaults, list):
            return False, vaults

        results = list(vaults)
        if token_id is not None:
            results = [
                vault
                for vault in results
                if int(vault.collateral_token_id or 0) == int(token_id)
            ]

        if asset:
            needle = str(asset).strip().lower().replace("-", "").replace("/", "")
            filtered: list[BorosVault] = []
            for vault in results:
                haystacks = [
                    str(vault.symbol or ""),
                    str(vault.market_symbol or ""),
                    str(vault.base_symbol or ""),
                    str(vault.quote_symbol or ""),
                    str(vault.collateral_symbol or ""),
                ]
                hay = "".join(
                    h.lower().replace("-", "").replace("/", "") for h in haystacks
                )
                if needle in hay:
                    filtered.append(vault)
            results = filtered

        if limit and limit > 0:
            results = results[: int(limit)]
        return True, results

    async def best_yield_vault(
        self,
        *,
        token_id: int = 3,
        amount_tokens: float,
        min_tenor_days: float = 3.0,
        allow_isolated_only: bool = False,
    ) -> tuple[bool, BorosVault | None | str]:
        ok, vaults = await self.search_vaults(
            token_id=token_id,
            limit=0,
            include_expired=False,
        )
        if not ok or not isinstance(vaults, list):
            return False, vaults

        min_isolated_cash_wei: int | None = None
        if allow_isolated_only:
            ok_fee, fee_data = await self.get_cash_fee_data(token_id=int(token_id))
            if ok_fee and isinstance(fee_data, dict):
                raw_min = fee_data.get("min_cash_isolated_wei")
                try:
                    min_isolated_cash_wei = int(raw_min)
                except (TypeError, ValueError):
                    min_isolated_cash_wei = None

        feasible: list[BorosVault] = []
        for vault in vaults:
            if not self.is_vault_open_for_deposit(
                vault,
                min_tenor_days=min_tenor_days,
                allow_isolated_only=allow_isolated_only,
            ):
                continue
            if (
                vault.is_isolated_only
                and min_isolated_cash_wei is not None
                and int(float(amount_tokens) * 1e18) < int(min_isolated_cash_wei)
            ):
                continue
            cap_tokens = self.estimate_vault_capacity_tokens(vault)
            if cap_tokens is not None and cap_tokens < float(amount_tokens):
                continue
            feasible.append(vault)

        if not feasible:
            return True, None

        feasible.sort(key=lambda vault: float(vault.apy or 0.0), reverse=True)
        return True, feasible[0]

    async def get_account_idle_balance(
        self,
        *,
        token_id: int,
        account_id: int = 0,
    ) -> tuple[bool, float | str]:
        ok, balances = await self.get_account_balances(
            token_id=int(token_id), account_id=int(account_id)
        )
        if not ok or not isinstance(balances, dict):
            return False, balances
        return True, float(balances.get("total") or 0.0)

    async def get_collaterals(
        self, *, account_id: int | None = None
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            data = await self.boros_client.get_collaterals(
                user_address=self.wallet_address,
                account_id=account_id,
            )
            return True, data
        except Exception as e:
            logger.error(f"Failed to get collaterals: {e}")
            return False, str(e)

    async def get_account_balances(
        self, token_id: int = 3, *, account_id: int | None = None
    ) -> tuple[bool, dict[str, Any] | str]:
        result: dict[str, Any] = {
            "isolated": 0.0,
            "cross": 0.0,
            "total": 0.0,
            "isolated_wei": 0,
            "cross_wei": 0,
            "isolated_market_id": None,
            "isolated_positions": [],
        }

        try:
            success, summary = await self.get_collaterals(account_id=account_id)
            if not success:
                return False, str(summary)

            collaterals = summary.get("collaterals", [])
            for coll in collaterals:
                if coll.get("tokenId") != token_id:
                    continue

                # Isolated positions
                for iso in coll.get("isolatedPositions", []):
                    net_raw = iso.get("availableBalance") or iso.get("netBalance")
                    if net_raw:
                        try:
                            wei = int(net_raw)
                            result["isolated_wei"] += wei
                            result["isolated"] += cash_wei_to_float(net_raw)
                            # Extract market ID from marketAcc (last 6 hex chars = 3 bytes)
                            market_acc = iso.get("marketAcc", "")
                            market_id = market_id_from_market_acc(market_acc)
                            if market_id is not None:
                                result["isolated_market_id"] = market_id
                                result["isolated_positions"].append(
                                    {
                                        "market_id": market_id,
                                        "balance": cash_wei_to_float(net_raw),
                                        "balance_wei": wei,
                                        "marketAcc": market_acc,
                                    }
                                )
                        except Exception:
                            pass

                # Cross position
                cross = coll.get("crossPosition", {})
                cross_raw = cross.get("availableBalance") or cross.get("netBalance")
                if cross_raw:
                    try:
                        wei = int(cross_raw)
                        result["cross_wei"] += wei
                        result["cross"] += cash_wei_to_float(cross_raw)
                    except Exception:
                        pass

            result["total"] = result["isolated"] + result["cross"]
            result["raw"] = summary  # Include raw data for marketAcc lookup
            return True, result
        except Exception as e:
            logger.error(f"Failed to get account balances: {e}")
            return False, str(e)

    async def get_active_positions(
        self, market_id: int | None = None
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        try:
            success, collaterals = await self.get_collaterals()
            if not success:
                return False, []

            coll_list = collaterals.get("collaterals", [])
            positions: list[dict[str, Any]] = []

            for entry in coll_list:
                token_id = entry.get("tokenId")

                # Cross position
                cross_pos = entry.get("crossPosition", {})
                for mkt_pos in cross_pos.get("marketPositions", []):
                    pos = self._parse_market_position(mkt_pos, token_id, is_cross=True)
                    if pos:
                        positions.append(pos)

                # Isolated positions
                for iso_pos in entry.get("isolatedPositions", []):
                    for mkt_pos in iso_pos.get("marketPositions", []):
                        pos = self._parse_market_position(
                            mkt_pos, token_id, is_cross=False
                        )
                        if pos:
                            positions.append(pos)

            if market_id is not None:
                positions = [p for p in positions if p.get("marketId") == market_id]

            return True, positions
        except Exception as e:
            logger.error(f"Failed to get active positions: {e}")
            return False, str(e)

    async def get_open_limit_orders(
        self, *, limit: int = 50
    ) -> tuple[bool, list[BorosLimitOrder] | str]:
        try:
            orders_raw = await self.boros_client.get_open_orders(
                user_address=self.wallet_address, limit=limit
            )

            orders: list[BorosLimitOrder] = []
            for o in orders_raw:
                try:
                    tick = int(o.get("limitTick") or 0)
                    tick_step = int(o.get("tickStep") or 1)
                    apr = self.rate_from_tick(tick, tick_step)

                    size = float(o.get("size") or 0) / 1e18
                    filled = float(o.get("filledSize") or 0) / 1e18
                    remaining = size - filled

                    orders.append(
                        BorosLimitOrder(
                            order_id=str(o.get("orderId") or o.get("id") or ""),
                            market_id=int(o.get("marketId") or 0),
                            side="long" if int(o.get("side") or 0) == 0 else "short",
                            size=size,
                            limit_tick=tick,
                            limit_apr=apr,
                            filled_size=filled,
                            remaining_size=remaining,
                            status=o.get("status") or "open",
                            raw=o,
                        )
                    )
                except Exception as e:
                    logger.warning(f"Failed to parse order: {e}")

            return True, orders
        except Exception as e:
            logger.error(f"Failed to get open orders: {e}")
            return False, str(e)

    async def get_full_user_state(
        self,
        *,
        account: str,
        account_id: int | None = None,
        token_id: int = 3,
        token_decimals: int = 6,
        open_orders_limit: int = 50,
        include_open_orders: bool = True,
        include_withdrawal_status: bool = True,
    ) -> tuple[bool, dict[str, Any] | str]:
        """
        Full Boros user state snapshot.

        Pulls:
          - Collaterals summary (raw)
          - Parsed positions (cross + isolated)
          - Cash balances for token_id (cross/isolated/total)
          - Open limit orders (optional)
          - Withdrawal status (optional)
        """
        addr = account

        try:
            collaterals = await self.boros_client.get_collaterals(
                user_address=addr,
                account_id=int(
                    account_id if account_id is not None else self.account_id
                ),
            )

            coll_list = collaterals.get("collaterals", [])

            # Positions (cross + isolated)
            positions: list[dict[str, Any]] = []
            for entry in coll_list:
                tid = entry.get("tokenId")

                cross_pos = entry.get("crossPosition", {})
                for mkt_pos in cross_pos.get("marketPositions", []):
                    pos = self._parse_market_position(mkt_pos, tid, is_cross=True)
                    if pos:
                        positions.append(pos)

                for iso_pos in entry.get("isolatedPositions", []):
                    for mkt_pos in iso_pos.get("marketPositions", []):
                        pos = self._parse_market_position(mkt_pos, tid, is_cross=False)
                        if pos:
                            positions.append(pos)

            # Cash balances (token_id only)
            balances: dict[str, Any] = {
                "token_id": int(token_id),
                "isolated": 0.0,
                "cross": 0.0,
                "total": 0.0,
                "isolated_wei": 0,
                "cross_wei": 0,
                "isolated_market_id": None,
                "isolated_positions": [],
            }
            for coll in coll_list:
                if coll.get("tokenId") != int(token_id):
                    continue

                for iso in coll.get("isolatedPositions", []):
                    net_raw = iso.get("availableBalance") or iso.get("netBalance")
                    if net_raw:
                        try:
                            wei = int(net_raw)
                            balances["isolated_wei"] += wei
                            balances["isolated"] += cash_wei_to_float(net_raw)
                            market_acc = iso.get("marketAcc", "")
                            market_id = market_id_from_market_acc(market_acc)
                            if market_id is not None:
                                balances["isolated_market_id"] = market_id
                                balances["isolated_positions"].append(
                                    {
                                        "market_id": market_id,
                                        "balance": cash_wei_to_float(net_raw),
                                        "balance_wei": wei,
                                        "marketAcc": market_acc,
                                    }
                                )
                        except Exception:
                            pass

                cross = coll.get("crossPosition", {})
                cross_raw = cross.get("availableBalance") or cross.get("netBalance")
                if cross_raw:
                    try:
                        wei = int(cross_raw)
                        balances["cross_wei"] += wei
                        balances["cross"] += cash_wei_to_float(cross_raw)
                    except Exception:
                        pass

            balances["total"] = balances["isolated"] + balances["cross"]

            # Orders
            orders: list[dict[str, Any]] | None = None
            if include_open_orders:
                try:
                    orders_raw = await self.boros_client.get_open_orders(
                        user_address=addr, limit=int(open_orders_limit)
                    )
                    parsed: list[dict[str, Any]] = []
                    for o in orders_raw:
                        try:
                            tick = int(o.get("limitTick") or 0)
                            tick_step = int(o.get("tickStep") or 1)
                            apr = self.rate_from_tick(tick, tick_step)

                            size = float(o.get("size") or 0) / 1e18
                            filled = float(o.get("filledSize") or 0) / 1e18
                            remaining = size - filled

                            parsed.append(
                                {
                                    "order_id": str(
                                        o.get("orderId") or o.get("id") or ""
                                    ),
                                    "market_id": int(o.get("marketId") or 0),
                                    "side": "long"
                                    if int(o.get("side") or 0) == 0
                                    else "short",
                                    "size": size,
                                    "limit_tick": tick,
                                    "limit_apr": apr,
                                    "filled_size": filled,
                                    "remaining_size": remaining,
                                    "status": o.get("status") or "open",
                                    "raw": o,
                                }
                            )
                        except Exception as exc:
                            logger.warning(f"Failed to parse order: {exc}")
                    orders = parsed
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"Failed to fetch open orders: {exc}")
                    orders = None

            # Withdrawal status
            withdrawal_status: dict[str, Any] | None = None
            if include_withdrawal_status:
                cooldown_seconds: int | None = None
                cooldown_source = "unknown"
                try:
                    async with web3_from_chain_id(self.chain_id) as web3:
                        market_hub = web3.eth.contract(
                            address=to_checksum_address(BOROS_MARKET_HUB),
                            abi=BOROS_MARKET_HUB_VIEW_ABI,
                        )
                        cooldown_seconds = int(
                            await market_hub.functions.getPersonalCooldown(
                                to_checksum_address(addr)
                            ).call()
                        )
                        cooldown_source = "onchain"
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"Failed to read Boros personal cooldown: {exc}")

                for coll in coll_list:
                    if coll.get("tokenId") != int(token_id):
                        continue

                    withdrawal = coll.get("withdrawal", {})
                    request_time = int(withdrawal.get("lastWithdrawalRequestTime", 0))
                    raw_amount = int(withdrawal.get("lastWithdrawalAmount", 0))
                    amount = (
                        raw_amount / (10 ** int(token_decimals)) if raw_amount else 0.0
                    )

                    current_time = int(time.time())
                    elapsed = current_time - request_time if request_time > 0 else 0
                    if cooldown_seconds is None:
                        cooldown_seconds = 3600
                        cooldown_source = "default_3600s"

                    withdrawal_status = {
                        "amount": amount,
                        "request_time": request_time,
                        "elapsed_seconds": elapsed,
                        "cooldown_seconds": cooldown_seconds,
                        "cooldown_source": cooldown_source,
                        "can_finalize": elapsed >= cooldown_seconds
                        if request_time > 0 and cooldown_seconds is not None
                        else False,
                        "wait_seconds": max(0, cooldown_seconds - elapsed)
                        if request_time > 0 and cooldown_seconds is not None
                        else None,
                    }
                    break

                if withdrawal_status is None:
                    withdrawal_status = {
                        "amount": 0,
                        "request_time": 0,
                        "can_finalize": False,
                    }

            return (
                True,
                {
                    "protocol": "boros",
                    "chainId": int(self.chain_id),
                    "account": addr,
                    "collaterals": collaterals,
                    "balances": balances,
                    "positions": positions,
                    "openOrders": orders,
                    "withdrawal": withdrawal_status,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_pending_withdrawal_amount(
        self, token_id: int = 3, *, token_decimals: int = 6
    ) -> tuple[bool, float]:
        try:
            success, collaterals = await self.get_collaterals()
            if not success:
                return False, 0.0

            amount = self.parse_pending_withdrawal_amount(
                collaterals, token_id=token_id, token_decimals=token_decimals
            )
            return True, amount
        except Exception as e:
            logger.error(f"Failed to get pending withdrawal amount: {e}")
            return False, 0.0

    @staticmethod
    def parse_pending_withdrawal_amount(
        collaterals_data: dict[str, Any],
        *,
        token_id: int,
        token_decimals: int = 6,
    ) -> float:
        """Parse pending withdrawal amount from collaterals response.

        Args:
            collaterals_data: Response from get_collaterals().
            token_id: Boros token ID to look for.
            token_decimals: Token decimals for conversion.

        Returns:
            Pending withdrawal amount in token units (native decimals).
        """
        try:
            coll_list = collaterals_data.get("collaterals", [])
            for coll in coll_list:
                if coll.get("tokenId") != token_id:
                    continue

                # Check withdrawal field (native decimals)
                withdrawal = coll.get("withdrawal", {})
                raw = withdrawal.get("lastWithdrawalAmount", "0")
                if raw and int(raw) > 0:
                    return from_erc20_raw(raw, token_decimals)

            return 0.0
        except Exception as e:
            logger.warning(f"Failed to parse pending withdrawal: {e}")
            return 0.0

    async def get_withdrawal_status(
        self, token_id: int = 3, *, token_decimals: int = 6
    ) -> tuple[bool, dict[str, Any]]:
        """Get withdrawal status including timing info.

        Boros withdrawals can have a user-specific cooldown. Prefer on-chain
        cooldown reads when connected to chain, and treat any fallback
        estimate as advisory only.

        Args:
            token_id: Boros token ID (default 3 = USDT).
            token_decimals: Token decimals for conversion.

        Returns:
            Tuple of (success, status dict with 'amount', 'request_time', 'can_finalize').
        """
        try:
            success, collaterals = await self.get_collaterals()
            if not success:
                return False, {"error": "Failed to get collaterals"}

            cooldown_seconds: int | None = None
            cooldown_source = "unknown"
            if self.wallet_address:
                try:
                    async with web3_from_chain_id(self.chain_id) as web3:
                        market_hub = web3.eth.contract(
                            address=to_checksum_address(BOROS_MARKET_HUB),
                            abi=BOROS_MARKET_HUB_VIEW_ABI,
                        )
                        cooldown_seconds = int(
                            await market_hub.functions.getPersonalCooldown(
                                to_checksum_address(self.wallet_address)
                            ).call()
                        )
                        cooldown_source = "onchain"
                except Exception as exc:
                    logger.warning(f"Failed to read Boros personal cooldown: {exc}")

            for coll in collaterals.get("collaterals", []):
                if coll.get("tokenId") != token_id:
                    continue

                withdrawal = coll.get("withdrawal", {})
                request_time = int(withdrawal.get("lastWithdrawalRequestTime", 0))
                raw_amount = int(withdrawal.get("lastWithdrawalAmount", 0))
                amount = (
                    from_erc20_raw(raw_amount, token_decimals) if raw_amount else 0.0
                )

                current_time = int(time.time())
                elapsed = current_time - request_time if request_time > 0 else 0
                if cooldown_seconds is None:
                    cooldown_seconds = 3600
                    cooldown_source = "default_3600s"

                return True, {
                    "amount": amount,
                    "request_time": request_time,
                    "elapsed_seconds": elapsed,
                    "cooldown_seconds": cooldown_seconds,
                    "cooldown_source": cooldown_source,
                    "can_finalize": elapsed >= cooldown_seconds
                    if request_time > 0 and cooldown_seconds is not None
                    else False,
                    "wait_seconds": max(0, cooldown_seconds - elapsed)
                    if request_time > 0 and cooldown_seconds is not None
                    else None,
                }

            return True, {"amount": 0, "request_time": 0, "can_finalize": False}
        except Exception as e:
            logger.error(f"Failed to get withdrawal status: {e}")
            return False, {"error": str(e)}

    # ------------------------------------------------------------------ #
    # Execution Methods                                                    #
    # ------------------------------------------------------------------ #

    async def deposit_collateral(
        self,
        collateral_address: str,
        amount_wei: int,
        *,
        token_id: int,
        market_id: int,
        target_margin: str = "cross",
    ) -> tuple[bool, dict[str, Any]]:
        """Deposit collateral into Boros margin.

        IMPORTANT: amount_wei is in the collateral token's native decimals.
        Example: USDT has 6 decimals, so 1 USDT = 1_000_000.

        For `target_margin="cross"`, Boros may first credit the cash as isolated for
        `market_id`; this helper then sweeps isolated -> cross.
        """
        try:
            if target_margin not in {"cross", "isolated"}:
                return False, {
                    "error": f"Unsupported target_margin={target_margin!r}",
                }

            # Cap to the on-chain ERC20 balance to avoid off-by-wei estimate reverts.
            try:
                bal_raw_i = await get_token_balance(
                    collateral_address, self.chain_id, self.wallet_address
                )
                if int(amount_wei) > bal_raw_i:
                    logger.warning(
                        "Capping Boros deposit amount to ERC20 balance: "
                        f"requested={int(amount_wei)} bal={bal_raw_i}"
                    )
                    amount_wei = bal_raw_i
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"Failed to check ERC20 balance before Boros deposit: {exc}"
                )

            if int(amount_wei) <= 0:
                return False, {"error": "Insufficient collateral balance for deposit"}

            calldata = await self.boros_client.build_deposit_calldata(
                token_id=token_id,
                amount_wei=amount_wei,
                market_id=market_id,
                user_address=self.wallet_address,
                account_id=0,
            )

            if not self.sign_callback or not self.wallet_address:
                return False, {
                    "error": "sign_callback or user_address not configured",
                    "calldata": calldata,
                }

            # Approve Boros to pull collateral for deposit.
            tx_src = self._unwrap_tx_payload(calldata)
            spender = tx_src.get("to") or calldata.get("to")
            if not isinstance(spender, str) or not spender:
                return False, {
                    "error": "Deposit calldata missing spender address",
                    "calldata": calldata,
                }

            try:
                approve_tx = await build_approve_transaction(
                    from_address=to_checksum_address(self.wallet_address),
                    chain_id=int(self.chain_id),
                    token_address=to_checksum_address(collateral_address),
                    spender_address=to_checksum_address(spender),
                    amount=int(amount_wei),
                )
                approve_hash = await send_transaction(
                    approve_tx, self.sign_callback, wait_for_receipt=True
                )
                approve_res = {"tx_hash": approve_hash}
            except Exception as e:
                return False, {
                    "error": f"ERC20 approval failed: {e}",
                    "approve": {"error": str(e)},
                    "calldata": calldata,
                }

            tx_ok, tx_res = await self._broadcast_calldata(calldata)
            if not tx_ok:
                return False, {
                    "error": f"Deposit transaction failed: {tx_res.get('error') or tx_res}",
                    "approve": approve_res,
                    "calldata": calldata,
                    "tx": tx_res,
                }

            if target_margin == "isolated":
                return True, {
                    "status": "ok",
                    "target_margin": target_margin,
                    "approve": approve_res,
                    "tx": tx_res,
                }

            sweep_ok, sweep_res = await self.sweep_isolated_to_cross(
                token_id=int(token_id),
                market_id=int(market_id),
            )
            moved = sweep_res.get("moved") if isinstance(sweep_res, dict) else None
            needs_direct_fallback = (not sweep_ok) or (
                isinstance(moved, list) and len(moved) == 0
            )
            if needs_direct_fallback:
                try:
                    scaled_amount = await self.unscaled_to_scaled_cash_wei(
                        int(token_id), int(amount_wei)
                    )
                    fallback_ok, fallback_res = await self.cash_transfer(
                        market_id=int(market_id),
                        amount_wei=int(scaled_amount),
                        is_deposit=False,
                    )
                    if fallback_ok:
                        sweep_ok, sweep_res = (
                            True,
                            {
                                "status": "fallback_direct_transfer",
                                "tx": fallback_res,
                            },
                        )
                    elif not sweep_ok:
                        sweep_res = {
                            "status": "warning",
                            "error": str(sweep_res),
                            "fallback_error": fallback_res,
                        }
                    else:
                        sweep_res = {
                            "status": "warning",
                            "moved": moved or [],
                            "fallback_error": fallback_res,
                        }
                except Exception as exc:  # noqa: BLE001
                    if not sweep_ok:
                        sweep_res = {
                            "status": "warning",
                            "error": str(sweep_res),
                            "fallback_error": str(exc),
                        }
                    else:
                        sweep_res = {
                            "status": "warning",
                            "moved": moved or [],
                            "fallback_error": str(exc),
                        }

            return True, {
                "status": "ok",
                "target_margin": target_margin,
                "approve": approve_res,
                "tx": tx_res,
                "sweep": sweep_res,
            }
        except Exception as e:
            logger.error(f"Failed to deposit collateral into Boros: {e}")
            return False, {"error": str(e)}

    async def deposit_to_cross_margin(
        self,
        collateral_address: str,
        amount_wei: int,
        *,
        token_id: int,
        market_id: int,
    ) -> tuple[bool, dict[str, Any]]:
        """Deposit collateral into Boros cross margin."""
        return await self.deposit_collateral(
            collateral_address=collateral_address,
            amount_wei=amount_wei,
            token_id=token_id,
            market_id=market_id,
            target_margin="cross",
        )

    async def deposit_to_isolated_margin(
        self,
        collateral_address: str,
        amount_wei: int,
        *,
        token_id: int,
        market_id: int,
    ) -> tuple[bool, dict[str, Any]]:
        """Deposit collateral into a market's isolated margin bucket."""
        return await self.deposit_collateral(
            collateral_address=collateral_address,
            amount_wei=amount_wei,
            token_id=token_id,
            market_id=market_id,
            target_margin="isolated",
        )

    async def withdraw_collateral(
        self,
        *,
        token_id: int,
        amount_native: int | None = None,
        amount_wei: int | None = None,
        account_id: int | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Withdraw collateral from Boros account.

        IMPORTANT: The amount must be in NATIVE token decimals, not 1e18!
        - For USDT (token_id=3): 6 decimals, so 1 USDT = 1_000_000
        - For other tokens: check their native decimals

        Args:
            token_id: Boros token ID.
            amount_native: Amount in native token decimals (e.g., 6 decimals for USDT).
            amount_wei: Backwards-compatible alias for amount_native (Boros APIs use
                "wei" naming even when values are native decimals).
            account_id: Account ID.

        Returns:
            Tuple of (success, transaction result).
        """
        # Backwards-compat: older callers/tests used amount_wei even though this is
        # native token decimals. Prefer amount_native going forward.
        if amount_native is None:
            if amount_wei is None:
                raise TypeError(
                    "withdraw_collateral requires amount_native (or amount_wei)"
                )
            amount_native = int(amount_wei)

        try:
            calldata = await self.boros_client.build_withdraw_calldata(
                token_id=token_id,
                amount_wei=amount_native,  # API expects native decimals despite param name
                user_address=self.wallet_address,
                account_id=account_id,
            )

            tx_ok, tx_res = await self._broadcast_calldata(calldata)
            if not tx_ok:
                return False, tx_res
            return True, tx_res
        except Exception as e:
            logger.error(f"Failed to withdraw collateral: {e}")
            return False, {"error": str(e)}

    async def cash_transfer(
        self,
        *,
        market_id: int,
        amount_wei: int,
        is_deposit: bool = False,
    ) -> tuple[bool, dict[str, Any]]:
        """Transfer cash between isolated and cross margin accounts.

        Semantics:
        - is_deposit=True: cross -> isolated
        - is_deposit=False: isolated -> cross

        Notes:
        - Boros uses 1e18 internal cash units for this call.
        """
        try:
            calldata = await self.boros_client.build_cash_transfer_calldata(
                market_id=market_id,
                amount_wei=amount_wei,
                is_deposit=is_deposit,
            )
            logger.debug(f"Boros cash_transfer calldata response: {calldata}")

            tx_ok, tx_res = await self._broadcast_calldata(calldata)
            logger.debug(f"Boros cash_transfer tx result: ok={tx_ok}, res={tx_res}")
            if not tx_ok:
                return False, tx_res
            direction = "cross -> isolated" if is_deposit else "isolated -> cross"
            logger.info(f"Boros cash_transfer succeeded ({direction})")
            return True, tx_res
        except Exception as e:
            logger.error(f"Failed to cash transfer: {e}")
            return False, {"error": str(e)}

    async def _get_amm_id_for_market(self, market_id: int) -> int | None:
        market_id = int(market_id)
        if cached := self._amm_id_by_market_cache.get(market_id):
            return cached

        for account in (self.wallet_address, None):
            ok, vaults = await self.get_vaults_summary(
                account=account,
                use_direct_lp_query=False,
            )
            if ok and isinstance(vaults, list):
                if cached := self._amm_id_by_market_cache.get(market_id):
                    return cached
        return None

    async def _get_vault_context_for_amm(self, amm_id: int) -> dict[str, Any] | None:
        amm_id = int(amm_id)
        if cached := self._vault_context_cache.get(amm_id):
            return dict(cached)

        for account in (self.wallet_address, None):
            ok, vaults = await self.get_vaults_summary(
                account=account,
                use_direct_lp_query=False,
            )
            if ok and isinstance(vaults, list):
                if cached := self._vault_context_cache.get(amm_id):
                    return dict(cached)
        return None

    async def deposit_to_vault_direct(
        self,
        *,
        amm_id: int,
        net_cash_in_wei: int,
        min_lp_out_wei: int | None = None,
        slippage_bps: int = 20,
        market_id: int | None = None,
        is_isolated_only: bool | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        if not self.wallet_address:
            return False, {"error": "wallet_address is required"}
        if not self.sign_callback:
            return False, {"error": "sign_callback is required"}

        net_cash = int(net_cash_in_wei)
        if net_cash <= 0:
            return False, {"error": "net_cash_in_wei must be positive"}

        if min_lp_out_wei is None or int(min_lp_out_wei) <= 0:
            min_lp_out_wei = int(net_cash * (1 - (int(slippage_bps) / 10_000)))

        desired_swap_side = self.SIDE_LONG
        desired_swap_rate = self.MAX_INT128
        context = None
        if market_id is None or is_isolated_only is None:
            context = await self._get_vault_context_for_amm(int(amm_id))
        resolved_market_id = int(market_id) if market_id is not None else None
        if context:
            if resolved_market_id is None:
                resolved_market_id = (
                    self._to_int(
                        context.get("market_id"),
                        default=0,
                    )
                    or None
                )
            if is_isolated_only is None:
                is_isolated_only = bool(context.get("is_isolated_only"))
        isolated_only = bool(is_isolated_only)
        last_error: str | None = None

        for enter_market in (True, False):
            try:
                tx = await encode_call(
                    target=BOROS_ROUTER,
                    abi=BOROS_ROUTER_VIEW_ABI,
                    fn_name="addLiquiditySingleCashToAmm",
                    args=[
                        (
                            not isolated_only,
                            int(amm_id),
                            bool(enter_market),
                            int(net_cash),
                            int(min_lp_out_wei),
                            int(desired_swap_side),
                            int(desired_swap_rate),
                        )
                    ],
                    from_address=self.wallet_address,
                    chain_id=self.chain_id,
                )
                tx_hash = await send_transaction(
                    tx, self.sign_callback, wait_for_receipt=True
                )
                self._invalidate_lp_cache()
                return True, {
                    "status": "ok",
                    "tx": {"tx_hash": tx_hash},
                    "amm_id": int(amm_id),
                    "market_id": int(resolved_market_id)
                    if resolved_market_id is not None
                    else None,
                    "net_cash_in_wei": int(net_cash),
                    "is_isolated_only": isolated_only,
                    "enter_market": bool(enter_market),
                }
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if enter_market:
                    continue
                return False, {"error": last_error}

        return False, {"error": last_error or "Failed to deposit to Boros vault"}

    async def withdraw_from_vault_direct(
        self,
        *,
        amm_id: int,
        lp_to_remove_wei: int,
        min_cash_out_wei: int = 0,
        market_id: int | None = None,
        is_isolated_only: bool | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        if not self.wallet_address:
            return False, {"error": "wallet_address is required"}
        if not self.sign_callback:
            return False, {"error": "sign_callback is required"}

        context = None
        if market_id is None or is_isolated_only is None:
            context = await self._get_vault_context_for_amm(int(amm_id))
        resolved_market_id = int(market_id) if market_id is not None else None
        if context:
            if resolved_market_id is None:
                resolved_market_id = (
                    self._to_int(
                        context.get("market_id"),
                        default=0,
                    )
                    or None
                )
            if is_isolated_only is None:
                is_isolated_only = bool(context.get("is_isolated_only"))
        isolated_only = bool(is_isolated_only)
        try:
            tx = await encode_call(
                target=BOROS_ROUTER,
                abi=BOROS_ROUTER_VIEW_ABI,
                fn_name="removeLiquiditySingleCashFromAmm",
                args=[
                    (
                        not isolated_only,
                        int(amm_id),
                        int(lp_to_remove_wei),
                        int(min_cash_out_wei),
                        int(self.SIDE_SHORT),
                        int(self.MIN_INT128),
                    )
                ],
                from_address=self.wallet_address,
                chain_id=self.chain_id,
            )
            tx_hash = await send_transaction(
                tx, self.sign_callback, wait_for_receipt=True
            )
            self._invalidate_lp_cache()
            return True, {
                "status": "ok",
                "tx": {"tx_hash": tx_hash},
                "amm_id": int(amm_id),
                "market_id": int(resolved_market_id)
                if resolved_market_id is not None
                else None,
                "lp_to_remove_wei": int(lp_to_remove_wei),
                "is_isolated_only": isolated_only,
            }
        except Exception as exc:  # noqa: BLE001
            return False, {"error": str(exc)}

    async def deposit_to_vault(
        self,
        *,
        market_id: int,
        net_cash_in_wei: int,
        min_lp_out_wei: int | None = None,
        slippage_bps: int = 20,
        simulate: bool = False,
    ) -> tuple[bool, dict[str, Any]]:
        if simulate:
            return False, {
                "error": "simulate=True is not supported for direct Boros vault ops"
            }

        effective_cash = int(net_cash_in_wei)
        amm_id: int | None = None
        match: BorosVault | None = None
        min_isolated_cash_wei = 0
        ok, vaults = await self.get_vaults_summary(
            account=self.wallet_address, use_direct_lp_query=False
        )
        if ok and isinstance(vaults, list):
            match = next(
                (vault for vault in vaults if int(vault.market_id) == int(market_id)),
                None,
            )
            if match:
                amm_id = int(match.amm_id)
                if match.is_isolated_only:
                    raw_token_id = self._to_int(
                        (match.raw or {}).get("collateralTokenId")
                        or (match.raw or {}).get("tokenId"),
                        default=0,
                    )
                    if raw_token_id > 0:
                        ok_fee, fee_data = await self.get_cash_fee_data(
                            token_id=int(raw_token_id)
                        )
                        if ok_fee and isinstance(fee_data, dict):
                            min_isolated_cash_wei = self._to_int(
                                fee_data.get("min_cash_isolated_wei"),
                                default=0,
                            )
                            if (
                                min_isolated_cash_wei > 0
                                and effective_cash < min_isolated_cash_wei
                            ):
                                min_isolated_cash = min_isolated_cash_wei / 1e18
                                return False, {
                                    "error": (
                                        "Isolated-only Boros vault requires at least "
                                        f"{min_isolated_cash:.6f} cash units"
                                    ),
                                    "market_id": int(market_id),
                                    "required_cash_wei": int(min_isolated_cash_wei),
                                    "requested_cash_wei": int(effective_cash),
                                }
                available = self._wei_amount_to_tokens(
                    ((match.raw or {}).get("user") or {}).get(
                        "availableBalanceToDeposit"
                    )
                )
                if available is not None and float(available) > 0:
                    available_wei = int(float(available) * 1e18)
                    if available_wei < effective_cash:
                        effective_cash = available_wei
                        if min_lp_out_wei is not None and int(net_cash_in_wei) > 0:
                            min_lp_out_wei = int(
                                int(min_lp_out_wei)
                                * effective_cash
                                / int(net_cash_in_wei)
                            )
                if (
                    match.is_isolated_only
                    and min_isolated_cash_wei > 0
                    and effective_cash < min_isolated_cash_wei
                ):
                    min_isolated_cash = min_isolated_cash_wei / 1e18
                    return False, {
                        "error": (
                            "Isolated-only Boros vault requires at least "
                            f"{min_isolated_cash:.6f} cash units"
                        ),
                        "market_id": int(market_id),
                        "required_cash_wei": int(min_isolated_cash_wei),
                        "requested_cash_wei": int(net_cash_in_wei),
                        "effective_cash_wei": int(effective_cash),
                    }

        if amm_id is None:
            amm_id = await self._get_amm_id_for_market(int(market_id))
        if amm_id is None:
            return False, {
                "error": f"Could not resolve amm_id for market_id={market_id}"
            }

        if effective_cash <= 0:
            return False, {"error": "No Boros cash available to deposit"}

        return await self.deposit_to_vault_direct(
            amm_id=int(amm_id),
            net_cash_in_wei=int(effective_cash),
            min_lp_out_wei=min_lp_out_wei,
            slippage_bps=int(slippage_bps),
            market_id=int(market_id),
            is_isolated_only=bool(match.is_isolated_only) if match else None,
        )

    async def withdraw_from_vault(
        self,
        *,
        market_id: int,
        lp_to_remove_wei: int,
        min_cash_out_wei: int = 0,
        simulate: bool = False,
    ) -> tuple[bool, dict[str, Any]]:
        if simulate:
            return False, {
                "error": "simulate=True is not supported for direct Boros vault ops"
            }

        amm_id = await self._get_amm_id_for_market(int(market_id))
        if amm_id is None:
            return False, {
                "error": f"Could not resolve amm_id for market_id={market_id}"
            }

        return await self.withdraw_from_vault_direct(
            amm_id=int(amm_id),
            lp_to_remove_wei=int(lp_to_remove_wei),
            min_cash_out_wei=int(min_cash_out_wei),
            market_id=int(market_id),
        )

    async def get_rewards(self) -> tuple[bool, dict[str, float] | str]:
        try:
            result = await self.boros_client.get_amm_rewards(
                user_address=self.wallet_address
            )
            return True, {
                "accrued_usd": float(result.get("accruedAmountInUsd", 0) or 0.0),
                "unclaimed_usd": float(result.get("unclaimedAmountInUsd", 0) or 0.0),
            }
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def get_claim_proof(self) -> tuple[bool, dict[str, Any] | str]:
        try:
            proof = await self.boros_client.get_amm_rewards_proof(
                user_address=self.wallet_address
            )
            if not proof or not proof.get("tokens"):
                return True, {}
            return True, proof
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def claim_rewards(self) -> tuple[bool, dict[str, Any]]:
        if not self.wallet_address:
            return False, {"error": "wallet_address is required"}
        if not self.sign_callback:
            return False, {"error": "sign_callback is required"}

        ok, proof = await self.get_claim_proof()
        if not ok:
            return False, {"error": proof}
        if not isinstance(proof, dict) or not proof.get("tokens"):
            return True, {"status": "no_rewards", "claimed": []}

        tx = await encode_call(
            target=BOROS_MERKLE_DISTRIBUTOR,
            abi=BOROS_MERKLE_DISTRIBUTOR_ABI,
            fn_name="claim",
            args=[
                to_checksum_address(self.wallet_address),
                [to_checksum_address(token) for token in proof["tokens"]],
                [int(amount) for amount in proof["accruedAmounts"]],
                proof["proofs"],
            ],
            from_address=self.wallet_address,
            chain_id=self.chain_id,
        )

        try:
            tx_hash = await send_transaction(
                tx, self.sign_callback, wait_for_receipt=True
            )
            return True, {
                "status": "claimed",
                "tx_hash": tx_hash,
                "tokens": proof["tokens"],
            }
        except Exception as exc:  # noqa: BLE001
            return False, {"error": str(exc)}

    async def bridge_hype_oft_hyperevm_to_arbitrum(
        self,
        *,
        amount_wei: int,
        max_value_wei: int | None = None,
        to_address: str | None = None,
        from_address: str | None = None,
        dst_eid: int = 30110,
        min_amount_wei: int = 0,
    ) -> tuple[bool, dict[str, Any]]:
        """Bridge native HYPE from HyperEVM -> Arbitrum via LayerZero OFT.

        Notes:
        - Uses HyperEVM chain id (999) for the transaction.
        - `amount_wei` is in 1e18 (native HYPE).
        - The OFT bridge requires `msg.value = amount + nativeFee`.
        - Amount must be rounded down to `decimalConversionRate()`.
        - If `max_value_wei` is provided, clamps amount so that (amount + fee) <= max_value_wei.
        """
        if amount_wei <= 0:
            return True, {"status": "no_op", "amount_wei": 0}

        if not self.sign_callback:
            return False, {"error": "sign_callback not configured"}

        sender = from_address or self.wallet_address
        recipient = to_address or self.wallet_address
        if not sender or not recipient:
            return False, {"error": "from_address/to_address not configured"}

        try:
            async with web3_from_chain_id(CHAIN_ID_HYPEREVM) as w3:
                contract = w3.eth.contract(
                    address=w3.to_checksum_address(HYPE_OFT_ADDRESS),
                    abi=HYPE_OFT_ABI,
                )

                conversion_rate = int(
                    await contract.functions.decimalConversionRate().call()
                )
                if conversion_rate > 0:
                    amount_wei = (int(amount_wei) // conversion_rate) * conversion_rate
                else:
                    amount_wei = int(amount_wei)

                if amount_wei <= 0:
                    return True, {"status": "no_op", "amount_wei": 0}

                to_bytes32 = self._pad_address_bytes32(recipient)

                def _send_params(amount_ld: int) -> tuple[Any, ...]:
                    return (
                        int(dst_eid),
                        to_bytes32,
                        int(amount_ld),
                        int(min_amount_wei),
                        b"",
                        b"",
                        b"",
                    )

                send_params = _send_params(int(amount_wei))
                fee = await contract.functions.quoteSend(send_params, False).call()
                native_fee_wei = int(fee[0])
                lz_token_fee_wei = int(fee[1])

                if max_value_wei is not None:
                    max_send_amount_wei = max(0, int(max_value_wei) - native_fee_wei)
                    if conversion_rate > 0:
                        max_send_amount_wei = (
                            max_send_amount_wei // conversion_rate
                        ) * conversion_rate
                    if amount_wei > max_send_amount_wei:
                        amount_wei = int(max_send_amount_wei)
                        if amount_wei <= 0:
                            return False, {
                                "error": "Insufficient balance to cover OFT fee",
                                "native_fee_wei": native_fee_wei,
                                "max_value_wei": int(max_value_wei),
                            }
                        send_params = _send_params(int(amount_wei))
                        fee = await contract.functions.quoteSend(
                            send_params, False
                        ).call()
                        native_fee_wei = int(fee[0])
                        lz_token_fee_wei = int(fee[1])

                total_value_wei = int(amount_wei) + int(native_fee_wei)
                if max_value_wei is not None and total_value_wei > int(max_value_wei):
                    return False, {
                        "error": "Insufficient balance after fee quote",
                        "amount_wei": int(amount_wei),
                        "native_fee_wei": int(native_fee_wei),
                        "total_value_wei": int(total_value_wei),
                        "max_value_wei": int(max_value_wei),
                    }

            tx = await encode_call(
                target=HYPE_OFT_ADDRESS,
                abi=HYPE_OFT_ABI,
                fn_name="send",
                args=[
                    send_params,
                    (int(native_fee_wei), int(lz_token_fee_wei)),
                    to_checksum_address(sender),
                ],
                from_address=sender,
                chain_id=CHAIN_ID_HYPEREVM,
                value=int(total_value_wei),
            )
            tx_hash = await send_transaction(
                tx, self.sign_callback, wait_for_receipt=True
            )
            return True, {
                "status": "ok",
                "tx_hash": tx_hash,
                "amount_wei": int(amount_wei),
                "native_fee_wei": int(native_fee_wei),
                "lz_token_fee_wei": int(lz_token_fee_wei),
                "total_value_wei": int(total_value_wei),
                "dst_eid": int(dst_eid),
                "to": recipient,
                "from": sender,
                "layerzeroscan": f"https://layerzeroscan.com/tx/{tx_hash}",
            }
        except Exception as exc:  # noqa: BLE001
            return False, {"error": str(exc)}

    async def bridge_hype_oft_arbitrum_to_hyperevm(
        self,
        *,
        amount_wei: int,
        max_fee_wei: int | None = None,
        to_address: str | None = None,
        from_address: str | None = None,
        dst_eid: int | None = None,
        min_amount_wei: int = 0,
    ) -> tuple[bool, dict[str, Any]]:
        """Bridge Arbitrum OFT HYPE (ERC20) -> HyperEVM native HYPE via LayerZero OFT.

        Notes:
        - Uses Arbitrum chain id (42161) for the transaction.
        - `amount_wei` is in 1e18 OFT units.
        - `msg.value` is the LayerZero native fee (ETH), NOT the amount.
        - Amount must be rounded down to `decimalConversionRate()`.
        """
        if amount_wei <= 0:
            return True, {"status": "no_op", "amount_wei": 0}

        if not self.sign_callback:
            return False, {"error": "sign_callback not configured"}

        sender = from_address or self.wallet_address
        recipient = to_address or self.wallet_address
        if not sender or not recipient:
            return False, {"error": "from_address/to_address not configured"}

        # Default destination for the Arbitrum -> HyperEVM bridge.
        if dst_eid is None:
            dst_eid = self.LZ_EID_HYPEREVM

        try:
            async with web3_from_chain_id(self.chain_id) as w3:
                contract = w3.eth.contract(
                    address=w3.to_checksum_address(HYPE_OFT_ADDRESS),
                    abi=HYPE_OFT_ABI,
                )

                conversion_rate = int(
                    await contract.functions.decimalConversionRate().call()
                )
                if conversion_rate > 0:
                    amount_wei = (int(amount_wei) // conversion_rate) * conversion_rate
                else:
                    amount_wei = int(amount_wei)

                if amount_wei <= 0:
                    return True, {"status": "no_op", "amount_wei": 0}

                to_bytes32 = self._pad_address_bytes32(recipient)

                def _send_params(amount_ld: int) -> tuple[Any, ...]:
                    # Match the `SendParam` tuple shape from `HYPE_OFT_ABI`.
                    return (
                        int(dst_eid),
                        to_bytes32,
                        int(amount_ld),
                        int(min_amount_wei),
                        b"",
                        b"",
                        b"",
                    )

                send_params = _send_params(int(amount_wei))
                fee = await contract.functions.quoteSend(send_params, False).call()
                native_fee_wei = int(fee[0])
                lz_token_fee_wei = int(fee[1])

                if max_fee_wei is not None and native_fee_wei > int(max_fee_wei):
                    return False, {
                        "error": "LayerZero fee exceeds max_fee_wei",
                        "native_fee_wei": native_fee_wei,
                        "max_fee_wei": int(max_fee_wei),
                    }

            tx = await encode_call(
                target=HYPE_OFT_ADDRESS,
                abi=HYPE_OFT_ABI,
                fn_name="send",
                args=[
                    send_params,
                    (int(native_fee_wei), int(lz_token_fee_wei)),
                    to_checksum_address(sender),
                ],
                from_address=sender,
                chain_id=int(self.chain_id),
                value=int(native_fee_wei),
            )
            tx_hash = await send_transaction(
                tx, self.sign_callback, wait_for_receipt=True
            )
            return True, {
                "status": "ok",
                "tx_hash": tx_hash,
                "amount_wei": int(amount_wei),
                "native_fee_wei": int(native_fee_wei),
                "lz_token_fee_wei": int(lz_token_fee_wei),
                "total_value_wei": int(native_fee_wei),
                "dst_eid": int(dst_eid),
                "to": recipient,
                "from": sender,
                "layerzeroscan": f"https://layerzeroscan.com/tx/{tx_hash}",
            }
        except Exception as exc:  # noqa: BLE001
            return False, {"error": str(exc)}

    async def close_positions_except(
        self,
        *,
        keep_market_id: int,
        token_id: int = 3,
        market_ids: list[int] | None = None,
        best_effort: bool = True,
    ) -> tuple[bool, dict[str, Any]]:
        """Close all Boros positions except `keep_market_id` (best-effort by default)."""
        if market_ids is None:
            ok_pos, positions = await self.get_active_positions()
            if not ok_pos:
                return False, {"error": f"Failed to get positions: {positions}"}
            market_ids = sorted(
                {
                    int(market_id)
                    for p in positions
                    if (market_id := p.get("marketId")) is not None
                }
            )

        results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for mid in market_ids:
            if int(mid) == int(keep_market_id):
                continue
            ok_close, res_close = await self.close_positions_market(
                int(mid), token_id=int(token_id)
            )
            entry = {"market_id": int(mid), "ok": bool(ok_close), "res": res_close}
            results.append(entry)
            if not ok_close:
                failures.append(entry)
                if not best_effort:
                    return False, {
                        "error": f"Failed to close position for market {mid}",
                        "results": results,
                    }

        return True, {"status": "ok", "results": results, "failures": failures}

    async def ensure_position_size_yu(
        self,
        *,
        market_id: int,
        token_id: int,
        target_size_yu: float,
        tif: str = "IOC",
        min_resize_excess_usd: float | None = None,
        yu_to_usd: float | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Ensure the Boros position size (YU) for a market.

        - Uses `place_rate_order(... side="short")` to increase the position.
        - Uses `close_positions_market(..., size_yu_wei=...)` to decrease it.
        - If `min_resize_excess_usd` is provided, uses `yu_to_usd` to skip small resizes.
        """
        ok_pos, positions = await self.get_active_positions(market_id=int(market_id))
        if not ok_pos:
            return False, {"error": f"Failed to get positions: {positions}"}

        if positions:
            current_size_yu = abs(float(positions[0].get("size", 0) or 0.0))
        else:
            current_size_yu = 0.0

        diff_yu = float(target_size_yu) - float(current_size_yu)
        diff_abs_yu = abs(diff_yu)

        if min_resize_excess_usd is not None:
            if yu_to_usd is None or yu_to_usd <= 0:
                return False, {
                    "error": "yu_to_usd required when min_resize_excess_usd is set",
                    "yu_to_usd": yu_to_usd,
                }
            diff_usd_equiv = diff_abs_yu * float(yu_to_usd)
            if diff_usd_equiv < float(min_resize_excess_usd):
                return True, {
                    "status": "ok",
                    "action": "no_op",
                    "market_id": int(market_id),
                    "token_id": int(token_id),
                    "current_size_yu": float(current_size_yu),
                    "target_size_yu": float(target_size_yu),
                    "diff_yu": float(diff_yu),
                }

        if diff_abs_yu < 1e-9:
            return True, {
                "status": "ok",
                "action": "no_op",
                "market_id": int(market_id),
                "token_id": int(token_id),
                "current_size_yu": float(current_size_yu),
                "target_size_yu": float(target_size_yu),
                "diff_yu": float(diff_yu),
            }

        size_yu_wei = int(diff_abs_yu * 1e18)

        if diff_yu > 0:
            ok_open, res_open = await self.place_rate_order(
                market_id=int(market_id),
                token_id=int(token_id),
                size_yu_wei=int(size_yu_wei),
                side="short",
                tif=tif,
            )
            if not ok_open:
                return False, {
                    "error": f"Failed to open/increase position: {res_open}",
                    "market_id": int(market_id),
                }
            return True, {
                "status": "ok",
                "action": "increase_short",
                "market_id": int(market_id),
                "token_id": int(token_id),
                "current_size_yu": float(current_size_yu),
                "target_size_yu": float(target_size_yu),
                "diff_yu": float(diff_yu),
                "tx": res_open,
            }

        ok_close, res_close = await self.close_positions_market(
            market_id=int(market_id),
            token_id=int(token_id),
            size_yu_wei=int(size_yu_wei),
        )
        if not ok_close:
            return False, {
                "error": f"Failed to close/decrease position: {res_close}",
                "market_id": int(market_id),
            }
        return True, {
            "status": "ok",
            "action": "decrease",
            "market_id": int(market_id),
            "token_id": int(token_id),
            "current_size_yu": float(current_size_yu),
            "target_size_yu": float(target_size_yu),
            "diff_yu": float(diff_yu),
            "tx": res_close,
        }

    async def place_rate_order(
        self,
        *,
        market_id: int,
        token_id: int,
        size_yu_wei: int,
        side: str,
        limit_tick: int | None = None,
        tif: str = "GTC",
        slippage: float = 0.05,
    ) -> tuple[bool, dict[str, Any]]:
        try:
            market_acc = await self._get_market_acc(token_id=token_id)

            if limit_tick is None:
                limit_tick = await self._pick_limit_tick_for_fill(
                    market_id=market_id, side=side, size_yu_wei=size_yu_wei
                )

            if limit_tick == 0:
                return False, {
                    "error": "Failed to determine limit_tick (orderbook may be empty or has no liquidity on this side)",
                    "market_id": market_id,
                    "side": side,
                }

            side_int = 0 if side.lower() in ("long", "buy") else 1
            tif_int = {"GTC": 0, "IOC": 1, "FOK": 2}.get(tif.upper(), 0)

            calldata = await self.boros_client.build_place_order_calldata(
                market_acc=market_acc,
                market_id=market_id,
                side=side_int,
                size_wei=size_yu_wei,
                limit_tick=limit_tick,
                tif=tif_int,
                slippage=slippage,
            )

            if not self.sign_callback:
                return False, {
                    "error": "sign_callback not configured",
                    "calldata": calldata,
                }

            tx_ok, tx_res = await self._broadcast_calldata(calldata)
            if not tx_ok:
                return False, tx_res
            return True, tx_res
        except Exception as e:
            logger.error(f"Failed to place rate order: {e}")
            return False, {"error": str(e)}

    async def close_positions_market(
        self,
        market_id: int,
        *,
        token_id: int = 3,
        size_yu_wei: int | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        try:
            success, positions = await self.get_active_positions(market_id=market_id)
            if not success or not positions:
                return True, {"status": "no_position"}

            position = positions[0]
            pos_side = int(position.get("side", 0))
            pos_size_wei = int(position.get("sizeWei") or 0)

            if pos_size_wei == 0:
                return True, {"status": "zero_size"}

            close_size = (
                abs(int(size_yu_wei)) if size_yu_wei is not None else abs(pos_size_wei)
            )
            close_side = 1 if pos_side == 0 else 0
            close_side_str = "short" if close_side == 1 else "long"

            market_acc = await self._get_market_acc(token_id=token_id)
            limit_tick = await self._pick_limit_tick_for_fill(
                market_id=market_id,
                side=close_side_str,
                size_yu_wei=close_size,
            )

            if limit_tick == 0:
                return False, {
                    "error": "Failed to determine limit_tick (orderbook may be empty or has no liquidity on this side)",
                    "market_id": market_id,
                    "side": close_side_str,
                }

            calldata = await self.boros_client.build_close_position_calldata(
                market_acc=market_acc,
                market_id=market_id,
                side=close_side,
                size_wei=close_size,
                limit_tick=limit_tick,
                tif=1,  # IOC
            )

            if not self.sign_callback:
                return False, {
                    "error": "sign_callback not configured",
                    "calldata": calldata,
                }

            tx_ok, tx_res = await self._broadcast_calldata(calldata)
            if not tx_ok:
                return False, tx_res
            return True, tx_res
        except Exception as e:
            logger.error(f"Failed to close positions: {e}")
            return False, {"error": str(e)}

    async def cancel_orders(
        self,
        *,
        market_id: int,
        token_id: int = 3,
        order_ids: list[str] | None = None,
        cancel_all: bool = False,
    ) -> tuple[bool, dict[str, Any]]:
        try:
            market_acc = await self._get_market_acc(token_id=token_id)

            calldata = await self.boros_client.build_cancel_order_calldata(
                market_acc=market_acc,
                market_id=market_id,
                order_ids=order_ids,
                cancel_all=cancel_all,
            )

            if not self.sign_callback:
                return False, {
                    "error": "sign_callback not configured",
                    "calldata": calldata,
                }

            tx_ok, tx_res = await self._broadcast_calldata(calldata)
            if not tx_ok:
                return False, tx_res
            return True, tx_res
        except Exception as e:
            logger.error(f"Failed to cancel orders: {e}")
            return False, {"error": str(e)}

    async def finalize_vault_withdrawal(
        self, *, token_id: int, root_address: str | None = None
    ) -> tuple[bool, dict[str, Any]]:
        """Finalize a previously requested MarketHub withdrawal.

        This transfers collateral that was previously requested for withdrawal
        to the root_address (defaults to the user's wallet address).

        Note: This calls the MarketHub contract directly as there's no API endpoint.

        Args:
            token_id: Boros token ID.
            root_address: Destination address (defaults to user_address).

        Returns:
            Tuple of (success, transaction result).
        """
        try:
            dest_address = root_address or self.wallet_address
            if not dest_address:
                return False, {"error": "No destination address configured"}

            if not self.sign_callback:
                return False, {"error": "sign_callback not configured"}

            # Encode finalizeVaultWithdrawal(address root, uint16 tokenId) directly
            # Function selector: keccak256("finalizeVaultWithdrawal(address,uint16)")[:4]
            selector = function_signature_to_4byte_selector(
                "finalizeVaultWithdrawal(address,uint16)"
            )
            params = encode(
                ["address", "uint16"], [to_checksum_address(dest_address), token_id]
            )
            data = "0x" + selector.hex() + params.hex()

            tx = {
                "chainId": self.chain_id,
                "from": to_checksum_address(self.wallet_address),
                "to": to_checksum_address(BOROS_MARKET_HUB),
                "data": data,
                "value": 0,
            }

            try:
                tx_hash = await send_transaction(
                    tx, self.sign_callback, wait_for_receipt=True
                )
                self._invalidate_lp_cache()
                return True, {"status": "ok", "tx": {"tx_hash": tx_hash}}
            except Exception as e:
                return False, {
                    "status": "error",
                    "error": str(e),
                    "tx": {"error": str(e)},
                }
        except Exception as e:
            logger.error(f"Failed to finalize vault withdrawal: {e}")
            return False, {"error": str(e)}

    # ------------------------------------------------------------------ #
    # Internal Helpers                                                     #
    # ------------------------------------------------------------------ #

    def _extract_symbol(self, market: dict[str, Any]) -> str:
        return extract_symbol(market)

    def _extract_underlying(self, market: dict[str, Any]) -> str:
        return extract_underlying(market)

    def _extract_collateral(self, market: dict[str, Any]) -> str:
        return extract_collateral(market)

    def _extract_maturity_ts(self, market: dict[str, Any]) -> int | None:
        return extract_maturity_ts(market)

    def _time_to_maturity_days(self, maturity_ts: int) -> float:
        return time_to_maturity_days(maturity_ts)

    def _parse_market_position(
        self,
        mkt_pos: dict[str, Any],
        token_id: int | None,
        is_cross: bool,
    ) -> dict[str, Any] | None:
        return parse_market_position(mkt_pos, token_id, is_cross=is_cross)

    async def _get_market_acc(self, token_id: int) -> str:
        """Get marketAcc from Boros API (collaterals/summary).

        Fetch from the Boros API rather than building locally to match backend expectations.

        Falls back to local construction if API doesn't return marketAcc.
        """
        if not self.wallet_address:
            raise ValueError("user_address not configured")

        # Try to get marketAcc from API (preferred)
        try:
            success, balances = await self.get_account_balances(token_id=token_id)
            if success and isinstance(balances, dict):
                # Look for marketAcc in the raw data
                raw = balances.get("raw", {})
                for coll in raw.get("collaterals", []):
                    if coll.get("tokenId") == token_id:
                        cross = coll.get("crossPosition") or {}
                        market_acc = cross.get("marketAcc")
                        if market_acc:
                            logger.debug(f"Got marketAcc from API: {market_acc}")
                            return market_acc
        except Exception as e:
            logger.debug(
                f"Failed to get marketAcc from API, falling back to local: {e}"
            )

        # Fallback: build locally
        # MarketAcc = address(20) | accountId(1) | tokenId(2) | marketId(3)
        addr = (
            self.wallet_address[2:]
            if self.wallet_address.startswith("0x")
            else self.wallet_address
        )
        account_hex = format(self.account_id, "02x")
        token_hex = format(token_id, "04x")
        market_hex = "ffffff"  # Cross margin marker

        market_acc = f"0x{addr.lower()}{account_hex}{token_hex}{market_hex}"
        logger.debug(f"Built marketAcc locally: {market_acc}")
        return market_acc

    async def _get_tick_step(self, market_id: int) -> int:
        try:
            success, mkt = await self.get_market(market_id)
            if not success:
                return 1
            step = (mkt.get("imData") or {}).get("tickStep") or mkt.get("tickStep") or 1
            return int(step)
        except Exception:
            return 1

    async def _pick_limit_tick_for_fill(
        self,
        market_id: int,
        side: str,
        size_yu_wei: int,
        max_ia_deviation: int = 50,
    ) -> int:
        """Find a limit tick deep enough in the orderbook to fill the order.

        IMPORTANT: The orderbook returns 'ia' (implied APR in bps, e.g., 116 = 1.16%)
        but Boros API expects 'limitTick' which uses TickMath (nonlinear).
        We must convert ia -> rate -> limitTick using the market's tickStep.

        For SHORT: walk down the long side (bids) until cumulative size >= order size
        For LONG: walk up the short side (asks) until cumulative size >= order size

        Args:
            market_id: Boros market ID
            side: "short"/"long"
            size_yu_wei: Order size in wei
            max_ia_deviation: Max allowed implied APR deviation from best (in bps)

        Returns:
            limitTick value for Boros API (NOT the same as ia!)
        """
        try:
            success, book = await self.get_orderbook(market_id, tick_size=0.0001)
            if not success:
                logger.warning(f"Failed to get orderbook for market {market_id}")
                return 0

            is_short = side.lower() in ("short", "sell")

            if is_short:
                # Selling YU -> hit the bids (long side)
                ia_list = (book.get("long") or {}).get("ia") or []
                sz_list = (book.get("long") or {}).get("sz") or []
            else:
                # Buying YU -> hit the asks (short side)
                ia_list = (book.get("short") or {}).get("ia") or []
                sz_list = (book.get("short") or {}).get("sz") or []

            if not ia_list or not sz_list:
                logger.warning(
                    f"Empty {'long' if is_short else 'short'} side in orderbook for market {market_id}"
                )
                return 0

            # Pair implied APR buckets with sizes and sort appropriately
            levels = list(zip(ia_list, sz_list, strict=False))
            if is_short:
                # For sells, go from highest ia (best bid) down
                levels.sort(key=lambda x: x[0], reverse=True)
            else:
                # For buys, go from lowest ia (best ask) up
                levels.sort(key=lambda x: x[0])

            best_ia = levels[0][0]
            cumulative = 0
            chosen_ia = best_ia

            for ia_bps, size_str in levels:
                # Check if we've deviated too far from best price
                if is_short:
                    if best_ia - ia_bps > max_ia_deviation:
                        break
                else:
                    if ia_bps - best_ia > max_ia_deviation:
                        break

                size_wei = int(size_str) if isinstance(size_str, str) else int(size_str)
                cumulative += size_wei
                chosen_ia = ia_bps

                if cumulative >= size_yu_wei:
                    break

            # Convert implied APR (bps) -> rate (decimal) -> limitTick
            tick_step = await self._get_tick_step(market_id)
            chosen_rate = (
                float(chosen_ia) / 10_000.0
            )  # ia is in bps (e.g., 116 = 1.16%)

            # For shorts, round_down to ensure we cross the spread and fill
            # For longs, round_up (round_down=False) to ensure we cross and fill
            limit_tick = self.tick_from_rate(
                chosen_rate, tick_step, round_down=is_short
            )

            logger.info(
                f"Boros tick selection: side={side}, chosen_ia={chosen_ia} bps ({chosen_rate * 100:.2f}%), "
                f"tick_step={tick_step}, limitTick={limit_tick}, "
                f"verify_rate={self.rate_from_tick(limit_tick, tick_step) * 100:.4f}%"
            )

            return limit_tick
        except Exception as e:
            logger.warning(f"Failed to pick limit tick: {e}")
            return 0
