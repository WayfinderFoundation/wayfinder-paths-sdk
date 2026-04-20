from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock

from wayfinder_paths.core.utils.signing import SigningCallbacks

FAKE_ADDR = "0x1234567890123456789012345678901234567890"


def fake_signing(
    *,
    sign: Callable[[dict[str, Any]], Awaitable[bytes]] | None = None,
    sign_typed_data: Callable[..., Awaitable[str]] | None = None,
    sign_hash: Callable[[str], Awaitable[str]] | None = None,
    address: str = FAKE_ADDR,
) -> SigningCallbacks:
    return SigningCallbacks(
        address=address,
        sign=sign if sign is not None else AsyncMock(return_value=b"\x00" * 32),
        sign_typed_data=(
            sign_typed_data
            if sign_typed_data is not None
            else AsyncMock(return_value="0x" + "00" * 65)
        ),
        sign_hash=(
            sign_hash
            if sign_hash is not None
            else AsyncMock(return_value="0x" + "00" * 65)
        ),
    )
