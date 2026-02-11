import json
from pathlib import Path
from typing import Any

from eth_account import Account

from wayfinder_paths.core.config import load_config_json

_WALLET_MNEMONIC_KEY = "wallet_mnemonic"
_DEFAULT_EVM_ACCOUNT_PATH_TEMPLATE = "m/44'/60'/0'/0/{index}"


def make_random_wallet() -> dict[str, str]:
    acct = Account.create()
    return {
        "address": acct.address,
        "private_key_hex": acct.key.hex(),
    }


def make_wallet_from_mnemonic(
    mnemonic: str,
    *,
    account_index: int = 0,
) -> dict[str, Any]:
    """Derive an EVM wallet from a BIP-39 mnemonic.

    Uses MetaMask's default derivation path: ``m/44'/60'/0'/0/{account_index}``.
    """
    Account.enable_unaudited_hdwallet_features()
    path = _DEFAULT_EVM_ACCOUNT_PATH_TEMPLATE.format(index=account_index)
    acct = Account.from_mnemonic(mnemonic, account_path=path)
    return {
        "address": acct.address,
        "private_key_hex": acct.key.hex(),
        "derivation_path": path,
        "derivation_index": account_index,
    }


def generate_wallet_mnemonic(*, num_words: int = 24) -> str:
    Account.enable_unaudited_hdwallet_features()
    _acct, mnemonic = Account.create_with_mnemonic(num_words=num_words)
    return mnemonic


def load_wallet_mnemonic(
    out_dir: str | Path = ".", filename: str = "config.json"
) -> str | None:
    file_path = Path(out_dir) / filename
    config = load_config_json(file_path)
    value = config.get(_WALLET_MNEMONIC_KEY)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def write_wallet_mnemonic(
    mnemonic: str,
    *,
    out_dir: str | Path = ".",
    filename: str = "config.json",
) -> Path:
    out_dir_path = Path(out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)
    file_path = out_dir_path / filename

    config = load_config_json(file_path)
    config[_WALLET_MNEMONIC_KEY] = mnemonic
    file_path.write_text(json.dumps(config, indent=2))
    return file_path


def ensure_wallet_mnemonic(
    *,
    out_dir: str | Path = ".",
    filename: str = "config.json",
    num_words: int = 24,
) -> str:
    existing = load_wallet_mnemonic(out_dir, filename)
    if existing:
        return existing
    mnemonic = generate_wallet_mnemonic(num_words=int(num_words))
    write_wallet_mnemonic(mnemonic, out_dir=out_dir, filename=filename)
    return mnemonic


def next_derivation_index_for_mnemonic(
    mnemonic: str,
    wallets: list[dict[str, Any]],
    *,
    start: int = 1,
    max_tries: int = 10_000,
) -> int:
    """Find the next unused derivation index for a mnemonic.

    This avoids clobbering existing wallets even if they don't have derivation
    metadata by checking derived addresses against addresses already in config.
    """
    existing_addrs = {
        str(w.get("address", "")).lower()
        for w in wallets
        if isinstance(w, dict) and w.get("address")
    }

    for i in range(start, start + max_tries):
        derived = make_wallet_from_mnemonic(mnemonic, account_index=i)
        if str(derived.get("address", "")).lower() not in existing_addrs:
            return i

    raise RuntimeError("Unable to find an unused derivation index")


def make_local_wallet(
    *,
    label: str,
    existing_wallets: list[dict[str, Any]] | None = None,
    mnemonic: str | None = None,
) -> dict[str, Any]:
    """Create a local dev wallet.

    - If a mnemonic is provided, derive MetaMask-style accounts.
    - Otherwise, generate a random wallet.
    """
    wallets = existing_wallets or []
    if mnemonic:
        derivation_index = (
            0
            if label.lower() == "main"
            else next_derivation_index_for_mnemonic(mnemonic, wallets, start=1)
        )
        wallet = make_wallet_from_mnemonic(mnemonic, account_index=derivation_index)
    else:
        wallet = make_random_wallet()
    wallet["label"] = label
    return wallet


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
    wallet: dict[str, Any], out_dir: str | Path = ".", filename: str = "config.json"
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
