from typing import Any

from hyperliquid.info import Info


def get_size_decimals_for_hypecore_asset(info: Info, asset_id: int) -> int:
    return info.asset_to_sz_decimals[asset_id]


def get_price_decimals_for_hypecore_asset(info: Info, asset_id: int) -> int:
    is_spot = asset_id >= 10_000
    decimals = (6 if not is_spot else 8) - get_size_decimals_for_hypecore_asset(
        info, asset_id
    )
    return decimals


def sig_hex_to_hl_signature(sig_hex: str) -> dict[str, Any]:
    """Convert a 65-byte hex signature into Hyperliquid {r,s,v}."""
    if not isinstance(sig_hex, str) or not sig_hex.startswith("0x"):
        raise ValueError("Expected hex signature string starting with 0x")
    raw = bytes.fromhex(sig_hex[2:])
    if len(raw) != 65:
        raise ValueError(f"Expected 65-byte signature, got {len(raw)} bytes")

    r = raw[0:32]
    s = raw[32:64]
    v = raw[64]
    if v < 27:
        v += 27

    return {"r": f"0x{r.hex()}", "s": f"0x{s.hex()}", "v": int(v)}
