from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from wayfinder_paths.core.utils.wallets import (
    _build_sign_hash_callback,
    _build_signing_callback,
    _build_typed_data_callback,
    find_wallet_by_label,
    get_local_sign_callback,
    get_local_sign_hash_callback,
    get_local_sign_typed_data_callback,
)

SignTx = Callable[[dict[str, Any]], Awaitable[bytes]]
SignHash = Callable[[str], Awaitable[str]]
SignTypedData = Callable[[str | dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class SigningCallbacks:
    address: str
    sign: SignTx | None = None
    sign_typed_data: SignTypedData | None = None
    sign_hash: SignHash | None = None


async def build_signing_callbacks(label: str) -> SigningCallbacks:
    wallet = await find_wallet_by_label(label)
    if not wallet:
        raise ValueError(f"Wallet '{label}' not found.")
    sign, address = _build_signing_callback(wallet, label)
    sign_hash, _ = _build_sign_hash_callback(wallet, label)
    sign_typed_data, _ = _build_typed_data_callback(wallet, label)
    return SigningCallbacks(
        address=address,
        sign=sign,
        sign_hash=sign_hash,
        sign_typed_data=sign_typed_data,
    )


def signing_from_private_key(private_key: str, address: str) -> SigningCallbacks:
    return SigningCallbacks(
        address=address,
        sign=get_local_sign_callback(private_key),
        sign_hash=get_local_sign_hash_callback(private_key),
        sign_typed_data=get_local_sign_typed_data_callback(private_key),
    )
