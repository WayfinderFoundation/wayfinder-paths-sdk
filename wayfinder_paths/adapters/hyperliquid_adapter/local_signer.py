from collections.abc import Awaitable, Callable
from typing import Any

from eth_account import Account


def _resolve_private_key(config: dict[str, Any]) -> str | None:
    """Extract private key from config."""
    # Try strategy_wallet first
    strategy_wallet = config.get("strategy_wallet", {})
    if isinstance(strategy_wallet, dict):
        pk = strategy_wallet.get("private_key_hex") or strategy_wallet.get(
            "private_key"
        )
        if pk:
            return pk

    # Try main_wallet as fallback (for single-wallet setups)
    main_wallet = config.get("main_wallet", {})
    if isinstance(main_wallet, dict):
        pk = main_wallet.get("private_key_hex") or main_wallet.get("private_key")
        if pk:
            return pk

    return None


def create_local_signer(config: dict[str, Any]) -> Callable[[dict], Awaitable[str]]:
    private_key = _resolve_private_key(config)
    if not private_key:
        raise ValueError(
            "No private key found in config. "
            "Provide strategy_wallet.private_key_hex or strategy_wallet.private_key"
        )

    # Create account
    pk = private_key if private_key.startswith("0x") else "0x" + private_key
    account: Account = Account.from_key(pk)

    async def sign(
        action: dict[str, Any], payload: str, address: str
    ) -> dict[str, str] | None:
        # Verify address matches account
        if address.lower() != account.address.lower():
            return None

        # Sign the hash
        signed = account.sign_message(payload)
        return {"r": hex(signed.r), "s": hex(signed.s), "v": signed.v}

    return sign
