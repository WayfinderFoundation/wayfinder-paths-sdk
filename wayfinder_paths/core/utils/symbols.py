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
    if symbol is None:
        return ""
    normalized = unicodedata.normalize("NFKD", symbol).translate(
        SYMBOL_TRANSLATION_TABLE
    )
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    filtered = "".join(ch for ch in ascii_only if ch.isalnum())
    if filtered:
        return filtered.lower()
    return symbol.lower()


def is_stable_symbol(symbol: str | None) -> bool:
    if not symbol:
        return False
    upper = symbol.upper()
    return any(keyword in upper for keyword in STABLE_SYMBOL_KEYWORDS)
