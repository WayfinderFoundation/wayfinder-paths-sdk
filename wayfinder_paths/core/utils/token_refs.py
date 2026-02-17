from __future__ import annotations

from eth_utils import is_address

from wayfinder_paths.core.constants.chains import CHAIN_CODE_TO_ID


def looks_like_evm_address(value: str | None) -> bool:
    if value is None:
        return False

    raw = value.strip()
    if not raw:
        return False

    if not raw.startswith(("0x", "0X")):
        return False

    return bool(is_address(raw))


def _parse_chain_part(value: str) -> int | None:
    raw = value.strip().lower()
    if not raw:
        return None
    if raw.isdigit():
        parsed = int(raw)
        return parsed if parsed > 0 else None
    return CHAIN_CODE_TO_ID.get(raw)


def parse_token_id_to_chain_and_address(
    token_id: str | None,
) -> tuple[int | None, str | None]:
    if token_id is None:
        return None, None

    raw = token_id.strip()
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
        return (chain_id, right) if chain_id is not None else (None, None)

    if looks_like_evm_address(left):
        chain_id = _parse_chain_part(right)
        return (chain_id, left) if chain_id is not None else (None, None)

    return None, None
