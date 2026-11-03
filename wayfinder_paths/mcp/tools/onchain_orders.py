from __future__ import annotations

from decimal import Decimal
from typing import Any

from wayfinder_paths.adapters.definitive_adapter.adapter import (
    DEFINITIVE_CHAINS,
    ORDER_TYPES,
    DefinitiveAdapter,
    build_flash_request,
)
from wayfinder_paths.core.constants.chains import CHAIN_ID_SOLANA
from wayfinder_paths.core.utils.token_resolver import TokenResolver
from wayfinder_paths.core.utils.wallets import (
    find_wallet_leg_for_chain,
    get_wallet_sign_hash_callback,
    get_wallet_signing_callback_for_chain,
    get_wallet_svm_message_callback,
    is_solana_enabled,
)
from wayfinder_paths.mcp.tools.execute import _annotate_profile
from wayfinder_paths.mcp.utils import (
    catch_errors,
    err,
    ok,
    parse_amount_to_raw,
    throw_if_empty_str,
)


async def _adapter(wallet_label: str, chain_id: int) -> tuple[DefinitiveAdapter, str]:
    """The adapter for the ring leg that funds orders on `chain_id`."""
    sign_callback, sender = await get_wallet_signing_callback_for_chain(
        wallet_label, chain_id
    )
    if int(chain_id) == CHAIN_ID_SOLANA:
        sign_message, message_address = await get_wallet_svm_message_callback(
            wallet_label
        )
        if message_address != sender:
            raise ValueError(
                "Token orders need one Solana wallet for signing and funding"
            )
        return (
            DefinitiveAdapter(
                sign_callback=sign_callback,
                svm_message_callback=sign_message,
                wallet_address=sender,
            ),
            sender,
        )
    sign_hash, hash_address = await get_wallet_sign_hash_callback(wallet_label)
    if hash_address.lower() != sender.lower():
        raise ValueError("Token orders need one EVM wallet for signing and funding")
    return (
        DefinitiveAdapter(
            sign_callback=sign_callback,
            sign_hash_callback=sign_hash,
            wallet_address=sender,
        ),
        sender,
    )


async def _legs(wallet_label: str) -> list[tuple[DefinitiveAdapter, str]]:
    """The wallet ring's EVM leg, plus its Solana leg when Solana is on."""
    legs = [await _adapter(wallet_label, 8453)]
    if await is_solana_enabled() and await find_wallet_leg_for_chain(
        wallet_label, CHAIN_ID_SOLANA
    ):
        legs.append(await _adapter(wallet_label, CHAIN_ID_SOLANA))
    return legs


@catch_errors
async def onchain_place_order(
    *,
    wallet_label: str,
    order_type: str,
    from_token: str,
    to_token: str,
    amount: str,
    limit_price: float | None = None,
    trigger_price: float | None = None,
    take_profit_price: float | None = None,
    stop_loss_price: float | None = None,
    trail_percent: float | None = None,
    activation_price: float | None = None,
) -> dict[str, Any]:
    """Place a resting onchain token order (Definitive Flash): limit, stop-loss,
    take-profit, breakout buy, trailing stop, or TP + SL bracket.

    Non-custodial: `from_token` stays in the wallet until the order fills (a
    bounded approval, or on Solana a delegate, is set up first if needed).
    Moving the tokens out cancels the order for good. Same chain only: EVM
    ERC-20s (wrap native gas tokens first) or Solana SPL tokens, where native
    SOL is spent via wSOL and bought SOL arrives as wSOL. Prices are the USD
    price of the token being bought (buy types) or sold (sell types). A
    trigger already crossed when placed closes the order immediately instead
    of executing, so check the current price first.

    Args:
        wallet_label: Wallet that holds `from_token` and signs.
        order_type: `limit_buy` (buy `to_token` at or below `limit_price`),
            `limit_sell` (sell `from_token` at or above `limit_price`),
            `stop_loss` (sell when price falls to `stop_loss_price`),
            `take_profit` (sell when price rises to `take_profit_price`),
            `breakout_buy` (buy when price rises to `trigger_price`),
            `trailing_stop` (sell once price pulls back `trail_percent` from
            its high, optionally only after `activation_price` trades), or
            `bracket` (sell at `take_profit_price` or `stop_loss_price`,
            whichever comes first).
        from_token: Token spent (buy types) or sold (sell types).
        to_token: Token received. Same chain as `from_token`.
        amount: Human units of `from_token`, e.g. "25.0".
        trail_percent: Percent pullback, 0.5–50.

    Returns:
        `{status: "placed"|"failed", order_id, approval_tx_hash |
        setup_tx_signature, request}`.
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    if order_type not in ORDER_TYPES:
        return err(
            "invalid_request", f"order_type must be one of {sorted(ORDER_TYPES)}"
        )
    try:
        from_meta = await TokenResolver.resolve_token_meta(from_token)
        to_meta = await TokenResolver.resolve_token_meta(to_token)
    except Exception as exc:  # noqa: BLE001
        return err("token_error", str(exc))
    chain_id = int(from_meta.get("chain_id") or 0)
    if chain_id != int(to_meta.get("chain_id") or -1):
        return err("invalid_request", "from_token and to_token must be on one chain")
    if chain_id not in DEFINITIVE_CHAINS:
        return err(
            "invalid_request", f"Token orders aren't supported on chain {chain_id}"
        )
    if chain_id == CHAIN_ID_SOLANA and not await is_solana_enabled():
        return err("solana_disabled", "Solana is not enabled for this account.")

    decimals = int(from_meta["decimals"])
    spent_raw = parse_amount_to_raw(amount, decimals)
    request = build_flash_request(
        order_type,
        chain_id=chain_id,
        from_token=str(from_meta["address"]),
        to_token=str(to_meta["address"]),
        # qty from the floored base units, so the signed amount can't exceed it
        amount=str(Decimal(spent_raw).scaleb(-decimals)),
        limit_price=limit_price,
        trigger_price=trigger_price,
        take_profit_price=take_profit_price,
        stop_loss_price=stop_loss_price,
        trail_percent=trail_percent,
        activation_price=activation_price,
    )

    adapter, sender = await _adapter(wallet_label, chain_id)
    placed, res = await adapter.place_order(chain_id, request, spent_raw)
    status = "placed" if placed else "failed"
    _annotate_profile(
        address=sender,
        label=wallet_label,
        protocol="definitive",
        action=f"place_{order_type}",
        tool="onchain_place_order",
        status=status,
        chain_id=chain_id,
        details={"order_id": res.get("order_id"), "request": request},
    )
    return ok({"status": status, "address": sender, **res, "request": request})


@catch_errors
async def onchain_cancel_order(*, wallet_label: str, order_id: str) -> dict[str, Any]:
    """Cancel a resting Definitive token order by id (the wallet signs the
    cancel; nothing moves). An order that filled first is reported as filled.
    Cancelling doesn't revoke the token approval or Solana delegate.
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    order_id = throw_if_empty_str("order_id is required", order_id)
    # Try each leg: an order belongs to the leg (EVM or Solana) that funds it.
    failure: dict[str, Any] = {}
    for adapter, sender in await _legs(wallet_label):
        cancelled, res = await adapter.cancel_order(order_id)
        if cancelled:
            return ok({"status": "cancelled", "address": sender, **res})
        # Keep the leg's real error over the other leg's "not found".
        if not failure or "not found" in str(failure.get("error", "")).lower():
            failure = {"address": sender, **res}
    return ok({"status": "failed", **failure})


@catch_errors
async def onchain_list_orders(
    *, wallet_label: str, include_closed: bool = False
) -> dict[str, Any]:
    """List the wallet's Definitive token orders, EVM and Solana (open only
    by default).

    Each order has `orderType`, `side`, `status`, `qty` (in the spent token),
    `targetAsset`/`contraAsset`, prices (`limitNotionalPrice`, `trigger`,
    `brackets`), `trailingStop` (`activated`, `watermarkPrice`,
    `effectiveTriggerPrice`), and `closeReason` once closed (e.g.
    `REASON_INSUFFICIENT_FUNDER_BALANCE` when tokens left the wallet).
    """
    wallet_label = throw_if_empty_str("wallet_label is required", wallet_label)
    orders: list[Any] = []
    addresses: list[str] = []
    for adapter, sender in await _legs(wallet_label):
        listed, found = await adapter.list_orders(include_closed=include_closed)
        if not listed:
            return err("definitive_error", str(found))
        orders.extend(found)
        addresses.append(sender)
    return ok({"addresses": addresses, "orders": orders})
