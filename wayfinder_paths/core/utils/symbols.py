from __future__ import annotations

import unicodedata

SYMBOL_TRANSLATION_TABLE = str.maketrans(
    {
        "₮": "T",
        "₿": "B",
        "Ξ": "X",
    }
)

STABLE_SYMBOL_KEYWORDS = {
    "USD",
    "USDC",
    "USDT",
    "USDP",
    "USDD",
    "USDS",
    "DAI",
    "USKB",
    "USDE",
    "USDH",
    "USDL",
    "USDR",
    "USDX",
    "SUSD",
    "LUSD",
    "GUSD",
    "TUSD",
    "USR",
    "USDHL",
}


def normalize_symbol(symbol: str | None) -> str:
    """Normalize a token symbol for stable comparisons / keys.

    - Unicode NFKD normalization
    - Common crypto symbol translation (₮/₿/Ξ)
    - ASCII-only
    - Keep only alphanumerics
    - Lowercase
    """
    if symbol is None:
        return ""
    normalized = unicodedata.normalize("NFKD", str(symbol)).translate(
        SYMBOL_TRANSLATION_TABLE
    )
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    filtered = "".join(ch for ch in ascii_only if ch.isalnum())
    if filtered:
        return filtered.lower()
    return str(symbol).lower()


def is_stable_symbol(symbol: str | None) -> bool:
    if not symbol:
        return False
    upper = str(symbol).upper()
    return any(keyword in upper for keyword in STABLE_SYMBOL_KEYWORDS)
