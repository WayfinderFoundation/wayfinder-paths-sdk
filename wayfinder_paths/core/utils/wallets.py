import json
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data
from loguru import logger

from wayfinder_paths.core.clients.OpenCodeClient import OPENCODE_CLIENT
from wayfinder_paths.core.clients.WalletClient import WALLET_CLIENT
from wayfinder_paths.core.config import (
    CONFIG,
    get_api_key,
    get_opencode_instance_id,
    load_config_json,
    load_wallet_mnemonic,
    write_wallet_mnemonic,
)
from wayfinder_paths.policies.ttl import build_ttl_policy

_DEFAULT_EVM_ACCOUNT_PATH_TEMPLATE = "m/44'/60'/0'/0/{index}"

Account.enable_unaudited_hdwallet_features()


# ---------------------------------------------------------------------------
# Loading wallets
# ---------------------------------------------------------------------------


def _load_local_wallets() -> list[dict[str, Any]]:
    wallets = CONFIG.get("wallets")
    if isinstance(wallets, list):
        return [w for w in wallets if isinstance(w, dict)]
    return []


async def load_remote_wallets() -> list[dict[str, Any]]:
    api_key = get_api_key()
    if not api_key:
        return []
    try:
        raw = await WALLET_CLIENT.list_wallets()
        wallets = []
        for i, w in enumerate(raw):
            addr = w.get("wallet_address")
            if not addr:
                continue
            wallets.append(
                {
                    "address": addr,
                    "label": w.get("label") or f"remote-{i}",
                    "type": "remote",
                    "chain_type": w.get("chain_type", "ethereum"),
                    "wallet_type": w.get("wallet_type", "policy"),
                }
            )
        return wallets
    except Exception as exc:
        logger.debug(f"Failed to fetch remote wallets: {exc}")
        return []


async def load_wallets() -> list[dict[str, Any]]:
    """Load local + remote wallets."""
    local = _load_local_wallets()
    remote = await load_remote_wallets()
    local_addrs = {str(w.get("address", "")).lower() for w in local}
    for w in remote:
        if str(w.get("address", "")).lower() not in local_addrs:
            local.append(w)
    return local


# ---------------------------------------------------------------------------
# Finding wallets by label
# ---------------------------------------------------------------------------


async def find_wallet_by_label(label: str) -> dict[str, Any] | None:
    """Async lookup — local + remote wallets."""
    want = str(label).strip()
    if not want:
        return None
    for w in await load_wallets():
        if str(w.get("label", "")).strip() == want:
            return w
    return None


# ---------------------------------------------------------------------------
# Signing callbacks (local)
# ---------------------------------------------------------------------------


def account_from_key(private_key: str) -> Account:
    pk = private_key.strip()
    return Account.from_key(pk if pk.startswith("0x") else f"0x{pk}")


def get_private_key(wallet: dict[str, Any]) -> str | None:
    pk = wallet.get("private_key") or wallet.get("private_key_hex")
    return str(pk).strip() if pk else None


def get_local_sign_callback(private_key: str):
    account = account_from_key(private_key)

    async def sign_callback(transaction: dict) -> bytes:
        signed = account.sign_transaction(transaction)
        return signed.raw_transaction

    return sign_callback


def get_local_sign_typed_data_callback(private_key: str):
    account = account_from_key(private_key)

    async def sign_typed_data(payload: str | dict) -> str:
        msg = json.loads(payload) if isinstance(payload, str) else payload
        signable = encode_typed_data(full_message=msg)
        signed = account.sign_message(signable)
        return "0x" + signed.signature.hex()

    return sign_typed_data


def get_local_sign_hash_callback(private_key: str):
    account = account_from_key(private_key)

    async def sign_hash(hash_hex: str) -> str:
        h = hash_hex if hash_hex.startswith("0x") else f"0x{hash_hex}"
        signed = account.unsafe_sign_hash(bytes.fromhex(h[2:]))
        return "0x" + signed.signature.hex()

    return sign_hash


# ---------------------------------------------------------------------------
# Signing callbacks (remote)
# ---------------------------------------------------------------------------


def _prepare_tx_for_privy(transaction: dict) -> dict:
    """Prepare a transaction dict for Privy: infer type, hex-encode large ints."""

    # privy wants transaction type
    tx = dict(transaction)
    if "type" not in tx:
        if "maxFeePerGas" in tx:
            tx["type"] = 2
        elif "gasPrice" in tx:
            tx["type"] = 0

    # privy wants hexes for large ints
    for key in (
        "value",
        "gas",
        "gasPrice",
        "maxFeePerGas",
        "maxPriorityFeePerGas",
        "nonce",
        "chainId",
    ):
        val = tx.get(key)
        if isinstance(val, int):
            tx[key] = hex(val)
    # web3py sets `to` = b'' for contract deploys; Privy wants it omitted
    if isinstance(tx.get("to"), bytes) and not tx["to"]:
        del tx["to"]
    return tx


def get_remote_sign_callback(wallet_address: str):
    async def sign_callback(transaction: dict) -> bytes:
        transaction["from"] = wallet_address
        hex_str = await WALLET_CLIENT.sign_transaction(
            wallet_address, _prepare_tx_for_privy(transaction)
        )
        return bytes.fromhex(hex_str.removeprefix("0x"))

    return sign_callback


def _sanitize_typed_data(obj: Any) -> Any:
    """Recursively hex-encode bytes for JSON serialization of EIP-712 payloads."""
    if isinstance(obj, bytes):
        return "0x" + obj.hex()
    if isinstance(obj, dict):
        return {k: _sanitize_typed_data(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_typed_data(v) for v in obj]
    return obj


def get_remote_sign_typed_data_callback(wallet_address: str):
    async def sign_typed_data(payload: str | dict) -> str:
        msg = json.loads(payload) if isinstance(payload, str) else payload
        sig = await WALLET_CLIENT.sign_typed_data(
            wallet_address, _sanitize_typed_data(msg)
        )
        return sig if sig.startswith("0x") else f"0x{sig}"

    return sign_typed_data


def get_remote_sign_hash_callback(wallet_address: str):
    async def sign_hash(hash_hex: str) -> str:
        h = hash_hex if hash_hex.startswith("0x") else f"0x{hash_hex}"
        sig = await WALLET_CLIENT.sign_hash(wallet_address, h)
        return sig if sig.startswith("0x") else f"0x{sig}"

    return sign_hash


# ---------------------------------------------------------------------------
# Wallet resolution → signing callback (unified local/remote)
# ---------------------------------------------------------------------------


def _require_wallet_address(wallet: dict[str, Any], label: str) -> str:
    address = wallet.get("address")
    if not address:
        raise ValueError(f"Wallet '{label}' has no address.")
    return str(address).strip()


def _build_signing_callback(wallet: dict[str, Any], label: str):
    address = _require_wallet_address(wallet, label)
    if wallet.get("type") == "remote":
        return get_remote_sign_callback(address), address
    else:
        pk = get_private_key(wallet)
        if not pk:
            raise ValueError(f"Wallet '{label}' is missing private_key_hex.")
        return get_local_sign_callback(pk), address


def _build_typed_data_callback(wallet: dict[str, Any], label: str):
    address = _require_wallet_address(wallet, label)
    if wallet.get("type") == "remote":
        return get_remote_sign_typed_data_callback(address), address
    pk = get_private_key(wallet)
    if not pk:
        raise ValueError(f"Wallet '{label}' is missing private_key_hex.")
    return get_local_sign_typed_data_callback(pk), address


def _build_sign_hash_callback(wallet: dict[str, Any], label: str):
    address = _require_wallet_address(wallet, label)
    if wallet.get("type") == "remote":
        return get_remote_sign_hash_callback(address), address
    pk = get_private_key(wallet)
    if not pk:
        raise ValueError(f"Wallet '{label}' is missing private_key_hex.")
    return get_local_sign_hash_callback(pk), address


async def resolve_wallet(label: str) -> tuple[str, str]:
    """Look up wallet by label, return (address, private_key). Local only."""
    wallet = await find_wallet_by_label(label)
    if not wallet:
        raise ValueError(f"Wallet '{label}' not found.")
    if wallet.get("type") == "remote":
        raise ValueError(
            f"Wallet '{label}' is remote — use get_wallet_signing_callback instead."
        )
    address = _require_wallet_address(wallet, label)
    pk = get_private_key(wallet)
    if not pk:
        raise ValueError(f"Wallet '{label}' is missing private_key_hex.")
    return address, pk


async def get_wallet_signing_callback(label: str):
    """Async — local + remote. Returns (sign_callback, address)."""
    wallet = await find_wallet_by_label(label)
    if not wallet:
        raise ValueError(f"Wallet '{label}' not found.")
    return _build_signing_callback(wallet, label)


async def get_wallet_sign_typed_data_callback(label: str):
    """Async — local + remote. Returns (sign_typed_data_callback, address)."""
    wallet = await find_wallet_by_label(label)
    if not wallet:
        raise ValueError(f"Wallet '{label}' not found.")
    return _build_typed_data_callback(wallet, label)


async def get_wallet_sign_hash_callback(label: str):
    """Async — local + remote. Returns (sign_hash_callback, address)."""
    wallet = await find_wallet_by_label(label)
    if not wallet:
        raise ValueError(f"Wallet '{label}' not found.")
    return _build_sign_hash_callback(wallet, label)


# ---------------------------------------------------------------------------
# Creating wallets
# ---------------------------------------------------------------------------


async def create_remote_wallet(
    label: str = "",
    chain_type: str = "ethereum",
    policies: list[dict] = [],  # noqa: B006
) -> dict[str, Any]:
    if len(policies) == 0:
        wallet_type = "ttl"
        policies = [build_ttl_policy()]
    else:
        wallet_type = "policy"
    result = await WALLET_CLIENT.create_wallet(
        chain_type=chain_type, policies=policies, label=label, wallet_type=wallet_type
    )
    if OPENCODE_CLIENT.healthy():
        await WALLET_CLIENT.bind_to_instance(
            result["wallet_address"], get_opencode_instance_id()
        )
    return result


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
    path = _DEFAULT_EVM_ACCOUNT_PATH_TEMPLATE.format(index=account_index)
    acct = Account.from_mnemonic(mnemonic, account_path=path)
    return {
        "address": acct.address,
        "private_key_hex": acct.key.hex(),
        "derivation_path": path,
        "derivation_index": account_index,
    }


def make_local_wallet(
    *,
    label: str,
    existing_wallets: list[dict[str, Any]] | None = None,
    mnemonic: str | None = None,
) -> dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Mnemonic helpers
# ---------------------------------------------------------------------------


def generate_wallet_mnemonic(*, num_words: int = 12) -> str:
    _acct, mnemonic = Account.create_with_mnemonic(num_words=num_words)
    return " ".join(str(mnemonic).strip().split())


def validate_wallet_mnemonic(mnemonic: str) -> str:
    phrase = " ".join(str(mnemonic).strip().split())
    if not phrase:
        raise ValueError("mnemonic is empty")
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


# ---------------------------------------------------------------------------
# Config file I/O
# ---------------------------------------------------------------------------


def write_wallet_to_json(
    wallet: dict[str, Any], out_dir: str | Path = ".", filename: str = "config.json"
) -> Path:
    out_dir_path = Path(out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)
    file_path = out_dir_path / filename

    config = load_config_json(file_path)
    existing: list[dict[str, Any]] = config.get("wallets", [])
    if not isinstance(existing, list):
        existing = []
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
    config["wallets"] = sorted(existing, key=lambda w: w.get("address", ""))
    file_path.write_text(json.dumps(config, indent=2))
    return file_path
