"""The FLOP network this SDK works on, and the check that an account is on it.

Only FLOP Testnet is supported. The backend reports the genesis hash of the network the
user's FLOP account is used on; that hash, not a name, picks the profile here, so a box
never signs for one chain with another chain's endpoints.
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
    # The strongest model this network serves: what inference uses when no model is named,
    # and what the daily job buys its turn of (it checks the model is a verified registry
    # entry here before it escrows anything).
    default_model: str


# Opened 24 November 2026. It has no hosted inference gateway: inference is a direct
# session with a miner.
TESTNET = FlopNetwork(
    name="testnet",
    genesis_hash="0x918abd58fb16306b30ef3886514cfaebffecb4de43e82b6f8ec4795c43cfd800",
    rpc_url="wss://testnet.flop.network",
    faucet_url="https://faucet.flop.network",
    # The largest of its verified models (all Qwen, 1.7B to 30B).
    default_model="Qwen/Qwen3-30B-A3B-FP8",
)
NETWORKS = (TESTNET,)


class UnknownFlopNetworkError(Exception):
    pass


def by_genesis(genesis_hash: str) -> FlopNetwork:
    wanted = genesis_hash.lower()
    for network in NETWORKS:
        if network.genesis_hash == wanted:
            return network
    raise UnknownFlopNetworkError(
        f"FLOP network with genesis {genesis_hash} is not FLOP Testnet, the only "
        "network this SDK supports"
    )


def for_account(account: dict[str, Any]) -> FlopNetwork:
    return by_genesis(str(account["genesis_hash"]))
