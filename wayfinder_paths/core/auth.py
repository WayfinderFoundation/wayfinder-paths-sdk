import time

from eth_account import Account
from eth_account.messages import encode_defunct

from wayfinder_paths.core.config import (
    CONFIG,
    get_api_key,
    get_nft_token_id,
    use_nft_authentication,
)


def build_auth_headers() -> dict[str, str]:
    if use_nft_authentication():
        return _build_nft_auth_headers()
    api_key = get_api_key()
    if api_key:
        return {"X-API-KEY": api_key}
    return {}


def _build_nft_auth_headers() -> dict[str, str]:
    token_id = get_nft_token_id()
    if token_id is None:
        raise ValueError(
            "system.nft_token_id is required when use_nft_authentication is true"
        )

    wallets = CONFIG.get("wallets", [])
    if not wallets:
        raise ValueError("No wallets in config.json for NFT auth signing")
    wallet = next((w for w in wallets if w.get("label") == "main"), None)
    if not wallet:
        raise ValueError("No 'main' wallet found in config.json for NFT auth signing")
    pk = wallet.get("private_key_hex") or wallet.get("private_key")
    if not pk:
        raise ValueError("Main wallet missing private_key_hex")

    timestamp = str(int(time.time()))
    signable = encode_defunct(text=f"Vault API Auth:{timestamp}")
    key = pk if pk.startswith("0x") else f"0x{pk}"
    signed = Account.from_key(key).sign_message(signable)

    return {
        "X-NFT-Token-Id": str(token_id),
        "X-Wallet-Signature": f"0x{signed.signature.hex()}",
        "X-Signature-Timestamp": timestamp,
    }
