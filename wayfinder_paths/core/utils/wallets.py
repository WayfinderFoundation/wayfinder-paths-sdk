import json
from pathlib import Path
from typing import Any

from eth_account import Account


def make_random_wallet() -> dict[str, str]:
    acct = Account.create()
    return {
        "address": acct.address,
        "private_key_hex": acct.key.hex(),
    }


def _load_existing_wallets(file_path: Path) -> list[dict[str, Any]]:
    if not file_path.exists():
        return []
    try:
        parsed = json.loads(file_path.read_text())
        if isinstance(parsed, dict):
            wallets = parsed.get("wallets")
            if isinstance(wallets, list):
                return wallets
        return []
    except Exception:
        return []


def _save_wallets(file_path: Path, wallets: list[dict[str, Any]]) -> None:
    config = {}
    if file_path.exists():
        try:
            config = json.loads(file_path.read_text())
        except Exception:
            pass

    sorted_wallets = sorted(wallets, key=lambda w: w.get("address", ""))
    config["wallets"] = sorted_wallets
    file_path.write_text(json.dumps(config, indent=2))


def write_wallet_to_json(
    wallet: dict[str, str], out_dir: str | Path = ".", filename: str = "config.json"
) -> Path:
    out_dir_path = Path(out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)
    file_path = out_dir_path / filename

    existing = _load_existing_wallets(file_path)
    index_by_address: dict[str, int] = {}
    for i, w in enumerate(existing):
        addr = w.get("address")
        if isinstance(addr, str):
            index_by_address[addr.lower()] = i

    addr_key = wallet["address"].lower()
    if addr_key in index_by_address:
        existing[index_by_address[addr_key]] = wallet
    else:
        existing.append(wallet)

    _save_wallets(file_path, existing)
    return file_path


def load_wallets(
    out_dir: str | Path = ".", filename: str = "config.json"
) -> list[dict[str, Any]]:
    return _load_existing_wallets(Path(out_dir) / filename)
