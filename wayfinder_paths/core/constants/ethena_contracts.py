from __future__ import annotations

from eth_utils import to_checksum_address

# ---------------------------------------------------------------------------
# Ethena: key addresses
# ---------------------------------------------------------------------------
#
# Notes:
# - Canonical sUSDe staking vault (ERC-4626) lives on Ethereum mainnet.
# - On most other EVM chains, USDe/sUSDe/ENA are LayerZero OFT tokens and are
#   not the canonical staking vault (i.e., no ERC-4626 deposit/withdraw there).
#
# Sources: Ethena docs (key addresses / staking USDe).

# Ethereum mainnet
ETHENA_USDE_MAINNET = to_checksum_address("0x4c9EDD5852cd905f086C759E8383e09bff1E68B3")
ETHENA_SUSDE_VAULT_MAINNET = to_checksum_address(
    "0x9D39A5DE30e57443BfF2A8307A4256c8797A3497"
)
ETHENA_REWARDS_DISTRIBUTOR_MAINNET = to_checksum_address(
    "0xf2fa332bd83149c66b09b45670bce64746c6b439"
)
ETHENA_USDE_SILO_MAINNET = to_checksum_address(
    "0x7FC7c91D556B400AFa565013E3F32055a0713425"
)
ETHENA_ENA_MAINNET = to_checksum_address("0x57e114B691Db790C35207b2e685D4A43181e6061")

# Most EVM chains (non-mainnet): OFT token addresses
ETHENA_USDE_OFT = to_checksum_address("0x5d3a1Ff2b6BAb83b63cd9AD0787074081a52ef34")
ETHENA_SUSDE_OFT = to_checksum_address("0x211Cc4DD073734dA055fbF44a2b4667d5E5fE5d2")
ETHENA_ENA_OFT = to_checksum_address("0x58538e6A46E07434d7E7375Bc268D3cb839C0133")

# Exceptions (EVM)
ETHENA_USDE_ZKSYNC = to_checksum_address("0x39Fe7a0DACcE31Bd90418e3e659fb0b5f0B3Db0d")
ETHENA_SUSDE_ZKSYNC = to_checksum_address("0xAD17Da2f6Ac76746EF261E835C50b2651ce36DA8")
ETHENA_ENA_ZKSYNC = to_checksum_address("0x686b311F82b407f0be842652a98e5619F64cC25F")

ETHENA_ENA_ZIRCUIT = to_checksum_address("0x813635891aA06bd55036bbd8f7d1A34aB3de9a0F")

# Chain IDs for known exceptions (kept local to this module to avoid expanding
# the global supported-chain list).
CHAIN_ID_ZKSYNC_ERA = 324
CHAIN_ID_ZIRCUIT = 48900


def ethena_tokens_by_chain_id(chain_id: int) -> dict[str, str]:
    """
    Return token addresses for USDe / sUSDe / ENA on a given EVM chain.

    - On mainnet (1), sUSDe is the canonical ERC-4626 vault (staking contract).
    - On most other EVM chains, the addresses are OFT representations.
    - zkSync and Zircuit have exceptions documented by Ethena.
    """
    cid = int(chain_id)

    if cid == 1:
        return {
            "usde": ETHENA_USDE_MAINNET,
            "susde": ETHENA_SUSDE_VAULT_MAINNET,
            "ena": ETHENA_ENA_MAINNET,
        }

    if cid == CHAIN_ID_ZKSYNC_ERA:
        return {
            "usde": ETHENA_USDE_ZKSYNC,
            "susde": ETHENA_SUSDE_ZKSYNC,
            "ena": ETHENA_ENA_ZKSYNC,
        }

    # Zircuit: ENA differs; USDe/sUSDe match the default OFT addresses.
    if cid == CHAIN_ID_ZIRCUIT:
        return {
            "usde": ETHENA_USDE_OFT,
            "susde": ETHENA_SUSDE_OFT,
            "ena": ETHENA_ENA_ZIRCUIT,
        }

    return {
        "usde": ETHENA_USDE_OFT,
        "susde": ETHENA_SUSDE_OFT,
        "ena": ETHENA_ENA_OFT,
    }
