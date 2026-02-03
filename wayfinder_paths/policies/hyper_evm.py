from wayfinder_paths.core.constants.contracts import (
    HYPERCORE_SENTINEL_ADDRESS,
    HYPERCORE_SENTINEL_VALUE,
    HYPEREVM_WHYPE,
)
from wayfinder_paths.policies.evm import native_transfer
from wayfinder_paths.policies.util import allow_functions

WHYPE_TOKEN = HYPEREVM_WHYPE


def hypecore_sentinel_deposit():
    return native_transfer(HYPERCORE_SENTINEL_ADDRESS, HYPERCORE_SENTINEL_VALUE)


async def whype_deposit_and_withdraw():
    return await allow_functions(
        policy_name="Allow WHYPE Deposit and Withdraw",
        abi_chain_id=999,
        address=WHYPE_TOKEN,
        function_names=["deposit", "withdraw"],
    )
