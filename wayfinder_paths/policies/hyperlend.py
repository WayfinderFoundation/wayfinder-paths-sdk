from wayfinder_paths.core.constants.contracts import HYPERLEND_POOL
from wayfinder_paths.policies.util import allow_functions


async def hyperlend_supply_and_withdraw():
    return await allow_functions(
        policy_name="Allow Hyperlend Supply and Withdraw",
        abi_chain_id=999,
        address=HYPERLEND_POOL,
        function_names=["supply", "withdraw"],
    )
