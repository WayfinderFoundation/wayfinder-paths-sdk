import json
from pathlib import Path
from typing import Any

from eth_account import Account

from wayfinder_paths.core.config import load_wallet_mnemonic, write_wallet_mnemonic

_DEFAULT_EVM_ACCOUNT_PATH_TEMPLATE = "m/44'/60'/0'/0/{index}"

Account.enable_unaudited_hdwallet_features()


def make_sign_callback(private_key: str):
    account = Account.from_key(private_key)

    async def sign_callback(transaction: dict) -> bytes:
        signed = account.sign_transaction(transaction)
        return signed.raw_transaction

    return sign_callback


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
    path = _DEFAULT_EVM_ACCOUNT_PATH_TEMPLATE.format(index=account_index)
    acct = Account.from_mnemonic(mnemonic, account_path=path)
    return {
        "address": acct.address,
        "private_key_hex": acct.key.hex(),
        "derivation_path": path,
        "derivation_index": account_index,
    }


def generate_wallet_mnemonic(*, num_words: int = 12) -> str:
    _acct, mnemonic = Account.create_with_mnemonic(num_words=num_words)
    return " ".join(str(mnemonic).strip().split())


def validate_wallet_mnemonic(mnemonic: str) -> str:
    phrase = " ".join(str(mnemonic).strip().split())
    if not phrase:
        raise ValueError("mnemonic is empty")
    # Raises if the phrase is not a valid mnemonic.
    make_wallet_from_mnemonic(phrase, account_index=0)
    return phrase


def ensure_wallet_mnemonic(
    *,
    config_path: str | Path = "config.json",
    num_words: int = 12,
) -> str:
    existing = load_wallet_mnemonic(config_path)
    if existing:
        return existing
    mnemonic = validate_wallet_mnemonic(generate_wallet_mnemonic(num_words=num_words))
    write_wallet_mnemonic(mnemonic, config_path)
    return mnemonic


def _next_derivation_index_for_mnemonic(
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
            else _next_derivation_index_for_mnemonic(mnemonic, wallets, start=1)
        )
        wallet = make_wallet_from_mnemonic(mnemonic, account_index=derivation_index)
    else:
        existing_addrs = {
            str(w.get("address", "")).lower()
            for w in wallets
            if isinstance(w, dict) and w.get("address")
        }
        for _ in range(10_000):
            wallet = make_random_wallet()
            if wallet["address"].lower() not in existing_addrs:
                break
        else:
            raise RuntimeError("Unable to generate a unique random wallet address")
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
    addr = wallet.get("address")
    if not isinstance(addr, str) or not addr.strip():
        raise ValueError("wallet.address is required")
    label = wallet.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("wallet.label is required")

    addr_key = addr.lower()
    label_key = label.strip()

    for w in existing:
        if not isinstance(w, dict):
            continue
        existing_addr = w.get("address")
        existing_label = w.get("label")

        if isinstance(existing_addr, str) and existing_addr.lower() == addr_key:
            if w == wallet:
                return file_path
            raise ValueError(
                f"Wallet address already exists in {file_path}; refusing to overwrite: {addr}"
            )

        if (
            isinstance(existing_label, str)
            and existing_label.strip() == label_key
            and isinstance(existing_addr, str)
            and existing_addr.lower() != addr_key
        ):
            raise ValueError(
                f"Wallet label already exists in {file_path}; refusing to create duplicate: {label_key}"
            )

    existing.append(wallet)

    _save_wallets(file_path, existing)
    return file_path


def load_wallets(
    out_dir: str | Path = ".", filename: str = "config.json"
) -> list[dict[str, Any]]:
    return _load_existing_wallets(Path(out_dir) / filename)
