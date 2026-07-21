from __future__ import annotations

import re

from eth_utils import is_address

from wayfinder_paths.core.constants.chains import CHAIN_CODE_TO_ID

# base58 (Bitcoin alphabet — no 0, O, I, l), 32–44 chars: a Solana mint/pubkey.
_SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def looks_like_evm_address(value: str | None) -> bool:
    if value is None:
        return False

    raw = str(value).strip()
    if not raw:
        return False

    # Avoid false positives for token slugs by requiring a 0x/0X prefix.
    if not raw.startswith(("0x", "0X")):
        return False

    return bool(is_address(raw))


def looks_like_solana_address(value: str | None) -> bool:
    if value is None:
        return False

    raw = str(value).strip()
    # 0x-prefixed strings are EVM; a bare base58 string of mint length is SVM.
    if not raw or raw.startswith(("0x", "0X")):
        return False

    return bool(_SOLANA_ADDRESS_RE.match(raw))


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

    if looks_like_evm_address(right) or looks_like_solana_address(right):
        chain_id = _parse_chain_part(left)
        return (int(chain_id), right) if chain_id is not None else (None, None)

    if looks_like_evm_address(left) or looks_like_solana_address(left):
        chain_id = _parse_chain_part(right)
        return (int(chain_id), left) if chain_id is not None else (None, None)

    return None, None
