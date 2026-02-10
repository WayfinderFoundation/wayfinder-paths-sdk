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
        "error": {"code": str(code), "message": str(message), "details": details},
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


def sha256_json(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
