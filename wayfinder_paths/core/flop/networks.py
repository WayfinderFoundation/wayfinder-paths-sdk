"""The FLOP networks this SDK can work on, and which one an account is on.

The backend reports the genesis hash of the network the user's FLOP account is used on;
that hash, not a name, picks the profile here, so a box never signs for one chain with
another chain's endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FlopNetwork:
    name: str
    genesis_hash: str
    rpc_url: str
    faucet_url: str
    # FLOP's hosted inference gateway. Testnet has none yet (`gateway.testnet.flop.network`
    # is announced but does not resolve), so testnet inference is direct sessions only.
    gateway_url: str | None
    # The model the daily job buys one small turn of. It must be a verified registry
    # entry on this network; the job checks that before it escrows anything.
    daily_spend_model: str


DEVNET = FlopNetwork(
    name="devnet",
    genesis_hash="0x99c172c095340ace980631f9123544c6bebd4839f3437e463533422d8be2d631",
    rpc_url="wss://rpc.devnet.flop.network",
    faucet_url="https://faucet.devnet.flop.network",
    gateway_url="https://api.flop.network/v1",
    daily_spend_model="qwen/qwen2.5-0.5b-instruct",
)
# Opened 24 November 2026.
TESTNET = FlopNetwork(
    name="testnet",
    genesis_hash="0x918abd58fb16306b30ef3886514cfaebffecb4de43e82b6f8ec4795c43cfd800",
    rpc_url="wss://testnet.flop.network",
    faucet_url="https://faucet.flop.network",
    gateway_url=None,
    # Testnet has no 0.5B model; Qwen3-1.7B is the smallest it registers.
    daily_spend_model="Qwen/Qwen3-1.7B",
)
NETWORKS = (DEVNET, TESTNET)


class UnknownFlopNetworkError(Exception):
    pass


def by_genesis(genesis_hash: str) -> FlopNetwork:
    wanted = genesis_hash.lower()
    for network in NETWORKS:
        if network.genesis_hash == wanted:
            return network
    raise UnknownFlopNetworkError(
        f"FLOP network with genesis {genesis_hash} is not one this SDK knows"
    )


def for_account(account: dict[str, Any]) -> FlopNetwork:
    return by_genesis(str(account["genesis_hash"]))
