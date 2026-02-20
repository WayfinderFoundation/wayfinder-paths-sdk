from wayfinder_paths.core.constants.contracts import AERODROME_ROUTER
from wayfinder_paths.policies.util import allow_functions


async def aerodrome_swap():
    return await allow_functions(
        policy_name="Allow aerodrome Swap",
        abi_chain_id=999,
        address=AERODROME_ROUTER,
        function_names=[
            "exactInput",
            "exactInputSingle",
            "exactOutput",
            "exactOutputSingle",
        ],
    )
