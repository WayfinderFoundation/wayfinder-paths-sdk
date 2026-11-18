"""Who signs for the user's FLOP account.

On a Shell the key is a Privy-held agent wallet: the backend checks every payload against its
FLOP allowlist and spend limits before asking Privy to sign. The local signer exists for
tests and for development against a throwaway key.
"""

from __future__ import annotations

from typing import Protocol

from eth_keys import keys as eth_keys

from wayfinder_paths.core.flop.account import (
    account_id_from_public_key,
    address_from_account_id,
    normalise_signature,
    signing_digest,
)
from wayfinder_paths.core.flop.wire import blake2_256


class FlopSigner(Protocol):
    account_id: bytes
    address: str

    async def sign_payload(self, payload: bytes) -> bytes: ...

    async def sign_enrollment(self, message: str) -> bytes: ...


class LocalFlopSigner:
    def __init__(self, private_key: bytes) -> None:
        self._key = eth_keys.PrivateKey(private_key)
        self.account_id = account_id_from_public_key(
            self._key.public_key.to_compressed_bytes()
        )
        self.address = address_from_account_id(self.account_id)

    async def sign_payload(self, payload: bytes) -> bytes:
        return self._key.sign_msg_hash(signing_digest(payload)).to_bytes()

    async def sign_enrollment(self, message: str) -> bytes:
        return self._key.sign_msg_hash(blake2_256(message.encode())).to_bytes()


class BackendFlopSigner:
    def __init__(self, account_id: bytes, client: BackendSigningClient) -> None:
        self.account_id = account_id
        self.address = address_from_account_id(account_id)
        self._client = client

    async def sign_payload(self, payload: bytes) -> bytes:
        signature = await self._client.sign_extrinsic("0x" + payload.hex())
        return normalise_signature(bytes.fromhex(signature.removeprefix("0x")))

    async def sign_enrollment(self, message: str) -> bytes:
        signature = await self._client.sign_gateway_enrollment(message)
        return normalise_signature(bytes.fromhex(signature.removeprefix("0x")))


class BackendSigningClient(Protocol):
    async def sign_extrinsic(self, payload_hex: str) -> str: ...

    async def sign_gateway_enrollment(self, message: str) -> str: ...
