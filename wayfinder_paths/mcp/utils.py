from __future__ import annotations

import hashlib
import json
from decimal import ROUND_DOWN, Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.core.config import CONFIG

getcontext().prec = 78


def ok(result: Any) -> dict[str, Any]:
    return {"ok": True, "result": result}


def err(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"code": code, "message": message, "details": details},
    }


def repo_root() -> Path:
    cur = Path(__file__).resolve()
    for parent in [cur, *cur.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    return data if isinstance(data, dict) else {}


def read_text_excerpt(path: Path, *, max_chars: int = 1200) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def load_wallets() -> list[dict[str, Any]]:
    if isinstance(CONFIG.get("wallets"), list):
        return [w for w in CONFIG["wallets"] if isinstance(w, dict)]
    return []


def find_wallet_by_label(label: str) -> dict[str, Any] | None:
    want = str(label).strip()
    if not want:
        return None
    for w in load_wallets():
        if str(w.get("label", "")).strip() == want:
            return w
    return None


def normalize_address(addr: str | None) -> str | None:
    if not addr:
        return None
    a = str(addr).strip()
    return a if a else None


def resolve_wallet_address(
    *, wallet_label: str | None = None, wallet_address: str | None = None
) -> tuple[str | None, str | None]:
    """Return ``(normalized_address, label_used)`` from a label or raw address."""
    waddr = normalize_address(wallet_address)
    if waddr:
        return waddr, None

    want = (wallet_label or "").strip()
    if not want:
        return None, None

    w = find_wallet_by_label(want)
    if not w:
        return None, None

    return normalize_address(w.get("address")), want


def parse_amount_to_raw(amount: str, decimals: int) -> int:
    try:
        d = Decimal(str(amount).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid amount: {amount}") from exc
    if d <= 0:
        raise ValueError("Amount must be positive")
    scale = Decimal(10) ** int(decimals)
    raw = (d * scale).to_integral_value(rounding=ROUND_DOWN)
    if raw <= 0:
        raise ValueError("Amount is too small after decimal scaling")
    return int(raw)


def extract_wallet_credentials(
    wallet_label: str,
) -> tuple[str, str, str, None] | tuple[None, None, None, dict[str, Any]]:
    """Look up a wallet by label and return (address, private_key, label, None) on
    success, or (None, None, None, error_response) on failure."""
    want = (wallet_label or "").strip()
    if not want:
        return None, None, None, err("invalid_request", "wallet_label is required")

    w = find_wallet_by_label(want)
    if not w:
        return None, None, None, err("not_found", f"Unknown wallet_label: {want}")

    sender = normalize_address(w.get("address"))
    pk = w.get("private_key") or w.get("private_key_hex")
    if not sender or not pk:
        return (
            None,
            None,
            None,
            err(
                "invalid_wallet",
                "Wallet must include address and private_key_hex in config.json (local dev only)",
                {"wallet_label": want},
            ),
        )

    return sender, pk, want, None


def validate_positive_float(
    value: Any, field_name: str
) -> tuple[float, None] | tuple[None, dict[str, Any]]:
    """Parse and validate a positive float, returning (value, None) or (None, error_response)."""
    if value is None:
        return None, err("invalid_request", f"{field_name} is required")
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None, err("invalid_request", f"{field_name} must be a number")
    if f <= 0:
        return None, err("invalid_request", f"{field_name} must be positive")
    return f, None


def validate_positive_int(
    value: Any, field_name: str
) -> tuple[int, None] | tuple[None, dict[str, Any]]:
    """Parse and validate a positive int, returning (value, None) or (None, error_response)."""
    if value is None:
        return None, err("invalid_request", f"{field_name} is required")
    try:
        i = int(value)
    except (TypeError, ValueError):
        return None, err("invalid_request", f"{field_name} must be an int")
    if i <= 0:
        return None, err("invalid_request", f"{field_name} must be positive")
    return i, None


def sha256_json(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
