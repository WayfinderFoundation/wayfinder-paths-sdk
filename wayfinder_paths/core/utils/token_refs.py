from __future__ import annotations

import re

from wayfinder_paths.core.constants.chains import CHAIN_CODE_TO_ID

_EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def looks_like_evm_address(value: str | None) -> bool:
    if value is None:
        return False
    return bool(_EVM_ADDRESS_RE.match(str(value).strip()))


def _parse_chain_part(value: str) -> int | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    if raw.isdigit():
        try:
            parsed = int(raw)
        except Exception:
            return None
        return parsed if parsed > 0 else None
    return CHAIN_CODE_TO_ID.get(raw)


def parse_token_id_to_chain_and_address(
    token_id: str | None,
) -> tuple[int | None, str | None]:
    """Parse token_id strings like `base_0xabc...` or `0xabc..._base`.

    Returns (chain_id, token_address) when it can be derived locally, otherwise (None, None).
    """
    if token_id is None:
        return None, None

    raw = str(token_id).strip()
    if not raw:
        return None, None

    if "_" not in raw:
        return None, None

    parts = raw.split("_")
    if len(parts) != 2:
        return None, None

    left, right = parts[0].strip(), parts[1].strip()

    if looks_like_evm_address(right):
        chain_id = _parse_chain_part(left)
        return (int(chain_id), right) if chain_id is not None else (None, None)

    if looks_like_evm_address(left):
        chain_id = _parse_chain_part(right)
        return (int(chain_id), left) if chain_id is not None else (None, None)

    return None, None
