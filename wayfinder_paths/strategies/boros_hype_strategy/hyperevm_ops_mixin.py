"""
HyperEVM / spot-leg operations for BorosHypeStrategy.

Kept as a mixin so the main strategy file stays readable without changing behavior.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from loguru import logger

from wayfinder_paths.core.utils.transaction import encode_call, send_transaction

from .constants import (
    HYPE_NATIVE,
    HYPEREVM_CHAIN_ID,
    KHYPE_LST,
    LOOPED_HYPE,
    MIN_HYPE_GAS,
    WHYPE_ABI,
    WHYPE_ADDRESS,
)
from .types import Inventory

if TYPE_CHECKING:
    from .strategy import BorosHypeStrategy


class BorosHypeHyperEvmOpsMixin:
    async def _ensure_gas_on_hyperevm(
        self: BorosHypeStrategy, params: dict[str, Any], inventory: Inventory
    ) -> tuple[bool, str]:
        min_hype = float(params.get("min_hype") or MIN_HYPE_GAS)
        need = max(0.0, min_hype - inventory.hype_hyperevm_balance)
        if need <= 0.0:
            return True, "HyperEVM gas already sufficient"

        # If we have WHYPE on HyperEVM, unwrap a small amount to native HYPE for gas.
        if float(inventory.whype_balance or 0.0) > 0.0:
            strategy_wallet = self._config.get("strategy_wallet", {})
            address = strategy_wallet.get("address")
            if address and self._sign_callback:
                unwrap_hype = min(
                    float(inventory.whype_balance or 0.0), max(0.01, need + 0.002)
                )
                unwrap_wei = int(unwrap_hype * 1e18)
                if unwrap_wei > 0:
                    logger.info(
                        f"Unwrapping {unwrap_hype:.6f} WHYPE → native HYPE for gas"
                    )
                    tx = await encode_call(
                        target=WHYPE_ADDRESS,
                        abi=WHYPE_ABI,
                        fn_name="withdraw",
                        args=[int(unwrap_wei)],
                        from_address=address,
                        chain_id=HYPEREVM_CHAIN_ID,
                    )
                    tx_hash = await send_transaction(
                        tx, self._sign_callback, wait_for_receipt=True
                    )
                    await asyncio.sleep(2)
                    return (
                        True,
                        f"Unwrapped {unwrap_hype:.6f} WHYPE for gas (tx={tx_hash})",
                    )

        # Best-effort: if HYPE exists on HL spot, bridge it over.
        if inventory.hl_spot_hype > max(0.1, need + 0.001):
            return await self._transfer_hl_spot_to_hyperevm(
                {"hype_amount": max(0.1, need)}, inventory
            )

        # Otherwise, we expect the upcoming BRIDGE_TO_HYPEREVM routine to bring HYPE.
        return True, "HyperEVM gas will be provisioned during routing"

    async def _ensure_gas_on_arbitrum(
        self: BorosHypeStrategy, params: dict[str, Any], inventory: Inventory
    ) -> tuple[bool, str]:
        # TODO: Implement - bridge ETH or use gas station
        return True, "Arbitrum gas routing not yet implemented"

    async def _swap_hype_to_lst(
        self: BorosHypeStrategy, params: dict[str, Any], inventory: Inventory
    ) -> tuple[bool, str]:
        hype_amount = params.get("hype_amount", 0.0)

        if hype_amount <= 0:
            return True, "No HYPE to swap"

        ok, msg = self._require_adapters("brap_adapter")
        if not ok:
            return False, msg

        ok_addr, wallet_address = self._require_strategy_wallet_address()
        if not ok_addr:
            return False, wallet_address

        # Normalize split fractions.
        khype_fraction = max(0.0, float(self.hedge_cfg.khype_fraction))
        looped_fraction = max(0.0, float(self.hedge_cfg.looped_hype_fraction))
        total_fraction = khype_fraction + looped_fraction
        if total_fraction <= 0:
            khype_fraction = 0.5
            looped_fraction = 0.5
            total_fraction = 1.0

        khype_share = khype_fraction / total_fraction

        # HYPE native has 18 decimals.
        hype_amount_wei = int(float(hype_amount) * 1e18)
        khype_amount_wei = int(hype_amount_wei * khype_share)
        looped_amount_wei = max(0, hype_amount_wei - khype_amount_wei)

        # If one side is dust, allocate to the other.
        min_swap_wei = int(0.02 * 1e18)
        if 0 < khype_amount_wei < min_swap_wei:
            looped_amount_wei += khype_amount_wei
            khype_amount_wei = 0
        if 0 < looped_amount_wei < min_swap_wei:
            khype_amount_wei += looped_amount_wei
            looped_amount_wei = 0

        results: list[str] = []

        if khype_amount_wei > 0:
            ok, res = await self.brap_adapter.swap_from_token_ids(
                from_token_id=HYPE_NATIVE,
                to_token_id=KHYPE_LST,
                from_address=wallet_address,
                amount=str(khype_amount_wei),
                slippage=0.01,
                strategy_name="boros_hype_strategy",
            )
            if not ok:
                return False, f"Swap HYPE→kHYPE failed: {res}"
            results.append(f"kHYPE({khype_amount_wei / 1e18:.4f} HYPE)")

        if looped_amount_wei > 0:
            ok, res = await self.brap_adapter.swap_from_token_ids(
                from_token_id=HYPE_NATIVE,
                to_token_id=LOOPED_HYPE,
                from_address=wallet_address,
                amount=str(looped_amount_wei),
                slippage=0.01,
                strategy_name="boros_hype_strategy",
            )
            if not ok:
                return False, f"Swap HYPE→looped HYPE failed: {res}"
            results.append(f"looped({looped_amount_wei / 1e18:.4f} HYPE)")

        if not results:
            return True, "No non-dust HYPE amount to swap"

        return True, f"Swapped HYPE → {', '.join(results)}"
