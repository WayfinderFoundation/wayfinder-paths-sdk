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


def resolve_path_inside_repo(
    path_raw: str | Path,
    *,
    field_name: str,
    not_found_message: str = "File not found",
) -> tuple[Path, str] | dict[str, Any]:
    raw = str(path_raw).strip()
    if not raw:
        return err("invalid_request", f"{field_name} is required")

    root = repo_root()
    root_resolved = root.resolve(strict=False)

    p = Path(raw)
    if not p.is_absolute():
        p = root / p
    resolved = p.resolve(strict=False)

    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return err(
            "invalid_request",
            f"{field_name} must be inside the repository",
            {"repo_root": str(root_resolved), field_name: str(resolved)},
        )

    if not resolved.exists():
        return err("not_found", not_found_message, {field_name: str(resolved)})

    display_path = str(resolved)
    try:
        display_path = str(resolved.relative_to(root_resolved))
    except ValueError:
        pass

    return resolved, display_path


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


def sha256_json(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sanitize_for_json(obj: Any) -> Any:
    """Recursively convert common web3 types into JSON-serializable forms."""
    if hasattr(obj, "hex") and callable(obj.hex):
        try:
            return obj.hex()
        except Exception:
            pass
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_for_json(v) for v in obj]
    return obj


def abi_function_signature(fn_abi: dict[str, Any]) -> str:
    name = str(fn_abi.get("name") or "").strip()
    inputs = fn_abi.get("inputs") if isinstance(fn_abi.get("inputs"), list) else []
    types = [str(i.get("type") or "").strip() for i in inputs if isinstance(i, dict)]
    return f"{name}({','.join(types)})"


def summarize_abi(abi: list[dict[str, Any]]) -> list[str]:
    entries: list[str] = []

    for item in abi:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").strip()
        name = str(item.get("name") or "").strip()

        if kind == "function":
            sig = abi_function_signature(item)
            outputs = (
                item.get("outputs") if isinstance(item.get("outputs"), list) else []
            )
            out_types = [
                str(o.get("type") or "?").strip()
                for o in outputs
                if isinstance(o, dict)
            ]
            out_part = f" -> ({','.join(out_types)})" if out_types else ""
            mut = str(item.get("stateMutability") or "").strip()
            mut_part = f" [{mut}]" if mut else ""
            entries.append(f"{sig}{out_part}{mut_part}")
            continue

        inputs = item.get("inputs") if isinstance(item.get("inputs"), list) else []
        input_types = ",".join(
            str(i.get("type") or "?").strip() for i in inputs if isinstance(i, dict)
        )

        if kind == "event":
            entries.append(f"event {name}({input_types})")
        elif kind == "constructor":
            entries.append(f"constructor({input_types})")

    return entries
