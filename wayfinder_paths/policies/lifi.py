from wayfinder_paths.core.constants.contracts import (
    LIFI_GENERIC,
    LIFI_ROUTER_HYPEREVM,
    LIFI_ROUTER_ROBINHOOD,
)
from wayfinder_paths.policies.util import allow_functions

LIFI_ROUTERS: dict[int, str] = {
    999: LIFI_ROUTER_HYPEREVM,
    4663: LIFI_ROUTER_ROBINHOOD,
}


async def lifi_swap(chain_id):
    # NOTE: we get the abi from the base contract as it is a generic abi used everywhere
    # and not all chains have this ABI published
    return await allow_functions(
        policy_name="Allow LIFI Swap",
        abi_chain_id=8453,
        address=LIFI_ROUTERS[chain_id],
        function_names=[
            "swapTokensMultipleV3ERC20ToERC20",
        ],
        abi_address_override=LIFI_GENERIC,
    )
