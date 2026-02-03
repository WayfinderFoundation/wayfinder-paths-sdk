from wayfinder_paths.core.constants.contracts import PRJX_NPM, PRJX_ROUTER
from wayfinder_paths.policies.util import allow_functions


async def prjx_swap():
    return await allow_functions(
        policy_name="Allow PRJX Swap",
        abi_chain_id=999,
        address=PRJX_ROUTER,
        function_names=[
            "exactInput",
            "exactInputSingle",
            "exactOutput",
            "exactOutputSingle",
        ],
    )


async def prjx_npm():
    return await allow_functions(
        policy_name="Allow PRJX NPM",
        abi_chain_id=999,
        address=PRJX_NPM,
        function_names=[
            "increaseLiquidity",
            "decreaseLiquidity",
        ],
    )
