"""FLOP accounts controlled by a secp256k1 key (the chain's `Ecdsa` signature type)."""

from __future__ import annotations

from eth_keys import keys as eth_keys
from substrateinterface.utils.ss58 import ss58_decode, ss58_encode

from wayfinder_paths.core.flop.wire import blake2_256

SS58_PREFIX = 42
FLOP_DECIMALS = 18
RAW_PER_FLOP = 10**FLOP_DECIMALS


def account_id_from_public_key(compressed_public_key: bytes) -> bytes:
    if len(compressed_public_key) != 33:
        raise ValueError("expected a 33-byte compressed secp256k1 public key")
    return blake2_256(compressed_public_key)


def address_from_account_id(account_id: bytes) -> str:
    return ss58_encode(account_id, SS58_PREFIX)


def account_id_from_address(address: str) -> bytes:
    return bytes.fromhex(ss58_decode(address))


def signing_digest(payload: bytes) -> bytes:
    """What the key signs for a transaction: payloads over 256 bytes are hashed first."""
    message = payload if len(payload) <= 256 else blake2_256(payload)
    return blake2_256(message)


def normalise_signature(signature: bytes) -> bytes:
    """65 bytes r || s || v with v as 0/1; Ethereum signers return 27/28."""
    if len(signature) != 65:
        raise ValueError("expected a 65-byte recoverable signature")
    v = signature[64]
    return signature[:64] + bytes([v - 27 if v >= 27 else v])


def recover_compressed_public_key(digest: bytes, signature: bytes) -> bytes:
    recoverable = eth_keys.Signature(normalise_signature(signature))
    return recoverable.recover_public_key_from_msg_hash(digest).to_compressed_bytes()


def flop_to_raw(amount: str | int | float) -> int:
    whole, _, fraction = str(amount).partition(".")
    if len(fraction) > FLOP_DECIMALS:
        raise ValueError("FLOP amounts have at most 18 decimal places")
    return int(whole or "0") * RAW_PER_FLOP + int(
        fraction.ljust(FLOP_DECIMALS, "0") or "0"
    )


def raw_to_flop(raw: int) -> str:
    whole, fraction = divmod(raw, RAW_PER_FLOP)
    return f"{whole}.{fraction:018d}".rstrip("0").rstrip(".")
